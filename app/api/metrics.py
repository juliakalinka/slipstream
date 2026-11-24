from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_devops
from app.db import get_db
from app.models import Alert, Project, User
from app.schemas import AlertOut
from app.services import metrics as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["Метрики"])


@router.get("/dashboard", summary="Зведений дашборд проєкту")
def dashboard(
    project: Project = Depends(get_project),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    pm = svc.pipeline_metrics(db, project.id, days)
    dora = svc.dora_metrics(db, project.id, days, project.default_branch)
    # Проєкт, у якому давно не було прогонів, показує в короткому вікні самі
    # нулі, і це виглядає як несправність. Тому відповідь називає момент
    # останнього прогону й найкоротше вікно, у яке він потрапляє.
    last = svc.last_run_at(db, project.id)
    suggested = None
    if pm.total == 0 and last is not None:
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        suggested = next((d for d in (7, 30, 90, 365) if d > days and age_days < d), None)
    return {
        "project": {"id": project.id, "name": project.name, "mode": project.plan_mode.value},
        "window_days": days,
        "last_run_at": last.isoformat() if last else None,
        "suggested_days": suggested,
        "pipelines": {
            "total": pm.total, "success": pm.success, "failed": pm.failed,
            "success_rate": pm.success_rate,
            "mean_duration_s": pm.mean_duration_s,
            "median_duration_s": pm.median_duration_s,
            "p95_duration_s": pm.p95_duration_s,
            "mean_queue_s": pm.mean_queue_s,
            "optimized_share": pm.optimized_share,
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
        "slowest_jobs": svc.slowest_jobs(db, project.id, days, limit=5),
        "flakiest_tests": svc.flakiest_tests(db, project.id, limit=5),
        "trend": pm.trend[-14:],
    }


@router.get("/dora", summary="Чотири метрики DORA")
def dora(
    project: Project = Depends(get_project),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    d = svc.dora_metrics(db, project.id, days, project.default_branch)
    return d.__dict__


@router.get("/flaky", summary="Нестабільні тести")
def flaky(
    project: Project = Depends(get_project),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return svc.flakiest_tests(db, project.id, limit)


@router.put("/alerts/{alert_id}/ack", response_model=AlertOut,
            summary="Підтвердити ознайомлення зі сповіщенням")
def acknowledge_alert(
    alert_id: int,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """Сповіщення, яке не можна закрити, швидко перетворюється на шум.

    Підтвердження прибирає сповіщення зі списку активних, але сам запис
    лишається: історія аномалій потрібна для аналізу.
    """
    alert = db.get(Alert, alert_id)
    if alert is None or alert.project_id != project.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    alert.acknowledged = True
    db.commit()
    db.refresh(alert)
    return alert


@router.get("/alerts", response_model=list[AlertOut], summary="Сповіщення про аномалії")
def alerts(
    project: Project = Depends(get_project),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return db.scalars(
        select(Alert)
        .where(Alert.project_id == project.id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).all()
