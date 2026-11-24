"""Приймання сирих вебхуків від GitLab і GitHub.

До цього ендпоінт приймав лише вже нормалізовану подію, тому справжній
CI-сервер не міг надсилати свої повідомлення напряму - ланка між адаптерами
й системою була розірвана. Тут перевіряється, що вона з'єднана і що
непідписані або підроблені події не приймаються.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations import github as gh
from app.integrations import gitlab as gl
from app.main import app
from app.models import CIProvider, Pipeline, PlanMode, Project, RunStatus, TestCase

API = "/api/v1"
SECRET = "webhook-secret-for-tests"
SHA = "b" * 40


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def project_id() -> int:
    """Окремий проєкт із налаштованим секретом, щоб не чіпати стендові."""
    db = SessionLocal()
    try:
        p = db.scalars(select(Project).where(Project.name == "__test_webhook_signed")).first()
        if p is None:
            p = Project(
                name="__test_webhook_signed", repo_url="https://example.invalid/r",
                ci_provider=CIProvider.GITLAB, external_id="o/r",
                default_branch="main", plan_mode=PlanMode.OFF,
                webhook_secret=SECRET,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
        elif p.webhook_secret != SECRET:
            p.webhook_secret = SECRET
            db.commit()
        # Прибираємо прогони з попередніх запусків, щоб перевірки на кількість
        # результатів були повторюваними.
        for old_pipe in db.scalars(select(Pipeline).where(Pipeline.project_id == p.id)):
            db.delete(old_pipe)
        db.commit()
        return p.id
    finally:
        db.close()


@pytest.fixture(scope="module")
def unsigned_project_id() -> int:
    """Проєкт БЕЗ секрету: такі події мають відхилятися."""
    db = SessionLocal()
    try:
        p = db.scalars(select(Project).where(Project.name == "__test_webhook_unsigned")).first()
        if p is None:
            p = Project(
                name="__test_webhook_unsigned", repo_url="https://example.invalid/r2",
                ci_provider=CIProvider.GITLAB, external_id="o/r2",
                default_branch="main", plan_mode=PlanMode.OFF, webhook_secret="",
            )
            db.add(p)
            db.commit()
            db.refresh(p)
        return p.id
    finally:
        db.close()


def gitlab_payload(pipeline_id: str, status: str = "success", duration: int = 300) -> dict:
    return {
        "object_kind": "pipeline",
        "object_attributes": {
            "id": pipeline_id, "ref": "feature/x", "sha": SHA,
            "status": status, "duration": duration, "queued_duration": 12,
        },
        "commit": {"id": SHA, "message": "правка обробника", "author": {"name": "Тестер"}},
        "builds": [
            {"id": 1, "stage": "test", "name": "unit", "status": "success",
             "duration": 120, "runner": {"description": "runner-1"}},
            {"id": 2, "stage": "build", "name": "compile", "status": "success", "duration": 60},
        ],
    }


def github_payload(run_id: int, conclusion: str = "success") -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id, "head_sha": SHA, "head_branch": "feature/y",
            "status": "completed", "conclusion": conclusion,
            "created_at": "2026-01-10T10:00:00Z",
            "run_started_at": "2026-01-10T10:01:00Z",
            "updated_at": "2026-01-10T10:06:00Z",
            "run_attempt": 1, "actor": {"login": "octo"},
            "head_commit": {"message": "оновлення залежностей"},
        },
    }


# ------------------------------------------------------- перетворення форматів
def test_gitlab_payload_becomes_event():
    ev = gl.parse_webhook(1, gitlab_payload("501"))
    assert ev is not None
    assert ev.event == "pipeline_finished"
    assert ev.external_id == "501"
    assert ev.commit_sha == SHA
    assert ev.ref == "feature/x"
    assert ev.status == RunStatus.SUCCESS
    assert ev.duration_s == 300
    assert ev.queue_time_s == 12
    assert ev.author == "Тестер"
    assert {j["name"] for j in ev.jobs} == {"unit", "compile"}


def test_gitlab_running_pipeline_is_started_event():
    ev = gl.parse_webhook(1, gitlab_payload("502", status="running", duration=0))
    assert ev is not None and ev.event == "pipeline_started"
    assert ev.status == RunStatus.RUNNING


def test_gitlab_ignores_non_pipeline_events():
    assert gl.parse_webhook(1, {"object_kind": "push"}) is None


def test_github_payload_becomes_event():
    ev = gh.parse_webhook(1, github_payload(900))
    assert ev is not None
    assert ev.event == "pipeline_finished"
    assert ev.external_id == "900"
    assert ev.status == RunStatus.SUCCESS
    assert ev.duration_s == 300      # 10:01 -> 10:06
    assert ev.queue_time_s == 60     # 10:00 -> 10:01
    assert ev.author == "octo"


def test_github_failed_conclusion_maps_to_failed():
    ev = gh.parse_webhook(1, github_payload(901, conclusion="failure"))
    assert ev is not None and ev.status == RunStatus.FAILED


def test_github_ignores_unrelated_events():
    assert gh.parse_webhook(1, {"action": "opened", "issue": {}}) is None


# --------------------------------------------------------------- перевірка підпису
def test_gitlab_rejects_wrong_token(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/gitlab/{project_id}",
        json=gitlab_payload("510"),
        headers={"X-Gitlab-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_gitlab_rejects_missing_token(client: TestClient, project_id: int):
    r = client.post(f"{API}/webhooks/gitlab/{project_id}", json=gitlab_payload("511"))
    assert r.status_code == 401


def test_project_without_secret_refuses_webhooks(client: TestClient, unsigned_project_id: int):
    """Відсутній секрет - це відмова, а не дозвіл приймати будь-що."""
    r = client.post(
        f"{API}/webhooks/gitlab/{unsigned_project_id}",
        json=gitlab_payload("512"),
        headers={"X-Gitlab-Token": "anything"},
    )
    assert r.status_code == 503
    assert "secret" in r.json()["detail"].lower()


def test_github_rejects_tampered_payload(client: TestClient, project_id: int):
    body = json.dumps(github_payload(920)).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    tampered = json.dumps(github_payload(921)).encode()
    r = client.post(
        f"{API}/webhooks/github/{project_id}",
        content=tampered,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_unknown_project_returns_404(client: TestClient):
    r = client.post(
        f"{API}/webhooks/gitlab/999999",
        json=gitlab_payload("513"),
        headers={"X-Gitlab-Token": SECRET},
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ приймання
def test_gitlab_webhook_creates_pipeline(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/gitlab/{project_id}",
        json=gitlab_payload("520"),
        headers={"X-Gitlab-Token": SECRET},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["status"] == "success"

    db = SessionLocal()
    try:
        p = db.get(Pipeline, body["pipeline_id"])
        assert p is not None
        assert p.external_id == "520"
        assert p.duration_s == 300
        assert {j.name for j in p.jobs} == {"unit", "compile"}
    finally:
        db.close()


def test_github_webhook_creates_pipeline(client: TestClient, project_id: int):
    body = json.dumps(github_payload(930)).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(
        f"{API}/webhooks/github/{project_id}",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is True


def test_push_event_is_acknowledged_not_error(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/gitlab/{project_id}",
        json={"object_kind": "push"},
        headers={"X-Gitlab-Token": SECRET},
    )
    assert r.status_code == 202
    assert r.json()["accepted"] is False


# --------------------------------------------------------------- звіт JUnit
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="tests.test_orders" tests="3" failures="1" time="1.5">
    <testcase classname="tests.test_orders" name="test_create" time="0.5"/>
    <testcase classname="tests.test_orders" name="test_cancel" time="0.4">
      <failure message="AssertionError">очікувано 200, отримано 500</failure>
    </testcase>
    <testcase classname="tests.test_orders" name="test_list" time="0.6"/>
  </testsuite>
</testsuites>
"""


