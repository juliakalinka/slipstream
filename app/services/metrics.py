"""Метрики ефективності конвеєрів, зокрема чотири метрики DORA.

DORA (DevOps Research and Assessment) визначає чотири ключові показники
продуктивності команди доставки:
  * частота розгортань;
  * час від коміту до продакшену (lead time for changes);
  * частка невдалих змін (change failure rate);
  * час відновлення після збою (time to restore service).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Commit, Job, Pipeline, RunStatus, TestCase, TestResult, TestStatus


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class DoraMetrics:
    deployment_frequency_per_day: float
    lead_time_hours: float
    change_failure_rate: float
    time_to_restore_hours: float
    window_days: int
    deployments: int


@dataclass
class PipelineMetrics:
    total: int
    success: int
    failed: int
    success_rate: float
    mean_duration_s: float
    median_duration_s: float
    p95_duration_s: float
    mean_queue_s: float
    optimized_share: float
    # Скільки прогонів мають збережений план і яку економію він обіцяв.
    # У режимі холостого ходу це єдиний спосіб побачити користь ДО ввімкнення:
    # частка optimized_share там завжди нульова за визначенням.
    planned_share: float = 0.0
    potential_saving: float = 0.0
    trend: list[dict] = field(default_factory=list)


def _window(days: int, since: datetime | None, until: datetime | None) -> tuple[datetime, datetime | None]:
    """Межі періоду: або останні `days` днів, або явно заданий інтервал.

    Дашборд рахує від поточного моменту назад, звіт за період - між двома
    датами, які обрав користувач. Обчислення те саме, тому межі зведено в
    одне місце, а не продубльовано в кожній функції.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
    return since, until


def pipeline_metrics(
    db: Session, project_id: int, days: int = 30,
    since: datetime | None = None, until: datetime | None = None,
) -> PipelineMetrics:
    since, until = _window(days, since, until)
    query = (
        select(Pipeline)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.started_at >= since)
        .order_by(Pipeline.started_at)
    )
    if until is not None:
        query = query.where(Pipeline.started_at < until)
    rows = db.scalars(query).all()
    if not rows:
        return PipelineMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [])

    # Нульова тривалість означає, що сервер CI не повідомив часу завершення
    # (надійшли лише звіти перевірок). Такий прогін рахується в кількості й
    # частці успішних, але не в статистиці тривалості.
    durations = sorted(float(p.duration_s) for p in rows if p.duration_s > 0) or [0.0]
    success = sum(1 for p in rows if p.status == RunStatus.SUCCESS)
    failed = sum(1 for p in rows if p.status == RunStatus.FAILED)
    p95_idx = max(0, int(len(durations) * 0.95) - 1)

    # тренд за днями
    by_day: dict[str, list[Pipeline]] = {}
    for p in rows:
        key = _aware(p.started_at).strftime("%Y-%m-%d")
        by_day.setdefault(key, []).append(p)
    trend = [
        {
            "date": d,
            "count": len(v),
            "mean_duration_s": round(statistics.fmean(
                [float(x.duration_s) for x in v if x.duration_s > 0] or [0.0]), 1),
            "success_rate": round(sum(1 for x in v if x.status == RunStatus.SUCCESS) / len(v), 3),
            "success": sum(1 for x in v if x.status == RunStatus.SUCCESS),
            "failed": sum(1 for x in v if x.status == RunStatus.FAILED),
            "optimized": sum(1 for x in v if x.optimized),
        }
        for d, v in sorted(by_day.items())
    ]

    planned = [p for p in rows if (p.plan_snapshot or {}).get("saving_ratio") is not None]
    savings = [float(p.plan_snapshot["saving_ratio"]) for p in planned]

    return PipelineMetrics(
        total=len(rows),
        success=success,
        failed=failed,
        success_rate=round(success / len(rows), 4),
        mean_duration_s=round(statistics.fmean(durations), 1),
        median_duration_s=round(statistics.median(durations), 1),
        p95_duration_s=round(durations[p95_idx], 1),
        mean_queue_s=round(statistics.fmean(float(p.queue_time_s) for p in rows), 1),
        optimized_share=round(sum(1 for p in rows if p.optimized) / len(rows), 4),
        planned_share=round(len(planned) / len(rows), 4),
        potential_saving=round(statistics.fmean(savings), 4) if savings else 0.0,
        trend=trend,
    )


