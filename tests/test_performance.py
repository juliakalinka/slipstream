"""Перевірка нефункціональних вимог до швидкодії.

Обґрунтування порогів. Поки система рахує план, конвеєр СТОЇТЬ і чекає.
Затримка додається до кожної збірки, тому вона має бути помітно меншою за
виграш від оптимізації. У підрозділі 2.4 записки задано вимогу: відповідь на
запит плану не довша за кілька секунд.
"""
from __future__ import annotations

import os
import statistics
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import CIProvider, Commit, Project

API = "/api/v1"

# Порогові значення
PLAN_P95_BUDGET_S = 3.0      # 95-й перцентиль часу відповіді на запит плану
PLAN_MAX_BUDGET_S = 6.0      # жодна відповідь не має бути повільнішою
DASHBOARD_BUDGET_S = 4.0     # дашборд відкриває людина, поріг м'якший


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    r = client.post(f"{API}/auth/login",
                    json={"email": "devops@example.com", "password": "devops123"})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def synthetic_project_id() -> int:
    db = SessionLocal()
    try:
        p = db.scalars(select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)).first()
        if p is None:
            pytest.skip("немає синтетичного стенда")
        return p.id
    finally:
        db.close()


# Кількість запитів у серії. Типові значення тримають набір швидким; для
# таблиці вимірювань у записці серія подовжується змінною PERF_SAMPLES.
SAMPLES = int(os.environ.get("PERF_SAMPLES", "0"))


def measure(fn, n: int = 12) -> dict:
    n = SAMPLES or n
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    times.sort()
    return {
        "median": statistics.median(times),
        "p95": times[max(0, int(len(times) * 0.95) - 1)],
        "max": times[-1],
        "n": n,
    }


def test_plan_endpoint_latency(client: TestClient, synthetic_project_id: int):
    """Головна вимога: конвеєр не має помітно чекати на план."""
    counter = {"i": 0}

    def call():
        counter["i"] += 1
        body = {
            "project_id": synthetic_project_id,
            "commit_sha": f"perf{counter['i']:036d}",
            "ref": "feature/perf-test",
            "author": "perf",
            "changed_paths": ["src/auth/service_1.py", "src/api/handlers_3.py"],
            "additions": 45, "deletions": 12,
        }
        r = client.post(f"{API}/pipelines/plan", json=body)
        assert r.status_code == 200, r.text

    m = measure(call, n=12)
    print(f"\n  план: медіана {m['median'] * 1000:.0f} мс, "
          f"95-й перцентиль {m['p95'] * 1000:.0f} мс, максимум {m['max'] * 1000:.0f} мс")

    assert m["p95"] < PLAN_P95_BUDGET_S, (
        f"95-й перцентиль {m['p95']:.2f} с перевищує бюджет {PLAN_P95_BUDGET_S} с: "
        f"конвеєр чекатиме надто довго"
    )
    assert m["max"] < PLAN_MAX_BUDGET_S


def test_dashboard_latency(client: TestClient, token: str, synthetic_project_id: int):
    h = {"Authorization": f"Bearer {token}"}

    def call():
        r = client.get(f"{API}/projects/{synthetic_project_id}/dashboard?days=90", headers=h)
        assert r.status_code == 200

    m = measure(call, n=6)
    print(f"  дашборд: медіана {m['median'] * 1000:.0f} мс, "
          f"95-й перцентиль {m['p95'] * 1000:.0f} мс, максимум {m['max'] * 1000:.0f} мс")
    assert m["p95"] < DASHBOARD_BUDGET_S


def test_webhook_accepts_quickly(client: TestClient, synthetic_project_id: int):
    """Вебхук має відповідати одразу: важка обробка йде у фон."""
    counter = {"i": 0}

    def call():
        counter["i"] += 1
        ev = {
            "project_id": synthetic_project_id,
            "event": "pipeline_started",
            "external_id": f"perf-hook-{counter['i']}",
            "commit_sha": f"hook{counter['i']:036d}",
            "ref": "feature/perf",
            "status": "running",
        }
        r = client.post(f"{API}/webhooks/ci", json=ev)
        assert r.status_code == 202

    m = measure(call, n=10)
    print(f"  вебхук: медіана {m['median'] * 1000:.0f} мс, "
          f"95-й перцентиль {m['p95'] * 1000:.0f} мс, максимум {m['max'] * 1000:.0f} мс")
    assert m["p95"] < 1.5, "приймання події має бути майже миттєвим"


def test_plan_scales_with_history_size(synthetic_project_id: int):
    """Побудова плану на повній історії має лишатися в межах секунд.

    Вимірюється тричі. Перше — зріз історії на половині: він належить
    відтворенню минулого (розділ 4) і навмисно робить більше роботи, бо
    перераховує підсумки окремих перевірок станом на момент зрізу; тут він
    наведений для порівняння масштабу, а не як взірець. Друге — повна
    історія з ПОРОЖНЬОГО кешу: саме так виглядає найгірший випадок у
    бойовому режимі, перший запит після появи нового прогону. Третє — те
    саме з теплим кешем: так виглядають усі решта запитів.
    """
    from datetime import timezone

    from app.ml.features import clear_history_cache
    from app.ml.training import _ordered_pipelines
    from app.services.plan_builder import build_plan

    db = SessionLocal()
    try:
        project = db.get(Project, synthetic_project_id)
        rows = _ordered_pipelines(db, project.id)
        if len(rows) < 100:
            pytest.skip("замало історії для перевірки масштабування")

        commit = db.scalars(
            select(Commit).where(Commit.project_id == project.id)
            .order_by(Commit.committed_at.desc())
        ).first()

        half = rows[len(rows) // 2][0].started_at
        if half.tzinfo is None:
            half = half.replace(tzinfo=timezone.utc)

        clear_history_cache()
        t0 = time.perf_counter()
        build_plan(db, project, commit, ref="feature/scale", cutoff=half)
        t_half = time.perf_counter() - t0

        clear_history_cache()
        t0 = time.perf_counter()
        build_plan(db, project, commit, ref="feature/scale")
        t_cold = time.perf_counter() - t0

        t0 = time.perf_counter()
        build_plan(db, project, commit, ref="feature/scale")
        t_warm = time.perf_counter() - t0

        print(f"  побудова плану: половина історії {t_half * 1000:.0f} мс, "
              f"повна з порожнього кешу {t_cold * 1000:.0f} мс, "
              f"повна з кешем {t_warm * 1000:.0f} мс")
        assert t_cold < 4.0, "побудова плану на повній історії надто повільна"
        assert t_warm < t_cold, "кеш історії не дає виграшу"
    finally:
        db.close()
