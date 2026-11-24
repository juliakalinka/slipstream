"""ORM-моделі. Відповідають схемі бази даних (Рис. 4.1 пояснювальної записки)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.services.crypto import EncryptedString


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- переліки
class Role(str, enum.Enum):
    DEVOPS = "devops"       # налаштовує політики, застосовує рекомендації
    DEVELOPER = "developer"  # переглядає свої збірки і тести
    LEAD = "lead"            # переглядає звіти й метрики


class CIProvider(str, enum.Enum):
    GITLAB = "gitlab"
    GITHUB = "github"
    JENKINS = "jenkins"
    SYNTHETIC = "synthetic"  # локальний стенд-генератор для експериментів


class RunStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    RUNNING = "running"


class TestStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PolicyType(str, enum.Enum):
    SKIP_DOCS_ONLY = "skip_docs_only"      # пропустити конвеєр для змін лише в документації
    TEST_SELECTION = "test_selection"      # відбір підмножини тестів
    SHARDING = "sharding"                  # розподіл тестів на паралельні шарди
    FAIL_FAST = "fail_fast"                # впорядкування задач за ризиком
    FLAKY_QUARANTINE = "flaky_quarantine"  # карантин нестабільних тестів


class ModelType(str, enum.Enum):
    FAILURE = "failure"      # класифікація: чи впаде конвеєр
    DURATION = "duration"    # регресія: тривалість задачі
    TEST_RANK = "test_rank"  # ранжування тестів за ймовірністю падіння
    ANOMALY = "anomaly"      # виявлення викидів


class RecoType(str, enum.Enum):
    QUARANTINE_FLAKY = "quarantine_flaky"
    INCREASE_SHARDS = "increase_shards"
    REORDER_JOBS = "reorder_jobs"
    ENABLE_CACHE = "enable_cache"
    SPLIT_SLOW_JOB = "split_slow_job"
    REDUCE_TEST_SCOPE = "reduce_test_scope"


class RecoStatus(str, enum.Enum):
    NEW = "new"
    APPLIED = "applied"
    REJECTED = "rejected"


class AlertKind(str, enum.Enum):
    DURATION_SPIKE = "duration_spike"
    QUEUE_DELAY = "queue_delay"
    FAILURE_RATE = "failure_rate"
    FLAKY_BURST = "flaky_burst"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class PlanMode(str, enum.Enum):
    OFF = "off"            # система лише спостерігає
    DRY_RUN = "dry_run"    # рахує план, але віддає повний набір
    ACTIVE = "active"      # план застосовується


# --------------------------------------------------------------------- таблиці
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.DEVELOPER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list[Project]] = relationship(back_populates="owner")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    repo_url: Mapped[str] = mapped_column(String(255), default="")
    ci_provider: Mapped[CIProvider] = mapped_column(Enum(CIProvider), default=CIProvider.GITLAB)
    external_id: Mapped[str] = mapped_column(String(64), default="")
    default_branch: Mapped[str] = mapped_column(String(60), default="main")
    # Маркер доступу до репозиторію лежить у базі зашифрованим (нефункціональна
    # вимога до зберігання маркерів): решта коду бачить звичайний рядок.
    access_token: Mapped[str] = mapped_column(EncryptedString(512), default="")
    webhook_secret: Mapped[str] = mapped_column(String(64), default="")
    plan_mode: Mapped[PlanMode] = mapped_column(Enum(PlanMode), default=PlanMode.DRY_RUN)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User | None] = relationship(back_populates="projects")
    pipelines: Mapped[list[Pipeline]] = relationship(back_populates="project", cascade="all, delete-orphan")
    policies: Mapped[list[Policy]] = relationship(back_populates="project", cascade="all, delete-orphan")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Policy(Base):
    """Політика оптимізації: що системі дозволено робити з конвеєром."""
    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[PolicyType] = mapped_column(Enum(PolicyType))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # гілки, на яких політику застосовувати; порожньо = всі, крім захищених
    branch_pattern: Mapped[str] = mapped_column(String(120), default="*")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="policies")


class Commit(Base):
    __tablename__ = "commits"

    sha: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    author: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    branch: Mapped[str] = mapped_column(String(120), default="main")
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    changed_paths: Mapped[list] = mapped_column(JSON, default=list)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    pipelines: Mapped[list[Pipeline]] = relationship(back_populates="commit")

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    commit_sha: Mapped[str] = mapped_column(ForeignKey("commits.sha"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), default="")
    ref: Mapped[str] = mapped_column(String(120), default="main")
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.RUNNING, index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="push")
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    queue_time_s: Mapped[int] = mapped_column(Integer, default=0)
    optimized: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    plan_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="pipelines")
    commit: Mapped[Commit] = relationship(back_populates="pipelines")
    jobs: Mapped[list[Job]] = relationship(back_populates="pipeline", cascade="all, delete-orphan")
    predictions: Mapped[list[Prediction]] = relationship(back_populates="pipeline", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_pipelines_project_started", "project_id", "started_at"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    stage: Mapped[str] = mapped_column(String(40), default="test")
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.RUNNING)
    runner: Mapped[str] = mapped_column(String(60), default="shared-runner-1")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    pred_duration_s: Mapped[float | None] = mapped_column(Float)
    pred_fail_prob: Mapped[float | None] = mapped_column(Float)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    shard_index: Mapped[int | None] = mapped_column(Integer)

    pipeline: Mapped[Pipeline] = relationship(back_populates="jobs")
    test_results: Mapped[list[TestResult]] = relationship(back_populates="job", cascade="all, delete-orphan")


class TestCase(Base):
    __tablename__ = "test_cases"
    __test__ = False  # не збирати як тестовий клас pytest

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    suite: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    file_path: Mapped[str] = mapped_column(String(255), default="")
    avg_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, default=0)
    flaky_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Тест залежить від порядку виконання: проходить лише тоді, коли перед ним
    # відпрацювали інші тести того самого набору. Такі тести НЕ МОЖНА вільно
    # переставляти й розкидати по шардах, інакше система сама створить збій.
    # Ознака заповнюється або з відкритого каталогу IDoFT, або за даними
    # власних прогонів із різним порядком тестів.
    order_dependent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flaky_category: Mapped[str] = mapped_column(String(24), default="")
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="test_cases")
    results: Mapped[list[TestResult]] = relationship(back_populates="test_case", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("project_id", "suite", "name", name="uq_test_case"),)

    @property
    def fail_rate(self) -> float:
        return self.total_failures / self.total_runs if self.total_runs else 0.0


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    test_case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id"), index=True)
    status: Mapped[TestStatus] = mapped_column(Enum(TestStatus), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")

    job: Mapped[Job] = relationship(back_populates="test_results")
    test_case: Mapped[TestCase] = relationship(back_populates="results")


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[ModelType] = mapped_column(Enum(ModelType), index=True)
    version: Mapped[str] = mapped_column(String(20))
    algorithm: Mapped[str] = mapped_column(String(60), default="")
    artifact_path: Mapped[str] = mapped_column(String(255), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_names: Mapped[list] = mapped_column(JSON, default=list)
    train_samples: Mapped[int] = mapped_column(Integer, default=0)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    predictions: Mapped[list[Prediction]] = relationship(back_populates="model_version")


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    type: Mapped[ModelType] = mapped_column(Enum(ModelType))
    value: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pipeline: Mapped[Pipeline] = relationship(back_populates="predictions")
    model_version: Mapped[ModelVersion | None] = relationship(back_populates="predictions")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"))
    type: Mapped[RecoType] = mapped_column(Enum(RecoType))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_gain_s: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[RecoStatus] = mapped_column(Enum(RecoStatus), default=RecoStatus.NEW, index=True)
    applied_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"))
    kind: Mapped[AlertKind] = mapped_column(Enum(AlertKind))
    severity: Mapped[Severity] = mapped_column(Enum(Severity), default=Severity.WARNING)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProblemReport(Base):
    """Повідомлення користувача про хибне спрацювання системи.

    Сповіщення про аномалію, план конвеєра чи рекомендація можуть бути
    помилковими, і помітити це здатна лише людина, яка знає проєкт. Запис
    зберігає, що саме вона вважає помилкою і чому, щоб DevOps-інженер міг
    розібрати випадок і за потреби змінити політику або перенавчити модель.
    """
    __tablename__ = "problem_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # alert | pipeline | recommendation | test_case | other
    subject_type: Mapped[str] = mapped_column(String(24), default="other")
    subject_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
