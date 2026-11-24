"""Звіт за період, повідомлення про проблеми, поштовий канал і захист маркера.

Чотири речі, яких бракувало до повного переліку варіантів використання та
нефункціональних вимог: керівник не мав звідки вивантажити показники за
довільний інтервал, користувач не мав як позначити хибне спрацювання,
зведення не можна було надіслати поштою, а маркер доступу до репозиторію
лежав у базі відкритим текстом.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.main import app
from app.models import (
    Alert, AlertKind, CIProvider, Pipeline, ProblemReport, Project, Severity,
)
from app.services import notification
from app.services.notification import Channel, EmailChannel, Message, NotificationService

API = "/api/v1"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _token(client: TestClient, email: str, password: str) -> dict:
    r = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def devops(client: TestClient) -> dict:
    return _token(client, "devops@example.com", "devops123")


@pytest.fixture(scope="module")
def developer(client: TestClient) -> dict:
    return _token(client, "dev@example.com", "dev123")


@pytest.fixture(scope="module")
def lead(client: TestClient) -> dict:
    return _token(client, "lead@example.com", "lead123")


@pytest.fixture(scope="module")
def project_id() -> int:
    db = SessionLocal()
    try:
        p = db.scalars(select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)).first()
        assert p is not None, "потрібен синтетичний стенд: scripts/seed_synthetic.py"
        return p.id
    finally:
        db.close()


@pytest.fixture(scope="module")
def busy_month(project_id: int) -> tuple[date, date]:
    """Календарний місяць, у якому проєкт має найсвіжіші прогони."""
    db = SessionLocal()
    try:
        last = db.scalar(select(func.max(Pipeline.started_at)).where(Pipeline.project_id == project_id))
    finally:
        db.close()
    assert last is not None
    first = date(last.year, last.month, 1)
    return first, last.date()


# ----------------------------------------------------------- звіт за період
def test_report_counts_exactly_the_runs_of_the_period(
    client: TestClient, lead: dict, project_id: int, busy_month: tuple[date, date],
):
    start, end = busy_month
    r = client.get(f"{API}/projects/{project_id}/report", headers=lead,
                   params={"date_from": start.isoformat(), "date_to": end.isoformat()})
    assert r.status_code == 200, r.text
    report = r.json()

    since = datetime.combine(start, time.min, tzinfo=timezone.utc)
    until = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        expected = db.scalar(
            select(func.count(Pipeline.id))
            .where(Pipeline.project_id == project_id)
            .where(Pipeline.started_at >= since).where(Pipeline.started_at < until)
        )
    finally:
        db.close()

    assert report["pipelines"]["total"] == expected > 0
    assert report["period"] == {"from": start.isoformat(), "to": end.isoformat(),
                                "days": (end - start).days + 1}
    assert sum(day["count"] for day in report["daily"]) == expected
    assert all(start.isoformat() <= day["date"] <= end.isoformat() for day in report["daily"])
    assert report["pipelines"]["success"] + report["pipelines"]["failed"] <= expected


def test_last_day_of_the_period_is_included(
    client: TestClient, lead: dict, project_id: int, busy_month: tuple[date, date],
):
    """Межі включні: звіт «по тридцяте» містить прогони тридцятого числа."""
    _, end = busy_month
    one_day = client.get(f"{API}/projects/{project_id}/report", headers=lead,
                         params={"date_from": end.isoformat(), "date_to": end.isoformat()}).json()
    assert one_day["period"]["days"] == 1
    assert one_day["pipelines"]["total"] > 0
    assert [d["date"] for d in one_day["daily"]] == [end.isoformat()]


def test_report_downloads_as_a_spreadsheet(
    client: TestClient, developer: dict, project_id: int, busy_month: tuple[date, date],
):
    start, end = busy_month
    params = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    as_json = client.get(f"{API}/projects/{project_id}/report", headers=developer, params=params).json()
    r = client.get(f"{API}/projects/{project_id}/report", headers=developer,
                   params={**params, "format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    lines = r.text.strip().splitlines()
    assert lines[0].startswith("Project,")
    assert f"Pipeline runs,{as_json['pipelines']['total']}" in lines
    header = lines.index("Date,Runs,Passed,Failed,Success rate,\"Mean duration, s\",Optimised")
    assert len(lines) - header - 1 == len(as_json["daily"])


def test_period_must_be_ordered_and_bounded(client: TestClient, lead: dict, project_id: int):
    url = f"{API}/projects/{project_id}/report"
    backwards = client.get(url, headers=lead, params={"date_from": "2026-09-30", "date_to": "2026-09-01"})
    assert backwards.status_code == 422
    too_long = client.get(url, headers=lead, params={"date_from": "2024-01-01", "date_to": "2026-09-01"})
    assert too_long.status_code == 422
    assert client.get(url).status_code == 401


def test_period_without_runs_is_an_empty_report_not_an_error(
    client: TestClient, lead: dict, project_id: int,
):
    r = client.get(f"{API}/projects/{project_id}/report", headers=lead,
                   params={"date_from": "2020-01-01", "date_to": "2020-01-31"})
    assert r.status_code == 200
    assert r.json()["pipelines"]["total"] == 0 and r.json()["daily"] == []


class _Recorder(Channel):
    name = "recorder"

    def __init__(self):
        self.messages: list[Message] = []

    def send(self, msg: Message) -> bool:
        self.messages.append(msg)
        return True


def test_digest_goes_out_through_the_configured_channels(
    client: TestClient, lead: dict, developer: dict, project_id: int, busy_month: tuple[date, date],
):
    start, end = busy_month
    params = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    recorder = _Recorder()
    notification.set_service(NotificationService([recorder]))
    try:
        denied = client.post(f"{API}/projects/{project_id}/report/send", headers=developer, params=params)
        assert denied.status_code == 403 and not recorder.messages

        r = client.post(f"{API}/projects/{project_id}/report/send", headers=lead, params=params)
        assert r.status_code == 200, r.text
        assert r.json()["channels"] == {"recorder": True}
    finally:
        notification.set_service(None)

    (msg,) = recorder.messages
    assert start.isoformat() in msg.body and end.isoformat() in msg.body
    assert msg.fields["Runs"] > 0 and "Time saved" in msg.fields


# ------------------------------------------------------------ поштовий канал
class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.user = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, mail):
        _FakeSMTP.sent.append((self, mail))


def test_email_channel_sends_a_readable_letter(monkeypatch: pytest.MonkeyPatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(notification.smtplib, "SMTP", _FakeSMTP)
    channel = EmailChannel("smtp.example.invalid", 587, "slipstream@example.invalid",
                           ["lead@example.invalid", "ops@example.invalid"],
                           username="robot", password="x")
    assert channel.send(Message(title="Digest for demo", body="Pipeline health.",
                                fields={"Runs": 42})) is True

    (smtp, mail), = _FakeSMTP.sent
    assert smtp.tls and smtp.user == "robot"
    assert mail["To"] == "lead@example.invalid, ops@example.invalid"
    assert "Digest for demo" in mail["Subject"]
    assert "Runs: 42" in mail.get_content()


def test_mail_server_failure_never_reaches_the_caller(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(notification.smtplib, "SMTP", refuse)
    channel = EmailChannel("smtp.example.invalid", 587, "a@example.invalid", ["b@example.invalid"])
    service = NotificationService([channel])
    assert service.send(Message(title="t", body="b")) == {"email": False}


def test_email_channel_is_switched_on_by_environment(monkeypatch: pytest.MonkeyPatch):
    for name in ("SLACK_WEBHOOK_URL", "NOTIFY_WEBHOOK_URL", "SMTP_HOST", "NOTIFY_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    assert [c.name for c in notification.build_from_env().channels] == ["log"]

    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    assert [c.name for c in notification.build_from_env().channels] == ["log"], \
        "без одержувачів поштовий канал не має вмикатися"
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "lead@example.invalid, ops@example.invalid")
    channels = notification.build_from_env().channels
    assert [c.name for c in channels] == ["log", "email"]
    assert channels[1].recipients == ["lead@example.invalid", "ops@example.invalid"]


# ------------------------------------------------ повідомлення про проблеми
@pytest.fixture()
def an_alert(project_id: int):
    db = SessionLocal()
    a = Alert(project_id=project_id, kind=AlertKind.DURATION_SPIKE, severity=Severity.WARNING,
              message="test alert for a problem report", details={}, acknowledged=False)
    db.add(a)
    db.commit()
    aid = a.id
    db.close()
    yield aid
    db = SessionLocal()
    for r in db.scalars(select(ProblemReport).where(ProblemReport.subject_type == "alert",
                                                    ProblemReport.subject_id == aid)).all():
        db.delete(r)
    obj = db.get(Alert, aid)
    if obj is not None:
        db.delete(obj)
    db.commit()
    db.close()


def test_any_role_can_report_a_false_alarm(
    client: TestClient, developer: dict, devops: dict, project_id: int, an_alert: int,
):
    url = f"{API}/projects/{project_id}/problems"
    r = client.post(url, headers=developer, json={
        "subject_type": "alert", "subject_id": an_alert,
        "note": "The run was slow because the runner was being upgraded, not because of the change.",
    })
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["resolved"] is False and created["reported_by"] == "dev@example.com"

    listed = client.get(url, headers=devops).json()
    assert created["id"] in [p["id"] for p in listed]


def test_report_must_say_what_is_wrong_and_about_what(
    client: TestClient, developer: dict, project_id: int, an_alert: int,
):
    url = f"{API}/projects/{project_id}/problems"
    assert client.post(url, headers=developer, json={
        "subject_type": "alert", "subject_id": an_alert, "note": ""}).status_code == 422
    assert client.post(url, headers=developer, json={
        "subject_type": "alert", "note": "no subject given"}).status_code == 422
    assert client.post(url, headers=developer, json={
        "subject_type": "alert", "subject_id": 10 ** 9, "note": "no such alert"}).status_code == 404


def test_subject_of_another_project_is_not_accepted(
    client: TestClient, developer: dict, project_id: int, an_alert: int,
):
    db = SessionLocal()
    try:
        other = db.scalars(select(Project).where(Project.id != project_id)).first()
    finally:
        db.close()
    assert other is not None
    r = client.post(f"{API}/projects/{other.id}/problems", headers=developer, json={
        "subject_type": "alert", "subject_id": an_alert, "note": "alert of a different project"})
    assert r.status_code == 404


def test_only_devops_closes_a_report(
    client: TestClient, developer: dict, devops: dict, project_id: int, an_alert: int,
):
    url = f"{API}/projects/{project_id}/problems"
    rid = client.post(url, headers=developer, json={
        "subject_type": "alert", "subject_id": an_alert, "note": "false alarm"}).json()["id"]

    assert client.put(f"{url}/{rid}/resolve", headers=developer).status_code == 403
    r = client.put(f"{url}/{rid}/resolve", headers=devops)
    assert r.status_code == 200 and r.json()["resolved"] is True and r.json()["resolved_at"]


# ------------------------------------------------- захист маркера доступу
def test_access_token_is_encrypted_at_rest(client: TestClient, devops: dict):
    secret = f"glpat-{uuid.uuid4().hex}"
    name = f"__test_token_{uuid.uuid4().hex[:8]}"
    r = client.post(f"{API}/projects", headers=devops, json={
        "name": name, "ci_provider": "gitlab", "access_token": secret})
    assert r.status_code == 201, r.text
    assert secret not in r.text, "маркер не повинен повертатися у відповіді"
    pid = r.json()["id"]

    db = SessionLocal()
    try:
        stored = db.execute(text("select access_token from projects where id = :id"), {"id": pid}).scalar()
        assert stored.startswith("enc:v1:") and secret not in stored
        assert db.get(Project, pid).access_token == secret, "через ORM маркер читається як звичайний рядок"
    finally:
        obj = db.get(Project, pid)
        db.delete(obj)
        db.commit()
        db.close()


def test_value_stored_before_encryption_is_still_readable(client: TestClient, devops: dict):
    name = f"__test_legacy_{uuid.uuid4().hex[:8]}"
    pid = client.post(f"{API}/projects", headers=devops,
                      json={"name": name, "ci_provider": "gitlab"}).json()["id"]
    db = SessionLocal()
    try:
        db.execute(text("update projects set access_token = 'plain-legacy' where id = :id"), {"id": pid})
        db.commit()
        db.expire_all()
        assert db.get(Project, pid).access_token == "plain-legacy"
    finally:
        db.delete(db.get(Project, pid))
        db.commit()
        db.close()


# ---------------------------------------------- підказка періоду на дашборді
def test_dashboard_names_a_window_that_has_data(client: TestClient, lead: dict, project_id: int):
    """Порожнє коротке вікно не лишає користувача перед нулями без пояснення."""
    narrow = client.get(f"{API}/projects/{project_id}/dashboard", headers=lead, params={"days": 1}).json()
    assert narrow["last_run_at"] is not None
    if narrow["pipelines"]["total"] > 0:
        assert narrow["suggested_days"] is None
        return
    wider = narrow["suggested_days"]
    if wider is None:      # останній прогін старший за рік: ширшого вікна немає
        return
    assert wider > 1
    assert client.get(f"{API}/projects/{project_id}/dashboard", headers=lead,
                      params={"days": wider}).json()["pipelines"]["total"] > 0
