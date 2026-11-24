"""Звіт про роботу конвеєрів за довільний інтервал часу.

Дашборд показує ковзне вікно від поточного моменту назад і відповідає на
питання «що відбувається зараз». Керівникові потрібне інше: показники за
конкретний проміжок - спринт, місяць, квартал, - які можна зберегти й
порівняти з попереднім. Звіт рахується тими самими функціями, що й дашборд,
тому числа в обох місцях збігаються за однакових меж.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Alert, Pipeline, Project, RecoStatus, Recommendation, TestCase,
)
from app.services import metrics as svc


def _bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """Інтервал [початок першого дня, початок дня після останнього)."""
    since = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    until = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return since, until


def build_report(db: Session, project: Project, date_from: date, date_to: date) -> dict:
    """Показники проєкту за інтервал, обидві дати включно."""
    since, until = _bounds(date_from, date_to)
    pm = svc.pipeline_metrics(db, project.id, since=since, until=until)
    dora = svc.dora_metrics(db, project.id, default_branch=project.default_branch,
                            since=since, until=until)

    # Заощаджений час рахується лише за прогонами, що справді виконувалися за
    # планом: обіцянка холостого ходу економією не є.
    saved_s = 0.0
    optimized = 0
    rows = db.scalars(
        select(Pipeline)
        .where(Pipeline.project_id == project.id)
        .where(Pipeline.started_at >= since)
        .where(Pipeline.started_at < until)
        .where(Pipeline.optimized.is_(True))
    ).all()
    for p in rows:
        snap = p.plan_snapshot or {}
        optimized += 1
        saved_s += max(0.0, float(snap.get("baseline_duration_s") or 0)
                       - float(snap.get("est_duration_s") or 0))

    def _count(model, *conditions) -> int:
        query = select(func.count(model.id)).where(model.project_id == project.id)
        for cond in conditions:
            query = query.where(cond)
        return int(db.scalar(query) or 0)

    in_period = lambda col: (col >= since, col < until)  # noqa: E731
    return {
        "project": {"id": project.id, "name": project.name, "mode": project.plan_mode.value},
        "period": {
            "from": date_from.isoformat(), "to": date_to.isoformat(),
            "days": (date_to - date_from).days + 1,
        },
        "pipelines": {
            "total": pm.total, "success": pm.success, "failed": pm.failed,
            "success_rate": pm.success_rate,
            "mean_duration_s": pm.mean_duration_s,
            "median_duration_s": pm.median_duration_s,
            "p95_duration_s": pm.p95_duration_s,
            "mean_queue_s": pm.mean_queue_s,
        },
        "optimisation": {
            "optimized_runs": optimized,
            "optimized_share": pm.optimized_share,
            "saved_hours": round(saved_s / 3600, 2),
            "saved_seconds": round(saved_s, 1),
            "planned_share": pm.planned_share,
            "potential_saving": pm.potential_saving,
        },
        "dora": {
            "deployment_frequency_per_day": dora.deployment_frequency_per_day,
            "lead_time_hours": dora.lead_time_hours,
            "change_failure_rate": dora.change_failure_rate,
            "time_to_restore_hours": dora.time_to_restore_hours,
            "deployments": dora.deployments,
        },
        "quality": {
            "alerts_raised": _count(Alert, *in_period(Alert.created_at)),
            "alerts_open": _count(Alert, *in_period(Alert.created_at),
                                  Alert.acknowledged.is_(False)),
            "recommendations_applied": _count(
                Recommendation, *in_period(Recommendation.created_at),
                Recommendation.status == RecoStatus.APPLIED),
            "recommendations_rejected": _count(
                Recommendation, *in_period(Recommendation.created_at),
                Recommendation.status == RecoStatus.REJECTED),
            # каталог перевірок не має історії за датами, тому це поточний стан
            "tests_quarantined": _count(TestCase, TestCase.quarantined.is_(True)),
        },
        "slowest_jobs": svc.slowest_jobs(db, project.id, limit=5, since=since, until=until),
        "daily": pm.trend,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def report_csv(report: dict) -> str:
    """Звіт як таблиця: зведення над рядками за днями.

    Формат розрахований на відкриття в табличному редакторі: спершу блок
    «показник - значення», далі порожній рядок і таблиця за днями.
    """
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    period, pl, opt, dora, quality = (report["period"], report["pipelines"],
                                      report["optimisation"], report["dora"], report["quality"])
    w.writerow(["Project", report["project"]["name"]])
    w.writerow(["Period", f"{period['from']} .. {period['to']}"])
    w.writerow(["Days", period["days"]])
    w.writerow([])
    w.writerow(["Metric", "Value"])
    for label, value in (
        ("Pipeline runs", pl["total"]),
        ("Passed", pl["success"]),
        ("Failed", pl["failed"]),
        ("Success rate", pl["success_rate"]),
        ("Mean duration, s", pl["mean_duration_s"]),
        ("Median duration, s", pl["median_duration_s"]),
        ("95th percentile duration, s", pl["p95_duration_s"]),
        ("Mean queue time, s", pl["mean_queue_s"]),
        ("Optimised runs", opt["optimized_runs"]),
        ("Optimised share", opt["optimized_share"]),
        ("Time saved, hours", opt["saved_hours"]),
        ("Deployment frequency, per day", dora["deployment_frequency_per_day"]),
        ("Lead time for changes, hours", dora["lead_time_hours"]),
        ("Change failure rate", dora["change_failure_rate"]),
        ("Time to restore, hours", dora["time_to_restore_hours"]),
        ("Alerts raised", quality["alerts_raised"]),
        ("Recommendations applied", quality["recommendations_applied"]),
        ("Tests in quarantine", quality["tests_quarantined"]),
    ):
        w.writerow([label, value])
    w.writerow([])
    w.writerow(["Date", "Runs", "Passed", "Failed", "Success rate", "Mean duration, s", "Optimised"])
    for day in report["daily"]:
        w.writerow([day["date"], day["count"], day["success"], day["failed"],
                    day["success_rate"], day["mean_duration_s"], day["optimized"]])
    return out.getvalue()
