"""Чесність відтворення минулого (розділ 4).

Експеримент «базовий конвеєр проти оптимізованого» відтворює рішення системи
так, ніби вона працювала в момент кожного минулого конвеєра. Це має сенс лише
тоді, коли жодна ознака не бачить майбутнього відносно цього моменту.

Таблиця test_cases зберігає підсумки за ВСЮ історію (total_runs,
total_failures, last_failed_at, flaky_score). У бойовому режимі це правильно,
бо «зараз» і є кінець історії. Під час відтворення минулого ці ж поля містять
майбутнє, а частота падінь і давність останнього падіння - найсильніші ознаки
ранжування тестів. Тут перевіряється, що при заданому cutoff агрегати
перераховуються на обрізаній історії.

Тест-побратим - test_no_future_leakage у test_ml_core.py, який перевіряє те
саме для ознак рівня коміту.
"""
from __future__ import annotations

from datetime import timezone

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
# TestResult/TestStatus перейменовано: інакше pytest намагається зібрати їх як тестові класи
from app.models import CIProvider, Job, Pipeline, Project
from app.models import TestResult as ResultRow
from app.models import TestStatus as Status
from app.services.plan_builder import _load_test_stats


@pytest.fixture()
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def project(db) -> Project:
    p = db.scalars(select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)).first()
    assert p is not None, "потрібен синтетичний стенд: scripts/seed_synthetic.py"
    return p


@pytest.fixture()
def midpoint(db, project):
    """Момент приблизно посередині історії проєкту."""
    rows = db.scalars(
        select(Pipeline.started_at)
        .where(Pipeline.project_id == project.id)
        .where(Pipeline.started_at.is_not(None))
        .order_by(Pipeline.started_at)
    ).all()
    assert len(rows) > 40, "замало конвеєрів для перевірки"
    mid = rows[len(rows) // 2]
    return mid if mid.tzinfo else mid.replace(tzinfo=timezone.utc)


def test_cutoff_reduces_test_history(db, project, midpoint):
    """Історія на середині має бути СТРОГО меншою за повну."""
    past, _ = _load_test_stats(db, project.id, midpoint)
    full, _ = _load_test_stats(db, project.id, None)

    past_runs = sum(t.runs for t in past)
    full_runs = sum(t.runs for t in full)
    assert past_runs > 0, "на середині історії вже мають бути прогони"
    assert past_runs < full_runs, (
        f"агрегати не обрізано за часом: {past_runs} запусків проти {full_runs} "
        "за всю історію - ознака fail_rate бачить майбутнє"
    )


def test_no_failure_is_dated_after_cutoff(db, project, midpoint):
    """Жодне «останнє падіння» не може бути з майбутнього."""
    past, _ = _load_test_stats(db, project.id, midpoint)
    for t in past:
        if t.last_failed_at is None:
            continue
        lf = t.last_failed_at if t.last_failed_at.tzinfo else t.last_failed_at.replace(tzinfo=timezone.utc)
        assert lf < midpoint, f"тест {t.test_id}: останнє падіння {lf} пізніше за {midpoint}"


def test_cutoff_aggregates_match_independent_query(db, project, midpoint):
    """Перерахунок збігається з незалежним запитом до результатів тестів."""
    past, _ = _load_test_stats(db, project.id, midpoint)
    by_id = {t.test_id: t for t in past}

    rows = db.execute(
        select(
            ResultRow.test_case_id,
            func.count(ResultRow.id),
            func.sum(func.iif(ResultRow.status == Status.FAILED, 1, 0)),
        )
        .join(Job, ResultRow.job_id == Job.id)
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project.id)
        .where(Pipeline.started_at < midpoint)
        .group_by(ResultRow.test_case_id)
    ).all()
    assert rows, "на середині історії мають бути результати тестів"

    checked = 0
    for tc_id, runs, failures in rows:
        st = by_id.get(tc_id)
        if st is None:
            continue
        assert st.runs == int(runs), f"тест {tc_id}: запусків {st.runs}, очікувано {runs}"
        assert st.failures == int(failures or 0), (
            f"тест {tc_id}: падінь {st.failures}, очікувано {failures}")
        checked += 1
    assert checked > 0, "жодного тесту не звірено"


def test_flakiness_is_not_leaked_into_replay(db, project, midpoint):
    """Оцінка нестабільності рахується по всій історії, тому у відтворенні - нуль."""
    past, _ = _load_test_stats(db, project.id, midpoint)
    assert all(t.flaky_score == 0.0 for t in past), (
        "оцінка нестабільності під час відтворення має бути нульовою: "
        "вона порахована детектором по всій історії, зокрема по майбутньому"
    )


def test_live_mode_keeps_full_history(db, project):
    """Без cutoff нічого не обрізається: у бойовому режимі «зараз» - кінець історії."""
    full, _ = _load_test_stats(db, project.id, None)
    assert sum(t.runs for t in full) > 0
    assert any(t.failures > 0 for t in full), "у повній історії мають бути падіння"
