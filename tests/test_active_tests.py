"""Кандидатами відбору є лише живі тести.

Таблиця test_cases накопичує всі тести, які проєкт колись мав. Набір тестів
змінюється: старі видаляють, перейменовують, переносять. Якщо відбирати
кандидатів з усієї таблиці, бюджет часу витрачається на тести, яких у цьому
складанні не буде, і справді потрібні не проходять відсікання.

На синтетичних даних ця вада непомітна, бо там щоразу виконується той самий
незмінний набір. На реальній історії RTPTorrent вона коштувала більшості
виявлених дефектів: у проєкті 107 тестів, у складанні виконується 44, і лише
чверть відібраних узагалі запускалася.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    CIProvider, Commit, Job, Pipeline, PlanMode, Project, RunStatus, TestCase,
)
from app.models import TestResult as ResultRow
from app.models import TestStatus as Status
from app.services.plan_builder import ACTIVE_WINDOW_BUILDS, _load_test_stats

PROJECT = "тест-живих-тестів"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def stand():
    """Проєкт, у якому один тест перестали запускати задовго до кінця історії."""
    db = SessionLocal()
    old = db.scalars(select(Project).where(Project.name == PROJECT)).first()
    if old is not None:
        db.execute(Commit.__table__.delete().where(Commit.project_id == old.id))
        db.delete(old)
        db.commit()

    project = Project(
        name=PROJECT, repo_url="https://example.invalid/alive",
        ci_provider=CIProvider.SYNTHETIC, default_branch="main",
        plan_mode=PlanMode.DRY_RUN,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    kept = TestCase(project_id=project.id, suite="suite", name="живий",
                    avg_duration_ms=1000.0)
    retired = TestCase(project_id=project.id, suite="suite", name="знятий",
                       avg_duration_ms=1000.0)
    db.add_all([kept, retired])
    db.commit()

    total = ACTIVE_WINDOW_BUILDS + 20        # свідомо більше за вікно
    stops_after = 5                          # «знятий» зникає майже одразу
    for i in range(total):
        sha = f"{i:040x}"
        db.add(Commit(
            sha=sha, project_id=project.id, author="tester", message="зміна",
            branch="main", files_changed=1, additions=5, deletions=1,
            changed_paths=["src/core/a.py"], committed_at=START + timedelta(hours=i),
        ))
        pipe = Pipeline(
            project_id=project.id, commit_sha=sha, external_id=str(i), ref="main",
            status=RunStatus.SUCCESS, started_at=START + timedelta(hours=i),
            finished_at=START + timedelta(hours=i, minutes=5), duration_s=300,
        )
        db.add(pipe)
        db.flush()
        job = Job(pipeline_id=pipe.id, name="test", stage="test",
                  status=RunStatus.SUCCESS, duration_s=300)
        db.add(job)
        db.flush()

        running = [kept] if i >= stops_after else [kept, retired]
        for tc in running:
            db.add(ResultRow(job_id=job.id, test_case_id=tc.id,
                             status=Status.PASSED, duration_ms=1000))
    db.commit()

    yield db, project, kept.id, retired.id

    db.execute(Commit.__table__.delete().where(Commit.project_id == project.id))
    db.delete(project)
    db.commit()
    db.close()


def test_retired_test_is_not_a_candidate(stand):
    """Тест, який давно не запускався, не має потрапляти в кандидати."""
    db, project, kept_id, retired_id = stand
    stats, _ = _load_test_stats(db, project.id, None)
    ids = {t.test_id for t in stats}
    assert kept_id in ids, "живий тест зник із кандидатів"
    assert retired_id not in ids, (
        "знятий тест лишився кандидатом: бюджет часу витрачався б на тест, "
        "якого в складанні не буде"
    )


def test_retired_test_is_a_candidate_while_still_running(stand):
    """До моменту зняття тест є повноцінним кандидатом."""
    db, project, kept_id, retired_id = stand
    cutoff = START + timedelta(hours=4)      # «знятий» ще виконується
    stats, _ = _load_test_stats(db, project.id, cutoff)
    ids = {t.test_id for t in stats}
    assert retired_id in ids, "тест відкинуто ще до того, як його перестали запускати"


def test_window_is_not_empty_for_a_fresh_project(stand):
    """Запобіжник: на початку історії система не має лишитися без тестів."""
    db, project, kept_id, _ = stand
    cutoff = START + timedelta(hours=1)
    stats, _ = _load_test_stats(db, project.id, cutoff)
    assert stats, "на початку історії кандидатів не лишилося"