def dora_metrics(
    db: Session, project_id: int, days: int = 30, default_branch: str = "main",
    since: datetime | None = None, until: datetime | None = None,
) -> DoraMetrics:
    since, until = _window(days, since, until)
    query = (
        select(Pipeline, Commit)
        .join(Commit, Pipeline.commit_sha == Commit.sha)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.started_at >= since)
        .where(Pipeline.ref == default_branch)
        .order_by(Pipeline.started_at)
    )
    if until is not None:
        query = query.where(Pipeline.started_at < until)
    rows = db.execute(query).all()

    if not rows:
        return DoraMetrics(0.0, 0.0, 0.0, 0.0, days, 0)

    # розгортанням вважаємо успішний конвеєр основної гілки
    deploys = [(p, c) for p, c in rows if p.status == RunStatus.SUCCESS]
    if until is not None:
        days = max(1, round((until - since).total_seconds() / 86400))
    freq = len(deploys) / days

    lead_times = []
    for p, c in deploys:
        fin, com = _aware(p.finished_at), _aware(c.committed_at)
        if fin and com:
            lead_times.append((fin - com).total_seconds() / 3600)

    failed = [p for p, _ in rows if p.status == RunStatus.FAILED]
    cfr = len(failed) / len(rows)

    # час відновлення: від невдалого конвеєра до наступного успішного
    restore = []
    ordered = [p for p, _ in rows]
    for i, p in enumerate(ordered):
        if p.status != RunStatus.FAILED:
            continue
        for nxt in ordered[i + 1:]:
            if nxt.status == RunStatus.SUCCESS:
                a, b = _aware(p.finished_at), _aware(nxt.finished_at)
                if a and b:
                    restore.append((b - a).total_seconds() / 3600)
                break

    return DoraMetrics(
        deployment_frequency_per_day=round(freq, 3),
        lead_time_hours=round(statistics.fmean(lead_times), 2) if lead_times else 0.0,
        change_failure_rate=round(cfr, 4),
        time_to_restore_hours=round(statistics.median(restore), 2) if restore else 0.0,
        window_days=days,
        deployments=len(deploys),
    )


def last_run_at(db: Session, project_id: int) -> datetime | None:
    """Момент початку найсвіжішого прогону проєкту, або None, якщо їх немає."""
    return _aware(db.scalar(
        select(func.max(Pipeline.started_at)).where(Pipeline.project_id == project_id)
    ))


def slowest_jobs(
    db: Session, project_id: int, days: int = 30, limit: int = 10,
    since: datetime | None = None, until: datetime | None = None,
) -> list[dict]:
    since, until = _window(days, since, until)
    query = (
        select(Job.name, func.avg(Job.duration_s), func.count(Job.id), func.max(Job.duration_s))
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.started_at >= since)
        .group_by(Job.name)
        .order_by(func.avg(Job.duration_s).desc())
        .limit(limit)
    )
    if until is not None:
        query = query.where(Pipeline.started_at < until)
    rows = db.execute(query).all()
    return [
        {"name": n, "mean_duration_s": round(float(a), 1), "runs": int(c), "max_duration_s": int(m)}
        for n, a, c, m in rows
    ]


def flakiest_tests(db: Session, project_id: int, limit: int = 10) -> list[dict]:
    """Нестабільні тести, а також тести, залежні від порядку виконання.

    Другі потрапляють у перелік навіть із нульовою оцінкою нестабільності:
    для планувальника вони не менш важливі, бо забороняють переставляння.
    """
    from sqlalchemy import or_

    rows = db.scalars(
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .where(or_(TestCase.flaky_score > 0, TestCase.order_dependent.is_(True)))
        # Сторінка присвячена саме нестабільності, тому первинне сортування
        # за оцінкою: залежність від порядку є додатковою ознакою, а не темою.
        .order_by(TestCase.flaky_score.desc(), TestCase.order_dependent.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": t.id, "suite": t.suite, "name": t.name,
            "flaky_score": round(t.flaky_score, 3),
            "fail_rate": round(t.fail_rate, 3),
            "runs": t.total_runs, "quarantined": t.quarantined,
            "order_dependent": bool(t.order_dependent),
            "flaky_category": t.flaky_category or "",
        }
        for t in rows
    ]
