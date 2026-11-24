"""Схеми запитів і відповідей API (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import (
    AlertKind, CIProvider, ModelType, PlanMode, PolicyType, RecoStatus, RecoType,
    Role, RunStatus, Severity,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ авторизація
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    email: str


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str
    role: Role


# ---------------------------------------------------------------------- проєкти
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    repo_url: str = ""
    ci_provider: CIProvider = CIProvider.GITLAB
    external_id: str = ""
    default_branch: str = "main"
    access_token: str = ""


class ProjectOut(ORMModel):
    id: int
    name: str
    repo_url: str
    ci_provider: CIProvider
    default_branch: str
    plan_mode: PlanMode
    created_at: datetime


class PlanModeUpdate(BaseModel):
    plan_mode: PlanMode


# --------------------------------------------------------------------- політики
class PolicyCreate(BaseModel):
    name: str
    type: PolicyType
    params: dict = Field(default_factory=dict)
    enabled: bool = True
    branch_pattern: str = "*"


class PolicyUpdate(BaseModel):
    """Часткове оновлення політики.

    Консоль змінює по одному полю: перемикач, шаблон гілок або окремий
    параметр. Вимагати повний об'єкт означало б, що інтерфейс мусить
    надсилати назву й тип політики, яких користувач не редагує.
    """
    name: str | None = None
    params: dict | None = None
    enabled: bool | None = None
    branch_pattern: str | None = None


class PolicyOut(ORMModel):
    id: int
    name: str
    type: PolicyType
    params: dict
    enabled: bool
    branch_pattern: str


# --------------------------------------------------------------------- конвеєри
class JobOut(ORMModel):
    id: int
    name: str
    stage: str
    status: RunStatus
    duration_s: int
    pred_duration_s: float | None
    cache_hit: bool
    shard_index: int | None


class PipelineOut(ORMModel):
    id: int
    project_id: int
    commit_sha: str
    ref: str
    status: RunStatus
    duration_s: int
    queue_time_s: int
    optimized: bool
    started_at: datetime | None
    finished_at: datetime | None
    # Скільки перевірок справді виконано в цьому прогоні й скільки невдалих.
    tests_run: int = 0
    tests_failed: int = 0
    # Парний прогін того самого коміту: GitLab виконує план дочірнім
    # конвеєром, і система бачить два прогони - запит плану і виконання.
    # linked_role - роль парного прогону: "plan" або "execution".
    linked_run_id: int | None = None
    linked_role: str | None = None
    linked_optimized: bool = False


class PipelineDetail(PipelineOut):
    jobs: list[JobOut] = Field(default_factory=list)
    # Прогноз поруч із фактом. Назва поля відрізняється від зв'язку ORM
    # Pipeline.predictions, інакше pydantic підхопив би самі об'єкти;
    # у відповіді ключ лишається «predictions».
    prediction_rows: list[dict] = Field(default_factory=list, serialization_alias="predictions")
    plan_snapshot: dict = Field(default_factory=dict)
    commit_author: str = ""
    commit_message: str = ""
    files_changed: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    # план видано в іншому прогоні (запиті плану), а тут він виконаний
    plan_from_run: int | None = None
    # перевірки проєкту, що були живими на момент прогону, і скільки з них не запускалося
    tests_total: int = 0
    checks_by_suite: list[dict] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------- план і події
class PlanRequest(BaseModel):
    """Запит плану конвеєра. Надсилається службовою задачею з CI.

    Довжини полів обмежені під розміри стовпців у БД. Без цього задовге
    значення доходило до бази й поверталося як внутрішня помилка сервера,
    хоча насправді це хибні вхідні дані: конвеєр одержував 500 замість
    зрозумілого «неправильний запит».
    """
    project_id: int
    commit_sha: str = Field(min_length=4, max_length=40)
    ref: str = Field(default="main", max_length=120)
    author: str = Field(default="", max_length=80)
    message: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    format: str = Field(default="json", pattern="^(json|gitlab-yaml)$")


class WebhookEvent(BaseModel):
    """Спрощена подія CI. Адаптери перетворюють у неї формати GitLab і GitHub."""
    project_id: int
    event: str = Field(pattern="^(pipeline_started|pipeline_finished)$")
    external_id: str = Field(max_length=64)
    commit_sha: str = Field(min_length=4, max_length=40)
    ref: str = Field(default="main", max_length=120)
    status: RunStatus = RunStatus.RUNNING
    duration_s: int = 0
    queue_time_s: int = 0
    author: str = Field(default="", max_length=80)
    message: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    jobs: list[dict] = Field(default_factory=list)
    test_results: list[dict] = Field(default_factory=list)
    optimized: bool = False


# ---------------------------------------------------------------- рекомендації
class RecommendationOut(ORMModel):
    id: int
    type: RecoType
    title: str
    description: str
    evidence: dict
    expected_gain_s: int
    status: RecoStatus
    created_at: datetime


# ---------------------------------------------------------------------- моделі
class ModelVersionOut(ORMModel):
    id: int
    type: ModelType
    version: str
    algorithm: str
    metrics: dict
    train_samples: int
    trained_at: datetime
    is_active: bool


class AlertOut(ORMModel):
    id: int
    kind: AlertKind
    severity: Severity
    message: str
    details: dict
    acknowledged: bool
    created_at: datetime


# ------------------------------------------------- повідомлення про проблеми
class ProblemReportIn(BaseModel):
    subject_type: Literal["alert", "pipeline", "recommendation", "test_case", "other"] = "other"
    subject_id: int | None = None
    note: str = Field(min_length=3, max_length=2000)


class ProblemReportOut(ORMModel):
    id: int
    project_id: int
    subject_type: str
    subject_id: int | None
    note: str
    resolved: bool
    created_at: datetime
    resolved_at: datetime | None
    reported_by: str = ""
