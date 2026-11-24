"""Адаптер GitLab CI.

GitLab, на відміну від GitHub Actions, віддає звіти тестів через API,
тому саме на стенді GitLab можна зібрати дані для моделі відбору тестів
і для виявлення нестабільних тестів.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.integrations.base import CIAdapter, RemoteCommit, RemoteJob, RemotePipeline

STATUS_MAP = {
    "success": "success", "failed": "failed", "canceled": "canceled",
    "running": "running", "pending": "running", "created": "running",
    "skipped": "canceled", "manual": "canceled",
}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitLabAdapter(CIAdapter):
    provider = "gitlab"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": self.token} if self.token else {},
            timeout=30.0,
        )

    def fetch_pipelines(self, since: datetime | None = None, limit: int = 100) -> list[RemotePipeline]:
        params: dict = {"per_page": min(limit, 100), "order_by": "updated_at", "sort": "desc"}
        if since:
            params["updated_after"] = since.astimezone(timezone.utc).isoformat()

        out: list[RemotePipeline] = []
        with self._client() as c:
            r = c.get(f"/projects/{self.project_ref}/pipelines", params=params)
            r.raise_for_status()
            for p in r.json()[:limit]:
                # список конвеєрів не містить тривалості, тому добираємо деталі
                d = c.get(f"/projects/{self.project_ref}/pipelines/{p['id']}")
                detail = d.json() if d.status_code == 200 else p
                created, started = _ts(detail.get("created_at")), _ts(detail.get("started_at"))
                out.append(RemotePipeline(
                    external_id=str(p["id"]),
                    commit_sha=p.get("sha", ""),
                    ref=p.get("ref", "main"),
                    status=STATUS_MAP.get(p.get("status", ""), "running"),
                    created_at=created,
                    started_at=started,
                    finished_at=_ts(detail.get("finished_at")),
                    duration_s=int(detail.get("duration") or 0),
                    queue_time_s=int((started - created).total_seconds()) if (started and created) else 0,
                    author=(detail.get("user") or {}).get("username", ""),
                    trigger=detail.get("source", "push"),
                    raw=detail,
                ))
        return out

    def fetch_jobs(self, pipeline_external_id: str) -> list[RemoteJob]:
        with self._client() as c:
            r = c.get(
                f"/projects/{self.project_ref}/pipelines/{pipeline_external_id}/jobs",
                params={"per_page": 100},
            )
            r.raise_for_status()
            return [
                RemoteJob(
                    external_id=str(j["id"]),
                    name=j.get("name", "job")[:80],
                    stage=j.get("stage", "test")[:40],
                    status=STATUS_MAP.get(j.get("status", ""), "running"),
                    duration_s=int(j.get("duration") or 0),
                    runner=((j.get("runner") or {}).get("description") or "")[:60],
                    started_at=_ts(j.get("started_at")),
                    finished_at=_ts(j.get("finished_at")),
                )
                for j in r.json()
            ]

    def fetch_commit(self, sha: str) -> RemoteCommit | None:
        with self._client() as c:
            r = c.get(f"/projects/{self.project_ref}/repository/commits/{sha}")
            if r.status_code != 200:
                return None
            d = r.json()
            stats = d.get("stats") or {}

            paths: list[str] = []
            diff = c.get(f"/projects/{self.project_ref}/repository/commits/{sha}/diff",
                         params={"per_page": 100})
            if diff.status_code == 200:
                paths = [x.get("new_path") or x.get("old_path", "") for x in diff.json()]

            return RemoteCommit(
                sha=sha,
                author=d.get("author_name", ""),
                message=(d.get("title") or "")[:500],
                changed_paths=[p for p in paths if p][:300],
                additions=int(stats.get("additions", 0)),
                deletions=int(stats.get("deletions", 0)),
                committed_at=_ts(d.get("committed_date")),
            )

    def fetch_test_report(self, pipeline_external_id: str) -> str:
        """GitLab віддає зведений звіт тестів у JSON, а артефакти - у JUnit XML."""
        with self._client() as c:
            for job in self.fetch_jobs(pipeline_external_id):
                r = c.get(
                    f"/projects/{self.project_ref}/jobs/{job.external_id}/artifacts/"
                    f"report.xml"
                )
                if r.status_code == 200 and r.text.strip().startswith("<"):
                    return r.text
        return ""

    def trigger_pipeline(self, ref: str, variables: dict[str, str] | None = None) -> str:
        """Запустити конвеєр із заданими змінними. Використовується для
        застосування плану, коли CI не вміє дочірніх конвеєрів."""
        with self._client() as c:
            r = c.post(
                f"/projects/{self.project_ref}/pipeline",
                json={
                    "ref": ref,
                    "variables": [{"key": k, "value": v} for k, v in (variables or {}).items()],
                },
            )
            r.raise_for_status()
            return str(r.json().get("id", ""))


def parse_webhook(project_id: int, payload: dict) -> "WebhookEvent | None":
    """Перетворити сирий Pipeline Hook GitLab у подію системи.

    Це та ланка, якої бракувало: ендпоінт вебхука приймав уже нормалізовану
    подію, тому справжній GitLab не міг надсилати свої повідомлення напряму.
    Повертає None для подій, які не стосуються конвеєра.
    """
    from app.models import RunStatus
    from app.schemas import WebhookEvent

    if payload.get("object_kind") != "pipeline":
        return None
    attrs = payload.get("object_attributes") or {}
    commit = payload.get("commit") or {}

    raw_status = (attrs.get("status") or "").lower()
    status = STATUS_MAP.get(raw_status, "running")
    finished = status in ("success", "failed", "canceled")

    jobs = []
    for b in payload.get("builds") or []:
        runner = (b.get("runner") or {}) or {}
        jobs.append({
            "name": (b.get("name") or "job")[:80],
            "stage": (b.get("stage") or "test")[:40],
            "status": STATUS_MAP.get((b.get("status") or "").lower(), "running"),
            "duration_s": int(b.get("duration") or 0),
            "runner": (runner.get("description") or "")[:60],
        })

    author = (commit.get("author") or {}).get("name") or ""
    variables = {v.get("key"): v.get("value") for v in (attrs.get("variables") or [])}

    return WebhookEvent(
        project_id=project_id,
        event="pipeline_finished" if finished else "pipeline_started",
        external_id=str(attrs.get("id") or ""),
        commit_sha=(attrs.get("sha") or commit.get("id") or ""),
        ref=(attrs.get("ref") or "main")[:120],
        status=RunStatus(status),
        duration_s=int(attrs.get("duration") or 0),
        queue_time_s=int(attrs.get("queued_duration") or 0),
        author=author[:80],
        message=(commit.get("message") or "")[:500],
        jobs=jobs,
        # ознака того, що конвеєр виконувався за планом системи
        optimized=str(variables.get("CICD_OPT_APPLIED", "")).lower() in ("1", "true", "yes"),
    )
