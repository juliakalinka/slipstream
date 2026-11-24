"""Інтерфейс адаптера CI-сервера.

Відповідає класу CIAdapter із діаграми класів (Рис. 2.6). Наявність єдиного
інтерфейсу дає змогу підключати нові CI-сервери без змін у бізнес-логіці:
Ingestion Service і Plan Builder працюють лише з цим інтерфейсом.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RemotePipeline:
    """Уніфіковане подання конвеєра, незалежне від конкретного CI-сервера."""
    external_id: str
    commit_sha: str
    ref: str
    status: str                      # success | failed | canceled | running
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: int = 0
    queue_time_s: int = 0
    author: str = ""
    message: str = ""
    trigger: str = "push"
    attempt: int = 1
    raw: dict = field(default_factory=dict)


@dataclass
class RemoteJob:
    external_id: str
    name: str
    stage: str
    status: str
    duration_s: int = 0
    runner: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class RemoteCommit:
    sha: str
    author: str = ""
    message: str = ""
    changed_paths: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    committed_at: datetime | None = None


class CIAdapter(ABC):
    """Контракт, який має реалізувати кожен адаптер CI-сервера."""

    provider: str = "generic"

    def __init__(self, base_url: str, token: str = "", project_ref: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_ref = project_ref

    @abstractmethod
    def fetch_pipelines(self, since: datetime | None = None, limit: int = 100) -> list[RemotePipeline]:
        """Отримати перелік конвеєрів проєкту."""

    @abstractmethod
    def fetch_jobs(self, pipeline_external_id: str) -> list[RemoteJob]:
        """Отримати задачі конкретного конвеєра."""

    @abstractmethod
    def fetch_commit(self, sha: str) -> RemoteCommit | None:
        """Отримати відомості про коміт разом зі зміненими файлами."""

    @abstractmethod
    def fetch_test_report(self, pipeline_external_id: str) -> str:
        """Отримати звіт тестів у форматі JUnit XML. Порожній рядок, якщо звіту немає."""

    def verify_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Перевірка підпису вебхука. За замовчуванням порівняння секрету."""
        return bool(secret) and signature == secret

    def health(self) -> bool:
        try:
            self.fetch_pipelines(limit=1)
            return True
        except Exception:
            return False
