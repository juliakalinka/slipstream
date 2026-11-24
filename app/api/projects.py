import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project, require_devops
from app.db import get_db
from app.config import settings
from app.models import Policy, PolicyType, Project, User
from app.schemas import (
    PlanModeUpdate, PolicyCreate, PolicyOut, PolicyUpdate, ProjectCreate, ProjectOut,
)

router = APIRouter(prefix="/projects", tags=["Проєкти"])


@router.get("", response_model=list[ProjectOut], summary="Список проєктів")
def list_projects(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.scalars(select(Project).order_by(Project.name)).all()


def default_policies(project_id: int) -> list[Policy]:
    """Набір політик, з якими проєкт починає роботу.

    Без них система для щойно підключеного проєкту не робить нічого: план
    завжди вироджується в повний прогін, бо жодна оптимізація не дозволена.
    Раніше політики створювали лише сценарії імпорту, тому проєкт, доданий
    через інтерфейс або програмний інтерфейс, лишався німим.

    Значення обрано обережними: відбір дозволено тільки на гілках розробника,
    основна гілка захищена, і не менше восьмої частини набору виконується
    завжди.
    """
    return [
        Policy(project_id=project_id, name="Skip docs-only changes",
               type=PolicyType.SKIP_DOCS_ONLY, params={"keep_lint": True},
               branch_pattern="*"),
        Policy(project_id=project_id, name="Test selection under a time budget",
               type=PolicyType.TEST_SELECTION,
               params={"budget_ratio": 0.30, "budget_s": settings.default_test_budget_sec,
                       "min_keep_ratio": 0.12, "protect_default_branch": True},
               branch_pattern="feature/*,fix/*,pull/*,*-patch-*"),
        Policy(project_id=project_id, name="Shard distribution",
               type=PolicyType.SHARDING,
               params={"max_shards": settings.default_shards, "target_s": 120},
               branch_pattern="*"),
        Policy(project_id=project_id, name="Risk-first job ordering",
               type=PolicyType.FAIL_FAST,
               params={"risk_threshold": settings.high_risk_threshold},
               branch_pattern="*"),
    ]


@router.post("", response_model=ProjectOut, status_code=201, summary="Підключити проєкт")
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    if db.scalars(select(Project).where(Project.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A project with this name already exists")
    project = Project(**body.model_dump(), owner_id=user.id)
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add_all(default_policies(project.id))
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectOut, summary="Картка проєкту")
def get_one(project: Project = Depends(get_project), user: User = Depends(current_user)):
    return project


@router.put("/{project_id}/mode", response_model=ProjectOut, summary="Змінити режим оптимізації")
def set_mode(
    body: PlanModeUpdate,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """off - лише спостереження, dry_run - холостий хід, active - план застосовується."""
    project.plan_mode = body.plan_mode
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/policies", response_model=list[PolicyOut], summary="Політики проєкту")
def list_policies(
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return db.scalars(select(Policy).where(Policy.project_id == project.id)).all()


@router.post("/{project_id}/policies", response_model=PolicyOut, status_code=201,
             summary="Додати політику")
def add_policy(
    body: PolicyCreate,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    policy = Policy(project_id=project.id, **body.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.put("/{project_id}/policies/{policy_id}", response_model=PolicyOut,
            summary="Змінити політику")
def update_policy(
    policy_id: int,
    body: PolicyUpdate,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    policy = db.get(Policy, policy_id)
    if policy is None or policy.project_id != project.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    # exclude_unset: поля, яких немає в запиті, лишаються незмінними.
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(policy, k, v)
    db.commit()
    db.refresh(policy)
    return policy


@router.get("/{project_id}/webhook", summary="Стан налаштування вебхука")
def webhook_status(
    project: Project = Depends(get_project),
    user: User = Depends(current_user),
):
    """Чи налаштовано секрет і за якими адресами CI-сервер має надсилати події.

    Сам секрет не повертається: він показується один раз у момент створення.
    """
    base = f"/api/v1/webhooks"
    return {
        "configured": bool(project.webhook_secret),
        "provider": project.ci_provider.value,
        "endpoints": {
            "gitlab": f"{base}/gitlab/{project.id}",
            "github": f"{base}/github/{project.id}",
            "junit": f"{base}/junit/{project.id}?external_id=<id>&commit_sha=<sha>&ref=<branch>",
        },
        "headers": {
            "gitlab": "X-Gitlab-Token",
            "github": "X-Hub-Signature-256 (HMAC-SHA256)",
            "junit": "X-CICD-Token",
        },
    }


@router.post("/{project_id}/webhook-secret", status_code=201,
             summary="Створити або замінити секрет вебхука")
def rotate_webhook_secret(
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """Створює новий секрет і повертає його ОДИН раз.

    Далі секрет не показується: його треба одразу прописати в налаштуваннях
    вебхука CI-сервера. Повторний виклик замінює секрет, і старий перестає
    діяти - це штатний спосіб відкликати доступ.
    """
    secret = secrets.token_urlsafe(32)
    project.webhook_secret = secret
    db.commit()
    return {
        "secret": secret,
        "note": "Секрет показано один раз. Пропишіть його в налаштуваннях вебхука CI-сервера.",
        "endpoints": {
            "gitlab": f"/api/v1/webhooks/gitlab/{project.id}",
            "github": f"/api/v1/webhooks/github/{project.id}",
            "junit": f"/api/v1/webhooks/junit/{project.id}",
        },
    }
