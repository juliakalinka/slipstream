from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_devops
from app.db import get_db
from app.models import Project, RecoStatus, Recommendation, User
from app.schemas import RecommendationOut
from app.services import recommendations as svc

router = APIRouter(tags=["Рекомендації"])


@router.get("/projects/{project_id}/recommendations", response_model=list[RecommendationOut],
            summary="Рекомендації проєкту")
def list_recos(
    project: Project = Depends(get_project),
    status_filter: RecoStatus | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    stmt = select(Recommendation).where(Recommendation.project_id == project.id)
    if status_filter:
        stmt = stmt.where(Recommendation.status == status_filter)
    return db.scalars(stmt.order_by(Recommendation.expected_gain_s.desc())).all()


@router.post("/projects/{project_id}/recommendations/generate",
             response_model=list[RecommendationOut], summary="Сформувати рекомендації")
def generate(
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    return svc.generate(db, project)


@router.put("/recommendations/{reco_id}/apply", summary="Застосувати рекомендацію")
def apply(
    reco_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    reco = db.get(Recommendation, reco_id)
    if reco is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found")
    if reco.status != RecoStatus.NEW:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Recommendation is already {reco.status.value}")
    result = svc.apply(db, reco, user.id)
    return {"id": reco.id, "status": reco.status.value, **result}


@router.put("/recommendations/{reco_id}/reject", summary="Відхилити рекомендацію")
def reject(
    reco_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    reco = db.get(Recommendation, reco_id)
    if reco is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found")
    svc.reject(db, reco, user.id)
    return {"id": reco.id, "status": reco.status.value}