def test_junit_requires_token(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/junit/{project_id}?external_id=520&commit_sha={SHA}",
        content=JUNIT, headers={"X-CICD-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_junit_report_is_ingested(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/junit/{project_id}?external_id=540&commit_sha={SHA}&ref=feature/x",
        content=JUNIT, headers={"X-CICD-Token": SECRET},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["tests"] == 3
    assert body["failed"] == 1

    db = SessionLocal()
    try:
        p = db.get(Pipeline, body["pipeline_id"])
        results = [tr for j in p.jobs for tr in j.test_results]
        assert len(results) == 3
        assert sum(1 for t in results if t.status.value == "failed") == 1
    finally:
        db.close()


def test_junit_does_not_overwrite_pipeline_duration(client: TestClient, project_id: int):
    """Звіт про тести не має затирати справжню тривалість прогону нулем."""
    client.post(
        f"{API}/webhooks/gitlab/{project_id}",
        json=gitlab_payload("550", duration=420),
        headers={"X-Gitlab-Token": SECRET},
    )
    r = client.post(
        f"{API}/webhooks/junit/{project_id}?external_id=550&commit_sha={SHA}&ref=feature/x",
        content=JUNIT, headers={"X-CICD-Token": SECRET},
    )
    assert r.status_code == 202, r.text

    db = SessionLocal()
    try:
        p = db.get(Pipeline, r.json()["pipeline_id"])
        assert p.duration_s == 420, "тривалість прогону затерта звітом про тести"
    finally:
        db.close()


def test_empty_report_is_rejected(client: TestClient, project_id: int):
    r = client.post(
        f"{API}/webhooks/junit/{project_id}?external_id=560&commit_sha={SHA}",
        content="", headers={"X-CICD-Token": SECRET},
    )
    assert r.status_code == 400


def test_repeated_report_does_not_duplicate_results(client: TestClient, project_id: int):
    """Повтор вебхука не має подвоювати результати.

    GitLab і GitHub повторюють доставку, якщо не дочекалися відповіді. Якби
    повтор додавав ще один комплект результатів, частота падінь тестів була б
    заниженою, а навчальна вибірка - зіпсованою.
    """
    url = f"{API}/webhooks/junit/{project_id}?external_id=570&commit_sha={SHA}&ref=feature/x"
    first = client.post(url, content=JUNIT, headers={"X-CICD-Token": SECRET})
    assert first.status_code == 202

    def runs_of(name: str) -> int:
        db = SessionLocal()
        try:
            tc = db.scalars(
                select(TestCase).where(
                    TestCase.project_id == project_id, TestCase.name == name
                )
            ).first()
            assert tc is not None
            return tc.total_runs
        finally:
            db.close()

    before = runs_of("test_cancel")
    second = client.post(url, content=JUNIT, headers={"X-CICD-Token": SECRET})
    assert second.status_code == 202

    db = SessionLocal()
    try:
        p = db.get(Pipeline, second.json()["pipeline_id"])
        results = [tr for j in p.jobs for tr in j.test_results]
        assert len(results) == 3, f"повтор подвоїв результати: {len(results)}"
    finally:
        db.close()

    assert runs_of("test_cancel") == before, "повтор збільшив лічильник запусків тесту"
