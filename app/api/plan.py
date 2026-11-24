"""Головний ендпоінт системи: видача оптимізованого плану конвеєра.

Саме його викликає службова задача з CI-сервера. Від швидкості й надійності
цієї відповіді залежить, чи можна взагалі вмикати систему в бойовому режимі:
конвеєр стоїть і чекає.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project
from app.db import get_db
from app.models import Commit, Pipeline, PlanMode, Project, User
from app.schemas import PlanRequest
from app.services.ingestion import upsert_commit
from app.services.plan_builder import build_plan
from app.services.predictions import save_plan_predictions, save_plan_snapshot

logger = logging.getLogger("cicd.plan")


def _latest_pipeline(db: Session, project_id: int, sha: str, ref: str) -> Pipeline | None:
    """Прогін, для якого запитано план: найновіший за цим комітом і гілкою."""
    return db.scalars(
        select(Pipeline)
        .where(Pipeline.project_id == project_id, Pipeline.commit_sha == sha, Pipeline.ref == ref)
        .order_by(Pipeline.id.desc())
        .limit(1)
    ).first()


router = APIRouter(prefix="/pipelines", tags=["План конвеєра"])


@router.post("/plan", summary="Отримати оптимізований план конвеєра")
def make_plan(body: PlanRequest, db: Session = Depends(get_db)):
    """Повертає план у форматі JSON або як опис дочірнього конвеєра GitLab CI.

    Ендпоінт навмисно не вимагає авторизації користувача: його викликає CI-раннер
    за токеном проєкту. У бойовому середовищі тут перевіряється підпис запиту.
    """
    project = db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    # Коміт міг з'явитися раніше з події конвеєра GitLab, а вона не несе
    # переліку змінених файлів. Тоді перелік береться із запиту плану: без
    # нього відбір не знав би, які модулі зачеплено.
    commit = db.get(Commit, body.commit_sha)
    if commit is None or (body.changed_paths and not commit.changed_paths):
        from app.schemas import WebhookEvent
        ev = WebhookEvent(
            project_id=project.id, event="pipeline_started", external_id="plan-request",
            commit_sha=body.commit_sha, ref=body.ref, author=body.author,
            message=body.message, changed_paths=body.changed_paths,
            additions=body.additions, deletions=body.deletions,
        )
        commit = upsert_commit(db, project.id, ev)
        db.commit()

    try:
        plan = build_plan(db, project, commit, ref=body.ref)
    except Exception as exc:
        # Відмовостійкість: будь-яка помилка планувальника не має зупиняти
        # конвеєр. Повертаємо ознаку повного прогону і пояснення.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Planner unavailable, run the full test suite: {exc}",
        )

    # Прогнози зберігаються, щоб згодом їх можна було зіставити з фактом.
    # Збій запису не повинен впливати на відповідь: конвеєр чекає на план.
    pipeline = _latest_pipeline(db, project.id, commit.sha, body.ref)
    if pipeline is not None:
        try:
            save_plan_snapshot(db, pipeline, plan)
            # Позначка «оптимізований» ставиться в момент видачі плану, якщо
            # він справді застосовується. Покладатися на те, що її проставить
            # CI-сервер, не можна: для цього конвеєр мусив би повернути
            # спеціальну змінну, і за її відсутності показник частки
            # оптимізованих прогонів лишався б нульовим навіть у бойовому
            # режимі. Присвоєння стоїть ДО збереження прогнозів, бо саме воно
            # фіксує зміни; інакше позначка лишилася б незафіксованою.
            if project.plan_mode == PlanMode.ACTIVE and plan.mode != "full":
                pipeline.optimized = True
            save_plan_predictions(db, pipeline, plan)
        except Exception as exc:
            logger.warning("Не вдалося зберегти прогнози для прогону %s: %s", pipeline.id, exc)
            db.rollback()

    if body.format == "gitlab-yaml":
        return PlainTextResponse(plan.to_gitlab_yaml(), media_type="text/yaml")

    data = plan.to_dict()
    data["applied"] = project.plan_mode == PlanMode.ACTIVE
    if project.plan_mode != PlanMode.ACTIVE:
        data["note"] = (
            f"Project is in \u00ab{project.plan_mode.value}\u00bb mode: the plan is computed "
            "but you should run the full suite."
        )
    return data


@router.get("/plan/preview/{project_id}/{commit_sha}", summary="Переглянути план для коміту")
def preview_plan(
    commit_sha: str,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    commit = db.get(Commit, commit_sha)
    if commit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commit not found")
    plan = build_plan(db, project, commit, ref=commit.branch)
    return {"plan": plan.to_dict(), "gitlab_yaml": plan.to_gitlab_yaml()}
