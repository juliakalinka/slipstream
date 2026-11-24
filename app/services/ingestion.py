"""Приймання та збереження подій CI-серверів.

Вебхук треба прийняти швидко, тому обробник лише перевіряє й зберігає дані.
Важкі операції (перерахунок оцінок нестабільності, перевірка аномалій,
формування рекомендацій) виносяться у фонову задачу.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ml.anomaly import check_pipeline
from app.models import (
    Alert, Commit, Job, Pipeline, Project, RunStatus, TestCase, TestResult, TestStatus,
)
from app.services.notification import notify_alert
from app.schemas import WebhookEvent


def upsert_commit(db: Session, project_id: int, ev: WebhookEvent) -> Commit:
    commit = db.get(Commit, ev.commit_sha)
    if commit is None:
        commit = Commit(
            sha=ev.commit_sha, project_id=project_id, author=ev.author or "unknown",
            message=ev.message, branch=ev.ref, files_changed=len(ev.changed_paths),
            additions=ev.additions, deletions=ev.deletions,
            changed_paths=ev.changed_paths, committed_at=datetime.now(timezone.utc),
        )
        db.add(commit)
        db.flush()
    elif ev.changed_paths and not commit.changed_paths:
        commit.changed_paths = ev.changed_paths
        commit.files_changed = len(ev.changed_paths)
        commit.additions = ev.additions
        commit.deletions = ev.deletions
    return commit


def _test_case(db: Session, project_id: int, suite: str, name: str, file_path: str = "") -> TestCase:
    tc = db.scalars(
        select(TestCase).where(
            TestCase.project_id == project_id, TestCase.suite == suite, TestCase.name == name
        )
    ).first()
    if tc is None:
        tc = TestCase(project_id=project_id, suite=suite, name=name, file_path=file_path)
        db.add(tc)
        db.flush()
    return tc


def ingest(db: Session, ev: WebhookEvent) -> Pipeline:
    """Зберегти подію конвеєра разом із задачами та результатами тестів."""
    upsert_commit(db, ev.project_id, ev)

    pipeline = db.scalars(
        select(Pipeline).where(
            Pipeline.project_id == ev.project_id, Pipeline.external_id == ev.external_id
        )
    ).first()

    now = datetime.now(timezone.utc)
    if pipeline is None:
        pipeline = Pipeline(
            project_id=ev.project_id, commit_sha=ev.commit_sha, external_id=ev.external_id,
            ref=ev.ref, status=ev.status, queued_at=now, started_at=now, optimized=ev.optimized,
        )
        db.add(pipeline)
        db.flush()

    pipeline.status = ev.status
    pipeline.optimized = ev.optimized or pipeline.optimized
    if ev.event == "pipeline_finished":
        pipeline.duration_s = ev.duration_s
        pipeline.queue_time_s = ev.queue_time_s
        pipeline.finished_at = now
        if pipeline.started_at is None:
            pipeline.started_at = now - timedelta(seconds=ev.duration_s)

    # --- задачі
    for j in ev.jobs:
        name = j.get("name", "job")
        job = db.scalars(
            select(Job).where(Job.pipeline_id == pipeline.id, Job.name == name)
        ).first()
        if job is None:
            job = Job(pipeline_id=pipeline.id, name=name)
            db.add(job)
            db.flush()
        job.stage = j.get("stage", job.stage or "test")
        job.status = RunStatus(j.get("status", "success"))
        job.duration_s = int(j.get("duration_s", 0))
        job.runner = j.get("runner", job.runner)
        job.cache_hit = bool(j.get("cache_hit", False))
        if j.get("shard_index") is not None:
            job.shard_index = int(j["shard_index"])
        if j.get("pred_duration_s") is not None:
            job.pred_duration_s = float(j["pred_duration_s"])

    # --- результати тестів
    default_job = None
    for tr in ev.test_results:
        suite = tr.get("suite", "default")
        name = tr.get("name", "unknown")
        job_name = tr.get("job", f"test:{suite}")
        job = db.scalars(
            select(Job).where(Job.pipeline_id == pipeline.id, Job.name == job_name)
        ).first()
        if job is None:
            if default_job is None:
                default_job = Job(pipeline_id=pipeline.id, name=job_name, stage="test",
                                  status=RunStatus.SUCCESS)
                db.add(default_job)
                db.flush()
            job = default_job

        tc = _test_case(db, ev.project_id, suite, name, tr.get("file_path", ""))
        status = TestStatus(tr.get("status", "passed"))

        # ІДЕМПОТЕНТНІСТЬ. GitLab і GitHub повторюють вебхук, якщо не дочекалися
        # відповіді. Без цієї перевірки повтор додав би ще один комплект
        # результатів, завищив total_runs і спотворив частоту падінь - тобто
        # зіпсував би саме ті дані, на яких навчаються моделі.
        existing = db.scalars(
            select(TestResult).where(
                TestResult.job_id == job.id, TestResult.test_case_id == tc.id
            )
        ).first()
        if existing is not None:
            existing.status = status
            existing.duration_ms = int(tr.get("duration_ms", 0))
            existing.retries = int(tr.get("retries", 0))
            existing.error_message = tr.get("error", "")[:2000]
            continue

        db.add(TestResult(
            job_id=job.id, test_case_id=tc.id, status=status,
            duration_ms=int(tr.get("duration_ms", 0)), retries=int(tr.get("retries", 0)),
            error_message=tr.get("error", "")[:2000],
        ))
        tc.total_runs += 1
        if status == TestStatus.FAILED:
            tc.total_failures += 1
            tc.last_failed_at = now
        # ковзне середнє тривалості тесту
        d = float(tr.get("duration_ms", 0))
        if d > 0:
            tc.avg_duration_ms = d if tc.avg_duration_ms == 0 else 0.9 * tc.avg_duration_ms + 0.1 * d

    db.commit()
    db.refresh(pipeline)
    return pipeline


def check_for_anomaly(db: Session, pipeline: Pipeline) -> Alert | None:
    """Порівняти завершений конвеєр із історією і за потреби створити сповіщення."""
    if pipeline.status == RunStatus.RUNNING or pipeline.duration_s <= 0:
        return None

    history = db.scalars(
        select(Pipeline)
        .where(Pipeline.project_id == pipeline.project_id)
        .where(Pipeline.id != pipeline.id)
        .where(Pipeline.duration_s > 0)
        .order_by(Pipeline.started_at.desc())
        .limit(60)
    ).all()
    if len(history) < 12:
        return None

    jobs = db.scalars(select(Job).where(Job.pipeline_id == pipeline.id)).all()
    n_failed = sum(1 for j in jobs if j.status == RunStatus.FAILED)
    verdict = check_pipeline(
        features={
            "duration_s": float(pipeline.duration_s),
            "queue_time_s": float(pipeline.queue_time_s),
            "n_jobs": float(len(jobs)),
            "fail_ratio": (n_failed / len(jobs)) if jobs else 0.0,
            "n_tests": 1.0,
        },
        duration_history=[float(p.duration_s) for p in history],
        queue_history=[float(p.queue_time_s) for p in history],
    )
    if not verdict.is_anomaly:
        return None

    alert = Alert(
        project_id=pipeline.project_id, pipeline_id=pipeline.id, kind=verdict.kind,
        severity=verdict.severity, message=verdict.message, details=verdict.details,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    project = db.get(Project, pipeline.project_id)
    notify_alert(alert, project.name if project else "")
    return alert
