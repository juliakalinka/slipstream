"""Адаптер GitHub Actions.

Обмеження, яке треба знати й описати в записці: API GitHub Actions НЕ віддає
результати окремих тестів. Вони лежать усередині артефактів або журналів
виконання. Тому для задач відбору тестів і виявлення нестабільних тестів
цей адаптер дає лише рівень задач, але не рівень окремих тестів.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.integrations.base import CIAdapter, RemoteCommit, RemoteJob, RemotePipeline

CONCLUSION_MAP = {
    "success": "success", "failure": "failed", "timed_out": "failed",
    "startup_failure": "failed", "cancelled": "canceled", "skipped": "canceled",
    "neutral": "success", "action_required": "failed",
}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubActionsAdapter(CIAdapter):
    provider = "github"

    def __init__(self, base_url: str = "https://api.github.com", token: str = "",
                 project_ref: str = "", workflow_file: str | None = None):
        super().__init__(base_url, token, project_ref)
        self.workflow_file = workflow_file

    def _client(self) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cicd-pipeline-optimizer",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=30.0)

    def fetch_pipelines(self, since: datetime | None = None, limit: int = 100) -> list[RemotePipeline]:
        path = (
            f"/repos/{self.project_ref}/actions/workflows/{self.workflow_file}/runs"
            if self.workflow_file
            else f"/repos/{self.project_ref}/actions/runs"
        )
        params: dict = {"per_page": min(limit, 100)}
        if since:
            params["created"] = f">={since.astimezone(timezone.utc).date().isoformat()}"

        with self._client() as c:
            r = c.get(path, params=params)
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])[:limit]

        out: list[RemotePipeline] = []
        for run in runs:
            created, started = _ts(run.get("created_at")), _ts(run.get("run_started_at"))
            finished = _ts(run.get("updated_at"))
            started = started or created
            head = run.get("head_commit") or {}
            out.append(RemotePipeline(
                external_id=str(run["id"]),
                commit_sha=run.get("head_sha", ""),
                ref=run.get("head_branch") or "main",
                status=("running" if run.get("status") != "completed"
                        else CONCLUSION_MAP.get(run.get("conclusion"), "canceled")),
                created_at=created, started_at=started, finished_at=finished,
                duration_s=int((finished - started).total_seconds()) if (finished and started) else 0,
                queue_time_s=int((started - created).total_seconds()) if (started and created) else 0,
                author=(run.get("actor") or {}).get("login", ""),
                message=(head.get("message") or "")[:500],
                trigger=run.get("event", "push"),
                attempt=int(run.get("run_attempt") or 1),
                raw={"workflow": run.get("name"), "path": run.get("path")},
            ))
        return out

    def fetch_jobs(self, pipeline_external_id: str) -> list[RemoteJob]:
        with self._client() as c:
            r = c.get(f"/repos/{self.project_ref}/actions/runs/{pipeline_external_id}/jobs",
                      params={"per_page": 100})
            r.raise_for_status()
            jobs = r.json().get("jobs", [])

        out: list[RemoteJob] = []
        for j in jobs:
            st, fin = _ts(j.get("started_at")), _ts(j.get("completed_at"))
            out.append(RemoteJob(
                external_id=str(j["id"]),
                name=(j.get("name") or "job")[:80],
                stage=(j.get("workflow_name") or "test")[:40],
                status=("running" if j.get("status") != "completed"
                        else CONCLUSION_MAP.get(j.get("conclusion"), "canceled")),
                duration_s=int((fin - st).total_seconds()) if (st and fin) else 0,
                runner=(j.get("runner_name") or "github-hosted")[:60],
                started_at=st, finished_at=fin,
            ))
        return out

    def fetch_commit(self, sha: str) -> RemoteCommit | None:
        with self._client() as c:
            r = c.get(f"/repos/{self.project_ref}/commits/{sha}")
            if r.status_code != 200:
                return None
            d = r.json()
        stats = d.get("stats") or {}
        files = d.get("files") or []
        commit = d.get("commit") or {}
        return RemoteCommit(
            sha=sha,
            author=((commit.get("author") or {}).get("name")
                    or (d.get("author") or {}).get("login") or ""),
            message=(commit.get("message") or "")[:500],
            changed_paths=[f.get("filename", "") for f in files][:300],
            additions=int(stats.get("additions", 0)),
            deletions=int(stats.get("deletions", 0)),
            committed_at=_ts((commit.get("author") or {}).get("date")),
        )

    def fetch_test_report(self, pipeline_external_id: str) -> str:
        """GitHub Actions не надає результатів окремих тестів через API.

        Повертається порожній рядок. Щоб отримати такі дані, конвеєр має
        сам надсилати звіт JUnit у систему через вебхук.
        """
        return ""

    def verify_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """GitHub підписує вебхуки за алгоритмом HMAC-SHA256."""
        import hashlib
        import hmac
        if not secret or not signature:
            return False
        expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


def parse_webhook(project_id: int, payload: dict) -> "WebhookEvent | None":
    """Перетворити сире повідомлення workflow_run GitHub у подію системи.

    Нагадування про обмеження: GitHub Actions не передає результатів окремих
    тестів навіть у вебхуку. Щоб вони потрапили в систему, конвеєр має сам
    надіслати звіт JUnit на /webhooks/junit.
    """
    from app.models import RunStatus
    from app.schemas import WebhookEvent

    run = payload.get("workflow_run")
    if not run:
        return None

    completed = run.get("status") == "completed"
    status = CONCLUSION_MAP.get(run.get("conclusion"), "canceled") if completed else "running"

    created, started = _ts(run.get("created_at")), _ts(run.get("run_started_at"))
    finished = _ts(run.get("updated_at"))
    started = started or created
    head = run.get("head_commit") or {}

    return WebhookEvent(
        project_id=project_id,
        event="pipeline_finished" if completed else "pipeline_started",
        external_id=str(run.get("id") or ""),
        commit_sha=run.get("head_sha", ""),
        ref=(run.get("head_branch") or "main")[:120],
        status=RunStatus(status),
        duration_s=int((finished - started).total_seconds()) if (finished and started) else 0,
        queue_time_s=int((started - created).total_seconds()) if (started and created) else 0,
        author=((run.get("actor") or {}).get("login") or "")[:80],
        message=(head.get("message") or "")[:500],
    )
