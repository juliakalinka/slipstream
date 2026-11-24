from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_devops
from app.db import SessionLocal, get_db
from app.models import ModelType, ModelVersion, Project, User
from app.schemas import ModelVersionOut

router = APIRouter(tags=["Моделі"])


def _retrain(project_id: int, default_branch: str) -> None:
    db = SessionLocal()
    try:
        from app.ml.training import train_all
        train_all(db, project_id, default_branch)
    finally:
        db.close()


@router.get("/projects/{project_id}/models", response_model=list[ModelVersionOut],
            summary="Версії моделей проєкту")
def list_models(
    project: Project = Depends(get_project),
    type_filter: ModelType | None = Query(None, alias="type"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    stmt = select(ModelVersion).where(ModelVersion.project_id == project.id)
    if type_filter:
        stmt = stmt.where(ModelVersion.type == type_filter)
    return db.scalars(stmt.order_by(ModelVersion.trained_at.desc())).all()


@router.post("/projects/{project_id}/models/retrain", status_code=202,
             summary="Запустити перенавчання моделей")
def retrain(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    user: User = Depends(require_devops),
):
    """Навчання виконується у фоні: на великій історії воно триває хвилини."""
    background.add_task(_retrain, project.id, project.default_branch)
    return {
        "accepted": True,
        "project_id": project.id,
        "note": "Перенавчання запущено у фоні. Стан дивіться у списку версій моделей.",
    }


@router.put("/models/{model_id}/activate", response_model=ModelVersionOut,
            summary="Зробити версію моделі активною")
def activate(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """Відкат на попередню версію моделі, якщо нова виявилася гіршою."""
    mv = db.get(ModelVersion, model_id)
    if mv is None:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model version not found")
    for other in db.scalars(
        select(ModelVersion).where(
            ModelVersion.project_id == mv.project_id,
            ModelVersion.type == mv.type,
            ModelVersion.is_active.is_(True),
        )
    ).all():
        other.is_active = False
    mv.is_active = True
    db.commit()
    db.refresh(mv)
    from app.ml import registry
    registry.clear_cache()
    return mv
