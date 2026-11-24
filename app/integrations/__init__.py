"""Адаптери CI-серверів."""
from app.integrations.base import CIAdapter, RemoteCommit, RemoteJob, RemotePipeline
from app.integrations.github import GitHubActionsAdapter
from app.integrations.gitlab import GitLabAdapter

__all__ = [
    "CIAdapter", "RemotePipeline", "RemoteJob", "RemoteCommit",
    "GitLabAdapter", "GitHubActionsAdapter", "get_adapter",
]


def get_adapter(provider: str, base_url: str, token: str = "", project_ref: str = "", **kw) -> CIAdapter:
    """Фабрика адаптерів: бізнес-логіка не знає про конкретні реалізації."""
    p = (provider or "").lower()
    if p == "gitlab":
        return GitLabAdapter(base_url or "https://gitlab.com", token, project_ref)
    if p == "github":
        return GitHubActionsAdapter(base_url or "https://api.github.com", token, project_ref,
                                    workflow_file=kw.get("workflow_file"))
    raise ValueError(f"немає адаптера для CI-сервера «{provider}»")
