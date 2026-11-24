"""Повідомлення про хибні спрацювання (варіант використання «Повідомити про проблему»)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_devops
from app.db import get_db
from app.models import Alert, Pipeline, ProblemReport, Project, Recommendation, TestCase, User
from app.schemas import ProblemReportIn, ProblemReportOut

router = APIRouter(prefix="/projects/{project_id}", tags=["Повідомлення про проблеми"])

# Таблиця, у якій шукати предмет повідомлення, щоб не прийняти посилання
# на запис чужого проєкту.
_SUBJECTS = {
    "alert": Alert, "pipeline": Pipeline,
    "recommendation": Recommendation, "test_case": TestCase,
}


def _out(report: ProblemReport, db: Session) -> ProblemReportOut:
    author = db.get(User, report.user_id) if report.user_id else None
    out = ProblemReportOut.model_validate(report)
    out.reported_by = author.email if author else ""
    return out


@router.post("/problems", response_model=ProblemReportOut, status_code=201,
             summary="Повідомити про хибне спрацювання системи")
def report_problem(
    body: ProblemReportIn,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Доступно всім ролям: помилку системи найчастіше помічає розробник.

    Повідомлення нічого не змінює в роботі системи саме по собі - воно лише
    зберігає оцінку людини, щоб DevOps-інженер розібрав випадок.
    """
    model = _SUBJECTS.get(body.subject_type)
    if model is not None:
        if body.subject_id is None:
            raise HTTPException(422,
                                f"subject_id is required for a {body.subject_type}")
        subject = db.get(model, body.subject_id)
        if subject is None or subject.project_id != project.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"{body.subject_type} {body.subject_id} not found in this project")
    report = ProblemReport(
        project_id=project.id, user_id=user.id, subject_type=body.subject_type,
        subject_id=body.subject_id, note=body.note.strip(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return _out(report, db)


@router.get("/problems", response_model=list[ProblemReportOut],
            summary="Повідомлення про проблеми в проєкті")
def list_problems(
    project: Project = Depends(get_project),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.scalars(
        select(ProblemReport)
        .where(ProblemReport.project_id == project.id)
        .order_by(ProblemReport.created_at.desc())
        .limit(limit)
    ).all()
    return [_out(r, db) for r in rows]


@router.put("/problems/{report_id}/resolve", response_model=ProblemReportOut,
            summary="Позначити повідомлення розглянутим")
def resolve_problem(
    report_id: int,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    report = db.get(ProblemReport, report_id)
    if report is None or report.project_id != project.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Problem report not found")
    report.resolved = True
    report.resolved_by = user.id
    report.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    return _out(report, db)
