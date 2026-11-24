"""Приймання подій CI-серверів.

Три способи надіслати дані в систему:

    POST /webhooks/ci               уже нормалізована подія (плагін, тести)
    POST /webhooks/gitlab/{id}      сирий Pipeline Hook GitLab
    POST /webhooks/github/{id}      сире повідомлення workflow_run GitHub
    POST /webhooks/junit/{id}       звіт JUnit XML із результатами тестів

Перетворення форматів живе в адаптерах (app/integrations), а не тут: саме це
дає змогу підключати нові CI-сервери без змін у бізнес-логіці, як описано в
підрозділі про інтерфейс CIAdapter.

ПРО ПІДПИС. Усі провайдерські ендпоінти вимагають налаштованого секрету
проєкту. Без перевірки підпису будь-хто зміг би надсилати вигадані прогони
й тим самим псувати навчальну вибірку моделей. Тому відсутній секрет - це
відмова, а не дозвіл.
"""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.integrations import github as gh_adapter
from app.integrations import gitlab as gl_adapter
from app.integrations import junit
from app.models import Pipeline, Project, RunStatus
from app.schemas import WebhookEvent
from app.services.ingestion import check_for_anomaly, ingest

logger = logging.getLogger("cicd.webhooks")

router = APIRouter(prefix="/webhooks", tags=["Події CI"])


def _post_process(pipeline_id: int) -> None:
    """Фонова обробка: перевірка аномалій після збереження події."""
    db = SessionLocal()
    try:
        p = db.get(Pipeline, pipeline_id)
        if p is not None:
            check_for_anomaly(db, p)
    finally:
        db.close()


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _require_secret(project: Project) -> str:
    if not project.webhook_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No webhook secret configured for this project: unsigned events are "
            "rejected, because accepting them would let anyone poison the training data",
        )
    return project.webhook_secret


def _accept(db: Session, ev: WebhookEvent, background: BackgroundTasks) -> dict:
    pipeline = ingest(db, ev)
    if ev.event == "pipeline_finished":
        background.add_task(_post_process, pipeline.id)
    return {
        "accepted": True,
        "pipeline_id": pipeline.id,
        "status": pipeline.status.value,
    }


async def _payload(request: Request) -> tuple[bytes, dict]:
    raw = await request.body()
    try:
        return raw, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body is not valid JSON")


# ------------------------------------------------- уже нормалізована подія
@router.post("/ci", status_code=202, summary="Прийняти подію CI-сервера")
def receive(
    ev: WebhookEvent,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Приймає подію і одразу відповідає. Важка обробка виконується у фоні,
    щоб CI-сервер не чекав."""
    _project_or_404(db, ev.project_id)
    return _accept(db, ev, background)


# ------------------------------------------------------------------ GitLab
@router.post("/gitlab/{project_id}", status_code=202, summary="Прийняти Pipeline Hook GitLab")
async def receive_gitlab(
    project_id: int,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Сирий вебхук GitLab. Підпис - секретний маркер у заголовку X-Gitlab-Token."""
    project = _project_or_404(db, project_id)
    secret = _require_secret(project)

    raw, payload = await _payload(request)
    token = request.headers.get("X-Gitlab-Token", "")
    if not hmac.compare_digest(token, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")

    ev = gl_adapter.parse_webhook(project_id, payload)
    if ev is None:
        # Це не подія конвеєра (push, issue тощо) - мовчки підтверджуємо.
        return {"accepted": False, "reason": "event is not about a pipeline"}
    return _accept(db, ev, background)


# ------------------------------------------------------------------ GitHub
@router.post("/github/{project_id}", status_code=202, summary="Прийняти подію workflow_run GitHub")
async def receive_github(
    project_id: int,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Сирий вебхук GitHub. Підпис - HMAC-SHA256 у заголовку X-Hub-Signature-256."""
    project = _project_or_404(db, project_id)
    secret = _require_secret(project)

    raw, payload = await _payload(request)
    signature = request.headers.get("X-Hub-Signature-256", "")
    adapter = gh_adapter.GitHubActionsAdapter(project_ref=project.external_id)
    if not adapter.verify_signature(raw, signature, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    ev = gh_adapter.parse_webhook(project_id, payload)
    if ev is None:
        return {"accepted": False, "reason": "event is not about a pipeline"}
    return _accept(db, ev, background)


# ------------------------------------------------------------- звіт JUnit
@router.post("/junit/{project_id}", status_code=202, summary="Прийняти звіт JUnit XML")
async def receive_junit(
    project_id: int,
    external_id: str,
    commit_sha: str,
    request: Request,
    background: BackgroundTasks,
    ref: str = "main",
    job: str = "",
    db: Session = Depends(get_db),
):
    """Результати окремих тестів у форматі JUnit XML.

    Це єдиний шлях, яким дані рівня окремого тесту потрапляють у систему з
    GitHub Actions, бо його API таких результатів не віддає. Конвеєр надсилає
    сюди вміст звіту після прогону тестів.

    Тривалість і стан уже збереженого прогону не перезаписуються: цей запит
    лише доповнює його результатами тестів.
    """
    project = _project_or_404(db, project_id)
    secret = _require_secret(project)
    if not hmac.compare_digest(request.headers.get("X-CICD-Token", ""), secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")

    xml = (await request.body()).decode("utf-8", errors="replace")
    if not xml.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty report")
    try:
        report = junit.parse_string(xml)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not parse the JUnit report: {exc}")
    if not report.tests:
        return {"accepted": False, "reason": "report contains no tests"}

    # Зберігаємо наявні стан і тривалість прогону: цей запит про тести, а не
    # про сам конвеєр. Інакше нульова тривалість затерла б справжню.
    existing = db.scalars(
        select(Pipeline).where(
            Pipeline.project_id == project_id, Pipeline.external_id == external_id
        )
    ).first()

    ev = WebhookEvent(
        project_id=project_id,
        event="pipeline_finished",
        external_id=external_id,
        commit_sha=commit_sha,
        ref=ref,
        status=existing.status if existing else RunStatus.SUCCESS,
        duration_s=existing.duration_s if existing else report.total_time_ms // 1000,
        queue_time_s=existing.queue_time_s if existing else 0,
        test_results=report.to_payload(job),
        optimized=bool(existing.optimized) if existing else False,
    )
    result = _accept(db, ev, background)
    result["tests"] = len(report.tests)
    result["failed"] = report.failed
    return result
