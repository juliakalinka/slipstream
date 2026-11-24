"""API, яким користується консоль: те, чого раніше не було видно в інтерфейсі.

До цього набір сповіщень, керування секретом вебхука, редагування політик і
повернення попередньої версії моделі існували лише в коді: жодна сторінка їх
не викликала, жоден тест не перевіряв. Тут перевіряється і сама поведінка, і
розмежування прав - ці дії доступні тільки DevOps-інженеру.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import (
    Alert, AlertKind, CIProvider, ModelVersion, Project, Severity,
)

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
def project_id() -> int:
    db = SessionLocal()
    try:
        p = db.scalars(select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)).first()
        assert p is not None, "потрібен синтетичний стенд: scripts/seed_synthetic.py"
        return p.id
    finally:
        db.close()


@pytest.fixture()
def an_alert(project_id: int):
    """Сповіщення, яке можна підтвердити, і прибирання за собою."""
    db = SessionLocal()
    a = Alert(
        project_id=project_id, kind=AlertKind.DURATION_SPIKE, severity=Severity.WARNING,
        message="test alert", details={"z_score": 4.2}, acknowledged=False,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    aid = a.id
    db.close()
    yield aid
    db = SessionLocal()
    obj = db.get(Alert, aid)
    if obj is not None:
        db.delete(obj)
        db.commit()
    db.close()


# ------------------------------------------------------------- сповіщення
def test_alerts_are_listed(client: TestClient, devops: dict, project_id: int, an_alert: int):
    r = client.get(f"{API}/projects/{project_id}/alerts", headers=devops)
    assert r.status_code == 200, r.text
    assert any(a["id"] == an_alert for a in r.json())


def test_acknowledging_an_alert_hides_it_from_the_open_list(
    client: TestClient, devops: dict, project_id: int, an_alert: int
):
    r = client.put(f"{API}/projects/{project_id}/alerts/{an_alert}/ack", headers=devops)
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged"] is True

    rows = client.get(f"{API}/projects/{project_id}/alerts", headers=devops).json()
    row = next(a for a in rows if a["id"] == an_alert)
    assert row["acknowledged"] is True, "запис має лишитися в історії, але вже підтвердженим"


def test_developer_cannot_acknowledge(
    client: TestClient, developer: dict, project_id: int, an_alert: int
):
    r = client.put(f"{API}/projects/{project_id}/alerts/{an_alert}/ack", headers=developer)
    assert r.status_code == 403


def test_alert_of_another_project_is_not_found(
    client: TestClient, devops: dict, project_id: int, an_alert: int
):
    """Сповіщення не можна підтвердити через чужий проєкт."""
    other = project_id + 10_000
    r = client.put(f"{API}/projects/{other}/alerts/{an_alert}/ack", headers=devops)
    assert r.status_code == 404


# ------------------------------------------------------------ вебхук
def test_webhook_status_never_reveals_the_secret(client: TestClient, devops: dict, project_id: int):
    r = client.get(f"{API}/projects/{project_id}/webhook", headers=devops)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["endpoints"]) == {"gitlab", "github", "junit"}
    assert "secret" not in str(body).lower() or body.get("secret") is None


def test_secret_is_returned_once_and_changes_on_rotation(
    client: TestClient, devops: dict, project_id: int
):
    first = client.post(f"{API}/projects/{project_id}/webhook-secret", headers=devops)
    assert first.status_code == 201, first.text
    s1 = first.json()["secret"]
    assert len(s1) >= 20

    status = client.get(f"{API}/projects/{project_id}/webhook", headers=devops).json()
    assert status["configured"] is True

    second = client.post(f"{API}/projects/{project_id}/webhook-secret", headers=devops)
    assert second.status_code == 201
    assert second.json()["secret"] != s1, "повторний виклик має відкликати попередній секрет"


def test_developer_cannot_rotate_the_secret(client: TestClient, developer: dict, project_id: int):
    r = client.post(f"{API}/projects/{project_id}/webhook-secret", headers=developer)
    assert r.status_code == 403


# ------------------------------------------------------------- політики
def test_policy_can_be_edited_and_restored(client: TestClient, devops: dict, project_id: int):
    rows = client.get(f"{API}/projects/{project_id}/policies", headers=devops).json()
    assert rows, "у стенда мають бути політики"
    pol = rows[0]
    before = {"enabled": pol["enabled"], "branch_pattern": pol["branch_pattern"]}

    r = client.put(
        f"{API}/projects/{project_id}/policies/{pol['id']}",
        headers=devops, json={"enabled": not before["enabled"], "branch_pattern": "feature/*"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is (not before["enabled"])
    assert r.json()["branch_pattern"] == "feature/*"

    back = client.put(
        f"{API}/projects/{project_id}/policies/{pol['id']}", headers=devops, json=before,
    )
    assert back.status_code == 200
    assert back.json()["enabled"] is before["enabled"]


def test_developer_cannot_edit_a_policy(client: TestClient, developer: dict, project_id: int):
    rows = client.get(f"{API}/projects/{project_id}/policies", headers=developer).json()
    r = client.put(
        f"{API}/projects/{project_id}/policies/{rows[0]['id']}",
        headers=developer, json={"enabled": False},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- моделі
def test_previous_model_version_can_be_activated_back(
    client: TestClient, devops: dict, project_id: int
):
    """Відкат на попередню версію - штатна операція, а не аварійна."""
    db = SessionLocal()
    try:
        versions = list(db.scalars(
            select(ModelVersion)
            .where(ModelVersion.project_id == project_id)
            .order_by(ModelVersion.id.desc())
        ))
    finally:
        db.close()
    active = next((m for m in versions if m.is_active), None)
    if active is None:
        pytest.skip("у стенда немає активних моделей")
    older = next((m for m in versions if m.type == active.type and not m.is_active), None)
    if older is None:
        pytest.skip("немає попередньої версії цього типу моделі")

    r = client.put(f"{API}/models/{older.id}/activate", headers=devops)
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True

    back = client.put(f"{API}/models/{active.id}/activate", headers=devops)
    assert back.status_code == 200
    assert back.json()["is_active"] is True


def test_activating_an_unknown_model_is_not_found(client: TestClient, devops: dict):
    r = client.put(f"{API}/models/99999999/activate", headers=devops)
    assert r.status_code == 404


# ------------------------------------------------- підключення проєкту
def test_new_project_starts_with_working_policies(client: TestClient, devops: dict):
    """Щойно підключений проєкт має бути одразу придатним до роботи.

    Без політик план завжди вироджується в повний прогін: система нічого не
    робить, і причина цього ззовні невидима. Раніше політики створювали лише
    сценарії імпорту, тож проєкт, доданий через інтерфейс, лишався німим.
    """
    import uuid
    name = f"__test_project_{uuid.uuid4().hex[:8]}"
    r = client.post(f"{API}/projects", headers=devops, json={
        "name": name, "repo_url": "https://example.invalid/x",
        "ci_provider": "gitlab", "default_branch": "main",
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    try:
        pols = client.get(f"{API}/projects/{pid}/policies", headers=devops).json()
        kinds = {p["type"] for p in pols}
        assert {"skip_docs_only", "test_selection", "sharding", "fail_fast"} <= kinds, kinds

        sel = next(p for p in pols if p["type"] == "test_selection")
        assert sel["params"]["protect_default_branch"] is True, "основна гілка має бути захищена"
        assert "feature/*" in sel["branch_pattern"]
        assert 0 < sel["params"]["min_keep_ratio"] < 1
    finally:
        db = SessionLocal()
        obj = db.get(Project, pid)
        if obj is not None:
            db.delete(obj)
            db.commit()
        db.close()
