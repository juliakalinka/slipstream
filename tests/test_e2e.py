"""Наскрізний тест системи: від входу до застосування рекомендації.

Відтворює той самий сценарій, який демонструється на захисті.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Commit, Project

API = "/api/v1"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    r = client.post(f"{API}/auth/login", json={"email": "devops@example.com", "password": "devops123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_health(client: TestClient):
    assert client.get("/health").json()["status"] == "ok"


def test_login_rejects_bad_password(client: TestClient):
    r = client.post(f"{API}/auth/login", json={"email": "devops@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_me(client: TestClient, auth: dict):
    r = client.get(f"{API}/auth/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["role"] == "devops"


def test_requires_auth(client: TestClient):
    assert client.get(f"{API}/projects").status_code == 401


def test_projects(client: TestClient, auth: dict):
    r = client.get(f"{API}/projects", headers=auth)
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_dashboard(client: TestClient, auth: dict):
    r = client.get(f"{API}/projects/1/dashboard?days=200", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["pipelines"]["total"] > 0
    assert 0.0 <= d["pipelines"]["success_rate"] <= 1.0
    assert set(d["dora"]) >= {
        "deployment_frequency_per_day", "lead_time_hours",
        "change_failure_rate", "time_to_restore_hours",
    }


def test_plan_selects_subset_on_feature_branch(client: TestClient):
    """Головна перевірка: на гілці розробника система відбирає підмножину тестів."""
    body = {
        "project_id": 1,
        "commit_sha": "e2e" + "0" * 37,
        "ref": "feature/task-999",
        "author": "test.user",
        "message": "feat: зміна в модулі авторизації",
        "changed_paths": ["src/auth/service_3.py", "src/auth/handlers_7.py"],
        "additions": 48,
        "deletions": 11,
    }
    r = client.post(f"{API}/pipelines/plan", json=body)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["mode"] in ("selective", "sharded", "full")
    assert 0 < plan["selected_tests"] <= plan["total_tests"]
    assert plan["shards"] >= 1
    assert plan["rationale"]


def test_plan_skips_docs_only_change(client: TestClient):
    """Зміна лише в документації не потребує тестування."""
    body = {
        "project_id": 1,
        "commit_sha": "doc" + "1" * 37,
        "ref": "feature/docs-update",
        "author": "test.user",
        "message": "docs: оновлення інструкції",
        "changed_paths": ["docs/guide_1.md", "docs/faq_2.md", "README.md"],
        "additions": 30,
        "deletions": 4,
    }
    r = client.post(f"{API}/pipelines/plan", json=body)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["mode"] == "skip"
    assert plan["selected_tests"] == 0


def test_plan_protects_default_branch(client: TestClient):
    """Запобіжник: на основній гілці відбір тестів не застосовується."""
    body = {
        "project_id": 1,
        "commit_sha": "man" + "2" * 37,
        "ref": "main",
        "author": "test.user",
        "message": "fix: критичне виправлення",
        "changed_paths": ["src/billing/service_2.py"],
        "additions": 20,
        "deletions": 5,
    }
    r = client.post(f"{API}/pipelines/plan", json=body)
    assert r.status_code == 200, r.text
    plan = r.json()
    # На основній гілці відбір тестів заборонено: виконується повний набір
    # (за вирахуванням тестів у карантині), і план це явно пояснює.
    assert plan["mode"] != "selective"
    assert plan["fallbacks"], "план має пояснити, чому відбір не застосовано"
    assert any(
        ("protected" in f) or ("does not cover" in f) for f in plan["fallbacks"]
    ), plan["fallbacks"]


def test_plan_gitlab_yaml_format(client: TestClient):
    body = {
        "project_id": 1,
        "commit_sha": "yml" + "3" * 37,
        "ref": "feature/task-998",
        "changed_paths": ["src/api/handlers_2.py"],
        "additions": 12, "deletions": 3,
        "format": "gitlab-yaml",
    }
    r = client.post(f"{API}/pipelines/plan", json=body)
    assert r.status_code == 200
    text = r.text
    assert "stages:" in text
    assert "script:" in text


def test_webhook_ingests_pipeline(client: TestClient):
    ev = {
        "project_id": 1,
        "event": "pipeline_finished",
        "external_id": "e2e-hook-1",
        "commit_sha": "hok" + "4" * 37,
        "ref": "feature/task-997",
        "status": "failed",
        "duration_s": 245,
        "queue_time_s": 12,
        "author": "test.user",
        "changed_paths": ["src/core/models_1.py"],
        "additions": 60, "deletions": 20,
        "jobs": [
            {"name": "build", "stage": "build", "status": "success", "duration_s": 70},
            {"name": "test:shard-1", "stage": "test", "status": "failed", "duration_s": 175},
        ],
        "test_results": [
            {"suite": "core", "name": "test_core_new", "status": "failed",
             "duration_ms": 1300, "job": "test:shard-1"},
            {"suite": "core", "name": "test_core_other", "status": "passed",
             "duration_ms": 800, "job": "test:shard-1"},
        ],
        "optimized": True,
    }
    r = client.post(f"{API}/webhooks/ci", json=ev)
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is True
    pid = r.json()["pipeline_id"]

    d = client.get(f"{API}/pipelines/{pid}", headers={}, )
    assert d.status_code == 401  # читання картки потребує авторизації


def test_recommendation_generation_is_idempotent(client: TestClient, auth: dict):
    """Повторний аналіз не має плодити дублікати вже наявних рекомендацій."""
    first = client.post(f"{API}/projects/1/recommendations/generate", headers=auth)
    assert first.status_code == 200, first.text
    before = len(client.get(f"{API}/projects/1/recommendations", headers=auth).json())

    second = client.post(f"{API}/projects/1/recommendations/generate", headers=auth)
    assert second.status_code == 200
    after = len(client.get(f"{API}/projects/1/recommendations", headers=auth).json())
    assert after == before, "повторний аналіз створив дублікати"


def test_recommendations_flow(client: TestClient, auth: dict):
    """Повний життєвий цикл рекомендації: створення, застосування, захист від повтору.

    Тест не залежить від стану системи: якщо активних рекомендацій немає,
    бо всі вже опрацьовані раніше, він створює власну.
    """
    from app.db import SessionLocal
    from app.models import RecoStatus, RecoType, Recommendation

    client.post(f"{API}/projects/1/recommendations/generate", headers=auth)
    items = client.get(f"{API}/projects/1/recommendations?status=new", headers=auth).json()

    created_here = False
    if not items:
        db = SessionLocal()
        try:
            r = Recommendation(
                project_id=1, type=RecoType.REORDER_JOBS,
                title="Перевірка життєвого циклу рекомендації",
                description="Створено автотестом", evidence={"source": "test"},
                expected_gain_s=42, status=RecoStatus.NEW,
            )
            db.add(r)
            db.commit()
            db.refresh(r)
            items = [{"id": r.id, "type": r.type.value}]
            created_here = True
        finally:
            db.close()

    target = next((i for i in items if i["type"] == "quarantine_flaky"), items[0])

    ap = client.put(f"{API}/recommendations/{target['id']}/apply", headers=auth)
    assert ap.status_code == 200, ap.text
    assert ap.json()["status"] == "applied"

    again = client.put(f"{API}/recommendations/{target['id']}/apply", headers=auth)
    assert again.status_code == 409, "повторне застосування має бути заборонене"

    if created_here:
        db = SessionLocal()
        try:
            db.delete(db.get(Recommendation, target["id"]))
            db.commit()
        finally:
            db.close()


def test_reject_flow(client: TestClient, auth: dict):
    """Відхилення рекомендації так само незворотне."""
    from app.db import SessionLocal
    from app.models import RecoStatus, RecoType, Recommendation

    db = SessionLocal()
    try:
        r = Recommendation(
            project_id=1, type=RecoType.ENABLE_CACHE, title="Перевірка відхилення",
            description="Створено автотестом", evidence={}, expected_gain_s=10,
            status=RecoStatus.NEW,
        )
        db.add(r)
        db.commit()
        db.refresh(r)
        rid = r.id
    finally:
        db.close()

    rj = client.put(f"{API}/recommendations/{rid}/reject", headers=auth)
    assert rj.status_code == 200
    assert rj.json()["status"] == "rejected"

    ap = client.put(f"{API}/recommendations/{rid}/apply", headers=auth)
    assert ap.status_code == 409, "відхилену рекомендацію не можна застосувати"

    db = SessionLocal()
    try:
        db.delete(db.get(Recommendation, rid))
        db.commit()
    finally:
        db.close()


def test_developer_cannot_change_policies(client: TestClient):
    tok = client.post(f"{API}/auth/login",
                      json={"email": "dev@example.com", "password": "dev123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = client.put(f"{API}/projects/1/mode", json={"plan_mode": "active"}, headers=h)
    assert r.status_code == 403


def test_models_listed(client: TestClient, auth: dict):
    r = client.get(f"{API}/projects/1/models", headers=auth)
    assert r.status_code == 200
    versions = r.json()
    assert versions, "моделі мають бути навчені"
    assert any(v["is_active"] for v in versions)


def test_pipeline_detail_exposes_its_project(client: TestClient, auth: dict):
    """Картка прогону має повідомляти, якому проєкту він належить.

    Без цього поля інтерфейс брав проєкт із бічного меню, і план конвеєра
    розраховувався для чужого репозиторію: у картці прогону тривалістю
    6 хвилин з'являвся план на 47 хвилин.
    """
    page = client.get("/api/v1/projects/1/pipelines?limit=1", headers=auth)
    runs = page.json()
    assert runs, "у проєкті 1 немає прогонів"

    # Перелік віддає сторінку, а не всю історію, тому повідомляє, скільки
    # прогонів усього, і вміє знайти прогін за номером або комітом.
    total = int(page.headers["X-Total-Count"])
    assert total >= len(runs)
    second = client.get("/api/v1/projects/1/pipelines?limit=1&offset=1", headers=auth).json()
    assert total == 1 or second[0]["id"] != runs[0]["id"]
    for term in (str(runs[0]["id"]), f"#{runs[0]['id']}", runs[0]["commit_sha"][:10]):
        found = client.get("/api/v1/projects/1/pipelines", params={"q": term}, headers=auth)
        assert runs[0]["id"] in [r["id"] for r in found.json()], term
    nothing = client.get("/api/v1/projects/1/pipelines?q=no-such-run-zzz", headers=auth)
    assert nothing.json() == [] and nothing.headers["X-Total-Count"] == "0"
    only = client.get("/api/v1/projects/1/pipelines?optimized=true&limit=500", headers=auth)
    assert all(r["optimized"] for r in only.json())
    assert int(only.headers["X-Total-Count"]) == len(only.json()) <= total

    detail = client.get(f"/api/v1/pipelines/{runs[0]['id']}", headers=auth)
    assert detail.status_code == 200
    body = detail.json()
    assert body["project_id"] == 1

    # Друга частина. Запит плану і його виконання дочірнім конвеєром показуються як пара.
    # GitLab надсилає дві події: батьківський конвеєр лише запитує план,
    # дочірній виконує перевірки. Подія конвеєра не несе змінених файлів,
    # тому їх має дати запит плану, навіть якщо коміт уже відомий системі.
    sha, ref = "lnk" + "5" * 37, "feature/linked-runs"
    started = {"project_id": 1, "event": "pipeline_started", "external_id": "e2e-parent",
               "commit_sha": sha, "ref": ref, "status": "running"}
    assert client.post(f"{API}/webhooks/ci", json=started).status_code == 202

    paths = ["src/billing/service_1.py"]
    r = client.post(f"{API}/pipelines/plan", json={
        "project_id": 1, "commit_sha": sha, "ref": ref, "changed_paths": paths,
        "additions": 5, "deletions": 1,
    })
    assert r.status_code == 200, r.text

    child = {"project_id": 1, "event": "pipeline_finished", "external_id": "e2e-child",
             "commit_sha": sha, "ref": ref, "status": "success", "duration_s": 30,
             "jobs": [{"name": "test:shard-1", "stage": "test", "status": "success", "duration_s": 25}],
             "test_results": [
                 {"suite": "billing", "name": "test_billing_0", "status": "passed",
                  "duration_ms": 900, "job": "test:shard-1"},
                 {"suite": "billing", "name": "test_billing_1", "status": "failed",
                  "duration_ms": 700, "job": "test:shard-1"},
             ]}
    r = client.post(f"{API}/webhooks/ci", json=child)
    assert r.status_code == 202, r.text
    child_id = r.json()["pipeline_id"]

    d = client.get(f"{API}/pipelines/{child_id}", headers=auth).json()
    assert d["changed_paths"] == paths
    assert d["tests_run"] == 2 and d["tests_failed"] == 1
    assert d["linked_role"] == "plan" and d["plan_from_run"] == d["linked_run_id"]
    assert d["plan_snapshot"].get("mode")
    assert any(s["suite"] == "billing" and s["ran"] == 2 for s in d["checks_by_suite"])
    assert d["tests_total"] >= d["tests_run"]

    parent = client.get(f"{API}/pipelines/{d['linked_run_id']}", headers=auth).json()
    assert parent["linked_role"] == "execution" and parent["linked_run_id"] == child_id
    assert parent["tests_run"] == 0

    rows = client.get(f"{API}/projects/1/pipelines", params={"q": ref}, headers=auth).json()
    by_id = {x["id"]: x for x in rows}
    assert by_id[child_id]["tests_run"] == 2
    assert by_id[parent["id"]]["linked_run_id"] == child_id



def test_a_malformed_commit_sha_is_rejected_as_bad_input(client: TestClient):
    """Хибні вхідні дані не повинні виглядати як поломка сервера.

    Стовпець ідентифікатора коміту має сорок символів. Доки довжина не
    перевірялася, задовге значення доходило до бази, і конвеєр одержував
    500 — тобто «винен сервер» — замість 422 «неправильний запит».
    """
    r = client.post(f"{API}/pipelines/plan", json={
        "project_id": 1, "commit_sha": "x" * 41, "ref": "feature/x",
        "author": "probe", "changed_paths": ["a.py"],
    })
    assert r.status_code == 422, f"очікували відмову через вхідні дані, одержали {r.status_code}"
