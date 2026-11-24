"""Замикання контуру зворотного зв'язку: збереження прогнозів і сповіщення.

Дві можливості, які раніше були описані, але не працювали: таблиця прогнозів
не заповнювалася жодним кодом, а сервіс сповіщень не був підключений до
застосунку. Ці тести не дають їм від'єднатися знову.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import (
    Alert, AlertKind, ModelType, Pipeline, Prediction, Project, RunStatus, Severity,
)
from app.services import notification as notif
from app.services.plan_builder import build_plan
from app.services.predictions import compare_with_fact, save_plan_predictions

API = "/api/v1"
PROJECT_ID = 1


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict:
    r = client.post(f"{API}/auth/login",
                    json={"email": "devops@example.com", "password": "devops123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class RecordingService(notif.NotificationService):
    """Канал-накопичувач: дає змогу перевірити, що сповіщення дійсно надіслано."""

    def __init__(self):
        super().__init__(channels=[])
        self.messages: list[notif.Message] = []

    def send(self, msg: notif.Message) -> dict[str, bool]:
        self.messages.append(msg)
        return {"recording": True}


@pytest.fixture
def recorder():
    svc = RecordingService()
    notif.set_service(svc)
    yield svc
    notif.set_service(None)          # повернути типовий сервіс


def _finished_pipeline(db) -> Pipeline:
    p = db.scalars(
        select(Pipeline)
        .where(Pipeline.project_id == PROJECT_ID, Pipeline.status == RunStatus.SUCCESS)
        .order_by(Pipeline.id.desc()).limit(1)
    ).first()
    assert p is not None, "у проєкті немає завершених прогонів"
    return p


# --------------------------------------------------------- збереження прогнозів
def test_plan_predictions_are_persisted(db):
    """План спирається на прогнози, тому вони мають лишатися в базі."""
    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    commit = pipeline.commit
    plan = build_plan(db, project, commit, ref=pipeline.ref)

    saved = save_plan_predictions(db, pipeline, plan)
    types = {p.type for p in saved}
    assert types == {ModelType.FAILURE, ModelType.DURATION}

    rows = db.scalars(select(Prediction).where(Prediction.pipeline_id == pipeline.id)).all()
    assert len(rows) == 2
    risk = next(r for r in rows if r.type == ModelType.FAILURE)
    assert 0.0 <= risk.value <= 1.0, "ризик є ймовірністю"
    assert risk.payload.get("mode") == plan.mode


def test_repeated_plan_does_not_duplicate_predictions(db):
    """Повторний запит плану оновлює запис, а не плодить копії."""
    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    plan = build_plan(db, project, pipeline.commit, ref=pipeline.ref)

    save_plan_predictions(db, pipeline, plan)
    save_plan_predictions(db, pipeline, plan)
    save_plan_predictions(db, pipeline, plan)

    rows = db.scalars(select(Prediction).where(Prediction.pipeline_id == pipeline.id)).all()
    assert len(rows) == 2, "на кожен тип моделі має бути рівно один запис"


def test_comparison_with_fact_uses_real_outcome(db):
    """Для завершеного прогону поруч із прогнозом стоїть фактичне значення."""
    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    save_plan_predictions(db, pipeline, build_plan(db, project, pipeline.commit, ref=pipeline.ref))

    rows = {r["type"]: r for r in compare_with_fact(db, pipeline)}
    assert rows["failure"]["actual"] == 0.0, "прогін успішний, отже факт збою нульовий"
    assert rows["duration"]["actual"] == pytest.approx(float(pipeline.duration_s))
    assert rows["duration"]["error"] is not None
    assert rows["failure"]["model_version"], "версія моделі має бути зафіксована"


def test_comparison_hides_fact_while_pipeline_runs(db):
    """Поки прогін не завершився, факт невідомий і не підмінюється нулем."""
    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    save_plan_predictions(db, pipeline, build_plan(db, project, pipeline.commit, ref=pipeline.ref))

    original = pipeline.status
    pipeline.status = RunStatus.RUNNING
    try:
        rows = {r["type"]: r for r in compare_with_fact(db, pipeline)}
        assert rows["failure"]["actual"] is None
        assert rows["failure"]["error"] is None
    finally:
        pipeline.status = original
        db.commit()


def test_pipeline_card_returns_predictions(client: TestClient, auth: dict, db):
    """Картка прогону віддає прогноз разом із фактом."""
    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    save_plan_predictions(db, pipeline, build_plan(db, project, pipeline.commit, ref=pipeline.ref))

    r = client.get(f"{API}/pipelines/{pipeline.id}", headers=auth)
    assert r.status_code == 200, r.text
    preds = r.json()["predictions"]
    assert {p["type"] for p in preds} == {"failure", "duration"}
    assert all("predicted" in p and "actual" in p for p in preds)


def test_plan_endpoint_persists_predictions(client: TestClient, db):
    """Ендпоінт плану, який викликає CI, теж зберігає прогнози."""
    pipeline = _finished_pipeline(db)
    db.query(Prediction).filter(Prediction.pipeline_id == pipeline.id).delete()
    db.commit()

    r = client.post(f"{API}/pipelines/plan", json={
        "project_id": PROJECT_ID, "commit_sha": pipeline.commit_sha, "ref": pipeline.ref,
    })
    assert r.status_code == 200, r.text

    rows = db.scalars(select(Prediction).where(Prediction.pipeline_id == pipeline.id)).all()
    assert len(rows) == 2, "після запиту плану прогнози мають зберегтися"


# ------------------------------------------------------------------ сповіщення
def test_anomaly_alert_is_sent(db, recorder):
    """Виявлена аномалія не лише лягає в базу, а й іде каналами сповіщень."""
    pipeline = _finished_pipeline(db)
    alert = Alert(
        project_id=PROJECT_ID, pipeline_id=pipeline.id, kind=AlertKind.DURATION_SPIKE,
        severity=Severity.WARNING, message="Тестова аномалія", details={"z": 4.2},
    )
    notif.notify_alert(alert, "payments-platform")

    assert len(recorder.messages) == 1
    msg = recorder.messages[0]
    assert "payments-platform" in msg.title
    assert msg.body == "Тестова аномалія"
    assert msg.severity == Severity.WARNING


def test_recommendation_generation_notifies(client: TestClient, auth: dict, recorder, db):
    """Кожна нова рекомендація породжує сповіщення."""
    r = client.post(f"{API}/projects/{PROJECT_ID}/recommendations/generate", headers=auth)
    assert r.status_code in (200, 201), r.text
    created = r.json()
    assert len(recorder.messages) == len(created), (
        "кількість сповіщень має збігатися з кількістю нових рекомендацій"
    )


def test_broken_channel_does_not_break_the_caller(db):
    """Відмова каналу не може зупинити приймання події конвеєра."""
    class Broken(notif.Channel):
        name = "broken"

        def send(self, msg: notif.Message) -> bool:
            raise RuntimeError("канал недоступний")

    notif.set_service(notif.NotificationService(channels=[Broken()]))
    try:
        alert = Alert(project_id=PROJECT_ID, kind=AlertKind.DURATION_SPIKE,
                      severity=Severity.WARNING, message="Аномалія", details={})
        notif.notify_alert(alert, "проєкт")       # не має кинути виняток
    finally:
        notif.set_service(None)


# ═══════════════════════════════════════════════ знімок плану в прогоні
def test_plan_endpoint_stores_the_issued_plan(client: TestClient, db):
    """План має лишатися на прогоні, а не зникати після відповіді.

    Без знімка картка минулого прогону перераховувала б план сьогоднішньою
    історією і показувала б не те рішення, яке ухвалювалося насправді.
    """
    from app.models import Commit

    pipeline, commit = db.execute(
        select(Pipeline, Commit)
        .join(Commit, Pipeline.commit_sha == Commit.sha)
        .where(Pipeline.project_id == PROJECT_ID)
        .order_by(Pipeline.started_at.desc())
        .limit(1)
    ).first()

    pipeline.plan_snapshot = {}
    db.commit()

    r = client.post(f"{API}/pipelines/plan", json={
        "project_id": PROJECT_ID, "commit_sha": commit.sha, "ref": pipeline.ref,
        "author": commit.author, "message": commit.message,
        "changed_paths": commit.changed_paths or [],
    })
    assert r.status_code == 200, r.text

    db.expire_all()
    snap = db.get(Pipeline, pipeline.id).plan_snapshot
    assert snap, "план не збережено на прогоні"
    for key in ("mode", "selected_tests", "total_tests", "est_duration_s",
                "baseline_duration_s", "saving_ratio", "rationale"):
        assert key in snap, f"у знімку немає поля {key}"
    assert snap["saving_ratio"] == r.json()["saving_ratio"]


def test_dashboard_reports_potential_saving_from_snapshots(client: TestClient, auth: dict, db):
    """У холостому ході частка оптимізованих нульова за визначенням.

    Корисне число для цього режиму - обіцяна економія, порахована зі
    збережених планів. Без неї дашборд виглядав би так, ніби система нічого
    не робить.
    """
    r = client.get(f"{API}/projects/{PROJECT_ID}/dashboard?days=365", headers=auth)
    assert r.status_code == 200, r.text
    p = r.json()["pipelines"]

    assert "planned_share" in p and "potential_saving" in p
    assert 0.0 <= p["planned_share"] <= 1.0
    assert 0.0 <= p["potential_saving"] <= 1.0

    planned = sum(
        1 for x in db.scalars(select(Pipeline).where(Pipeline.project_id == PROJECT_ID))
        if (x.plan_snapshot or {}).get("saving_ratio") is not None
    )
    if planned:
        assert p["planned_share"] > 0, "є збережені плани, але частка нульова"
        assert p["potential_saving"] > 0, "є збережені плани, але економія нульова"


def test_applied_plan_marks_the_run_as_optimised(client: TestClient, db):
    """Прогін, який виконувався за планом, має бути позначений.

    Покладатися на те, що позначку проставить CI-сервер, не можна: для цього
    конвеєр мусив би повернути спеціальну змінну. За її відсутності частка
    оптимізованих прогонів лишалася б нульовою навіть у бойовому режимі, і
    система виглядала б так, ніби вона нічого не робить.
    """
    from app.models import Commit, PlanMode, Project

    project = db.get(Project, PROJECT_ID)
    saved_mode = project.plan_mode
    # Ендпоінт обирає прогін так само: найновіший за ідентифікатором для
    # цього коміту й гілки. Беремо той самий, інакше позначка опиниться на
    # іншому записі й перевірка буде хибною.
    pipeline, commit = db.execute(
        select(Pipeline, Commit)
        .join(Commit, Pipeline.commit_sha == Commit.sha)
        .where(Pipeline.project_id == PROJECT_ID, Pipeline.ref != project.default_branch)
        .order_by(Pipeline.id.desc())
        .limit(1)
    ).first()

    pipeline.optimized = False
    project.plan_mode = PlanMode.ACTIVE
    db.commit()
    try:
        r = client.post(f"{API}/pipelines/plan", json={
            "project_id": PROJECT_ID, "commit_sha": commit.sha, "ref": pipeline.ref,
            "author": commit.author, "message": commit.message,
            "changed_paths": commit.changed_paths or [],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        db.expire_all()
        fresh = db.get(Pipeline, pipeline.id)
        if body.get("applied") and body.get("mode") != "full":
            assert fresh.optimized is True, "застосований план не позначив прогін"
        else:
            assert fresh.optimized is False, "позначку поставлено, хоча план не застосовано"
    finally:
        project.plan_mode = saved_mode
        db.commit()


def test_an_applied_plan_is_not_overwritten_by_a_later_what_if(db):
    """Знімок застосованого плану — запис про подію, а не оцінка.

    План запитують за парою «коміт — гілка». Якщо для того самого коміту
    пізніше запитати план ще раз — скажімо, оцінюючи «що було б» у режимі
    холостого ходу, — старий запис не повинен затиратися. Інакше в картці
    прогону з'являється позначка «оптимізований» поряд із планом, у якому
    написано, що виконувався повний набір перевірок.
    """
    from app.services.predictions import save_plan_snapshot

    pipeline = _finished_pipeline(db)
    project = db.get(Project, PROJECT_ID)
    plan = build_plan(db, project, pipeline.commit, ref=pipeline.ref)

    was_optimized, was_snapshot = pipeline.optimized, pipeline.plan_snapshot
    try:
        pipeline.optimized = True
        pipeline.plan_snapshot = {}
        save_plan_snapshot(db, pipeline, plan)
        db.commit()
        applied = dict(pipeline.plan_snapshot)
        assert applied, "знімок застосованого плану не збережено"

        later = build_plan(db, project, pipeline.commit, ref=pipeline.ref, force_mode="full")
        save_plan_snapshot(db, pipeline, later)
        db.commit()

        assert pipeline.plan_snapshot == applied, "запис про подію перезаписано оцінкою"
    finally:
        pipeline.optimized, pipeline.plan_snapshot = was_optimized, was_snapshot
        db.commit()
