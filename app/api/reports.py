"""Звіт за період і розсилання зведення (варіант використання «Сформувати звіт за період»)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_lead_or_devops
from app.db import get_db
from app.models import Project, User
from app.services import reports
from app.services.notification import get_service

router = APIRouter(prefix="/projects/{project_id}", tags=["Звіти"])

MAX_PERIOD_DAYS = 366


def _period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """Межі звіту; без дат - останні тридцять днів до сьогодні включно."""
    today = datetime.now(timezone.utc).date()
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=29))
    if date_from > date_to:
        raise HTTPException(422,
                            "The period starts after it ends")
    if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(422,
                            f"The period cannot be longer than {MAX_PERIOD_DAYS} days")
    return date_from, date_to


@router.get("/report", summary="Звіт про роботу конвеєрів за довільний період")
def period_report(
    project: Project = Depends(get_project),
    date_from: date | None = Query(None, description="Перший день періоду, РРРР-ММ-ДД"),
    date_to: date | None = Query(None, description="Останній день періоду, РРРР-ММ-ДД"),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Показники за інтервал, обидві дати включно.

    Звіт доступний усім ролям: він лише читає дані. У форматі csv відповідь
    призначена для збереження у файл і відкриття в табличному редакторі.
    """
    date_from, date_to = _period(date_from, date_to)
    report = reports.build_report(db, project, date_from, date_to)
    if format == "csv":
        name = f"slipstream-{project.id}-{date_from.isoformat()}-{date_to.isoformat()}.csv"
        return Response(
            content=reports.report_csv(report), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return report


@router.post("/report/send", summary="Надіслати зведення за період налаштованими каналами")
def send_digest(
    project: Project = Depends(get_project),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_lead_or_devops),
):
    """Розсилає зведення тими самими каналами, що й сповіщення про аномалії.

    Повертає результат за кожним каналом: керівник має бачити, чи дійшов
    лист, а не дізнаватися про відмову поштового сервера від одержувача.
    """
    date_from, date_to = _period(date_from, date_to)
    report = reports.build_report(db, project, date_from, date_to)
    pl, opt, dora = report["pipelines"], report["optimisation"], report["dora"]
    delivered = get_service().digest(
        project.name,
        {
            "total": pl["total"],
            "success_rate": pl["success_rate"],
            "mean_duration_s": pl["mean_duration_s"],
            "optimized_share": opt["optimized_share"],
            "Time saved": f"{opt['saved_hours']} h",
            "Deployments": dora["deployments"],
            "Change failure rate": f"{dora['change_failure_rate']:.1%}",
        },
        period=f"{date_from.isoformat()} .. {date_to.isoformat()}",
    )
    return {"period": report["period"], "channels": delivered}
