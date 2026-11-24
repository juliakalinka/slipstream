"""Імпорт РЕАЛЬНОЇ історії CI з GitHub Actions у базу системи.

Це основне джерело даних для розділу 4 пояснювальної записки. На відміну від
scripts/seed_synthetic.py, тут немає жодної закладеної закономірності:
беруться справжні запуски конвеєрів відкритих проєктів з їхніми справжніми
тривалостями, статусами, авторами й повторними спробами.

Що дає GitHub Actions API:
    workflow_run  -> конвеєр (статус, тривалість, час у черзі, гілка, подія)
    run_attempt   -> повторний запуск того самого коміту (сигнал нестабільності)
    head_commit   -> коміт (автор, повідомлення, час)
    jobs          -> задачі конвеєра з їхніми тривалостями
    commit stats  -> кількість змінених файлів, додані й вилучені рядки

ОБМЕЖЕННЯ ЧАСТОТИ ЗАПИТІВ. Без токена GitHub дозволяє 60 запитів на годину,
з токеном - 5000. Один запит повертає до 100 запусків, але деталі задач і
статистика комітів коштують по одному запиту кожна. Тому за замовчуванням
вони вимикаються, а вмикаються прапорцями під наявний ліміт.

Створення токена: Settings -> Developer settings -> Personal access tokens.
Для відкритих репозиторіїв достатньо токена без жодних дозволів.

    export GITHUB_TOKEN=ghp_...
    python scripts/import_github.py --repo scikit-learn/scikit-learn --runs 800 \
        --with-jobs 200 --with-commits 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    CIProvider, Commit, Job, Pipeline, PlanMode, Policy, PolicyType, Project,
    Role, RunStatus, User,
)
from app.services.security import hash_password  # noqa: E402

API = "https://api.github.com"


class RateLimitExhausted(RuntimeError):
    pass


class GitHubClient:
    """Мінімальний клієнт із обліком ліміту запитів."""

    def __init__(self, token: str | None = None, reserve: int = 2):
        self.token = token
        self.reserve = reserve      # скільки запитів лишити недоторканими
        self.remaining: int | None = None
        self.used = 0

    def get(self, path: str) -> tuple[object, dict]:
        if self.remaining is not None and self.remaining <= self.reserve:
            raise RateLimitExhausted(
                f"вичерпано ліміт запитів (лишилося {self.remaining}). "
                f"Задайте GITHUB_TOKEN, щоб підняти ліміт до 5000 на годину."
            )
        url = path if path.startswith("http") else f"{API}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "cicd-pipeline-optimizer-research",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                    hdrs = dict(r.headers)
                self.used += 1
                if "X-RateLimit-Remaining" in hdrs:
                    self.remaining = int(hdrs["X-RateLimit-Remaining"])
                return data, hdrs
            except urllib.error.HTTPError as e:
                if e.code == 403 and "rate limit" in e.read().decode(errors="ignore").lower():
                    raise RateLimitExhausted("GitHub відхилив запит через ліміт частоти") from e
                if e.code in (502, 503) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except (TimeoutError, urllib.error.URLError):
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("не вдалося виконати запит")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def map_conclusion(run: dict) -> RunStatus:
    """Звести висновок GitHub до статусу нашої моделі."""
    if run.get("status") != "completed":
        return RunStatus.RUNNING
    return {
        "success": RunStatus.SUCCESS,
        "failure": RunStatus.FAILED,
        "timed_out": RunStatus.FAILED,
        "startup_failure": RunStatus.FAILED,
        "cancelled": RunStatus.CANCELED,
        "skipped": RunStatus.CANCELED,
        "neutral": RunStatus.SUCCESS,
    }.get(run.get("conclusion"), RunStatus.CANCELED)


def ensure_users(db) -> User:
    devops = db.scalars(select(User).where(User.email == "devops@example.com")).first()
    if devops:
        return devops
    devops = User(email="devops@example.com", full_name="DevOps-інженер",
                  password_hash=hash_password("devops123"), role=Role.DEVOPS)
    db.add_all([
        devops,
        User(email="dev@example.com", full_name="Розробник",
             password_hash=hash_password("dev123"), role=Role.DEVELOPER),
        User(email="lead@example.com", full_name="Керівник",
             password_hash=hash_password("lead123"), role=Role.LEAD),
    ])
    db.commit()
    db.refresh(devops)
    return devops


def ensure_project(db, repo: str, owner_id: int, default_branch: str) -> Project:
    project = db.scalars(select(Project).where(Project.name == repo)).first()
    if project:
        return project
    project = Project(
        name=repo,
        repo_url=f"https://github.com/{repo}",
        ci_provider=CIProvider.GITHUB,
        external_id=repo,
        default_branch=default_branch,
        plan_mode=PlanMode.DRY_RUN,
        owner_id=owner_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add_all([
        Policy(project_id=project.id, name="Пропускати конвеєр для змін лише в документації",
               type=PolicyType.SKIP_DOCS_ONLY, params={"keep_lint": True}, branch_pattern="*"),
        Policy(project_id=project.id, name="Відбір тестів за бюджетом часу",
               type=PolicyType.TEST_SELECTION,
               params={"budget_ratio": 0.30, "budget_s": 900, "min_keep_ratio": 0.12,
                       "protect_default_branch": True},
               branch_pattern="feature/*,fix/*,pull/*,*-patch-*"),
        Policy(project_id=project.id, name="Розподіл тестів на шарди",
               type=PolicyType.SHARDING, params={"max_shards": 4, "target_s": 120},
               branch_pattern="*"),
        Policy(project_id=project.id, name="Впорядкування задач за ризиком",
               type=PolicyType.FAIL_FAST, params={"risk_threshold": 0.45}, branch_pattern="*"),
    ])
    db.commit()
    return project


def list_workflows(gh: GitHubClient, repo: str) -> None:
    """Показати робочі процеси репозиторію, щоб обрати справжній CI."""
    data, _ = gh.get(f"/repos/{repo}/actions/workflows?per_page=100")
    print(f"\nРобочі процеси {repo}:")
    for w in sorted(data.get("workflows", []), key=lambda x: x.get("name", "")):
        path = (w.get("path") or "").replace(".github/workflows/", "")
        print(f"   {w.get('state', '?'):10} {path:34} {w.get('name', '')[:40]}")


def import_repo(
    gh: GitHubClient, db, repo: str, max_runs: int,
    with_jobs: int, with_commits: int, workflow_filter: str | None,
    workflow_file: str | None = None, skip_cancelled: bool = False,
) -> dict:
    owner = ensure_users(db)

    meta, _ = gh.get(f"/repos/{repo}")
    project = ensure_project(db, repo, owner.id, meta.get("default_branch", "main"))

    print(f"\nПроєкт {repo} (id={project.id}), основна гілка {project.default_branch}")

    # --- 1. запуски конвеєрів
    # Запити лише за одним робочим процесом дають набагато щільніші дані:
    # у більшості репозиторіїв основний обсяг запусків припадає на службові
    # боти, які завжди завершуються успішно і нічого не додають до вибірки.
    base = (
        f"/repos/{repo}/actions/workflows/{workflow_file}/runs"
        if workflow_file else f"/repos/{repo}/actions/runs"
    )
    runs: list[dict] = []
    page = 1
    while len(runs) < max_runs:
        per_page = min(100, max_runs - len(runs))
        try:
            data, _ = gh.get(
                f"{base}?per_page={per_page}&page={page}&exclude_pull_requests=false"
            )
        except RateLimitExhausted as e:
            print(f"   [!] {e}")
            break
        batch = data.get("workflow_runs", [])
        if not batch:
            break
        runs.extend(batch)
        page += 1
        if len(batch) < per_page:
            break
    print(f"   отримано запусків: {len(runs)} (запитів витрачено {gh.used})")

    if workflow_filter:
        before = len(runs)
        runs = [r for r in runs if workflow_filter.lower() in (r.get("name") or "").lower()]
        print(f"   після фільтра «{workflow_filter}»: {len(runs)} із {before}")

    # --- 2. збереження комітів і конвеєрів
    seen_commits: set[str] = set()
    n_new_pipe = n_skip = 0
    pipelines_by_sha: dict[str, list[Pipeline]] = {}

    for r in runs:
        sha = r.get("head_sha")
        if not sha:
            continue
        if skip_cancelled and map_conclusion(r) == RunStatus.CANCELED:
            n_skip += 1
            continue
        head = r.get("head_commit") or {}
        actor = (r.get("actor") or {}).get("login") or (head.get("author") or {}).get("name") or "unknown"

        if sha not in seen_commits and db.get(Commit, sha) is None:
            db.add(Commit(
                sha=sha, project_id=project.id, author=actor,
                message=(head.get("message") or "")[:500],
                branch=r.get("head_branch") or project.default_branch,
                files_changed=0, additions=0, deletions=0, changed_paths=[],
                committed_at=parse_ts(head.get("timestamp")) or parse_ts(r.get("created_at"))
                             or datetime.now(timezone.utc),
            ))
            db.flush()
        seen_commits.add(sha)

        ext = str(r["id"])
        if db.scalars(select(Pipeline).where(
            Pipeline.project_id == project.id, Pipeline.external_id == ext
        )).first():
            n_skip += 1
            continue

        created = parse_ts(r.get("created_at"))
        started = parse_ts(r.get("run_started_at")) or created
        finished = parse_ts(r.get("updated_at"))
        duration = int((finished - started).total_seconds()) if (finished and started) else 0
        queue = int((started - created).total_seconds()) if (started and created) else 0

        p = Pipeline(
            project_id=project.id, commit_sha=sha, external_id=ext,
            ref=r.get("head_branch") or project.default_branch,
            status=map_conclusion(r),
            trigger=r.get("event") or "push",
            queued_at=created, started_at=started, finished_at=finished,
            duration_s=max(duration, 0), queue_time_s=max(queue, 0),
            optimized=False,
            plan_snapshot={
                "workflow": r.get("name"),
                "workflow_path": r.get("path"),
                "run_attempt": r.get("run_attempt", 1),
                "run_number": r.get("run_number"),
                "source": "github-actions",
            },
        )
        db.add(p)
        n_new_pipe += 1
        pipelines_by_sha.setdefault(sha, []).append(p)

    db.commit()
    print(f"   збережено конвеєрів: {n_new_pipe} (пропущено як дублікати: {n_skip})")

    # --- 3. деталі задач для частини конвеєрів
    n_jobs = 0
    if with_jobs > 0:
        completed = [r for r in runs if r.get("status") == "completed"][:with_jobs]
        print(f"   завантаження задач для {len(completed)} конвеєрів...")
        for i, r in enumerate(completed):
            try:
                data, _ = gh.get(f"/repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")
            except RateLimitExhausted as e:
                print(f"   [!] {e}")
                break
            pipe = db.scalars(select(Pipeline).where(
                Pipeline.project_id == project.id, Pipeline.external_id == str(r["id"])
            )).first()
            if pipe is None:
                continue
            for j in data.get("jobs", []):
                st, fin = parse_ts(j.get("started_at")), parse_ts(j.get("completed_at"))
                dur = int((fin - st).total_seconds()) if (st and fin) else 0
                name = (j.get("name") or "job")[:80]
                if db.scalars(select(Job).where(Job.pipeline_id == pipe.id, Job.name == name)).first():
                    continue
                db.add(Job(
                    pipeline_id=pipe.id, name=name,
                    stage=(j.get("workflow_name") or "test")[:40],
                    status=map_conclusion({"status": j.get("status"), "conclusion": j.get("conclusion")}),
                    runner=(j.get("runner_name") or "github-hosted")[:60],
                    started_at=st, finished_at=fin, duration_s=max(dur, 0),
                    cache_hit=False,
                ))
                n_jobs += 1
            if (i + 1) % 25 == 0:
                db.commit()
                print(f"      оброблено {i + 1}/{len(completed)}")
        db.commit()
        print(f"   збережено задач: {n_jobs}")

    # --- 4. статистика комітів (змінені файли)
    n_commits = 0
    if with_commits > 0:
        targets = [s for s in list(seen_commits)[:with_commits]]
        print(f"   завантаження статистики для {len(targets)} комітів...")
        for i, sha in enumerate(targets):
            c = db.get(Commit, sha)
            if c is None or c.files_changed:
                continue
            try:
                data, _ = gh.get(f"/repos/{repo}/commits/{sha}")
            except RateLimitExhausted as e:
                print(f"   [!] {e}")
                break
            except urllib.error.HTTPError:
                continue
            stats = data.get("stats") or {}
            files = data.get("files") or []
            c.additions = int(stats.get("additions", 0))
            c.deletions = int(stats.get("deletions", 0))
            c.changed_paths = [f.get("filename", "") for f in files][:300]
            c.files_changed = len(c.changed_paths)
            n_commits += 1
            if (i + 1) % 25 == 0:
                db.commit()
        db.commit()
        print(f"   доповнено комітів: {n_commits}")

    total = len(db.scalars(select(Pipeline).where(Pipeline.project_id == project.id)).all())
    failed = len(db.scalars(select(Pipeline).where(
        Pipeline.project_id == project.id, Pipeline.status == RunStatus.FAILED
    )).all())
    reruns = sum(1 for r in runs if (r.get("run_attempt") or 1) > 1)

    return {
        "project_id": project.id, "repo": repo, "pipelines": total,
        "failed": failed, "jobs": n_jobs, "commits_enriched": n_commits,
        "reruns": reruns, "requests_used": gh.used,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Імпорт реальної історії GitHub Actions")
    ap.add_argument("--repo", action="append", required=True,
                    help="репозиторій у форматі owner/name, можна вказати кілька разів")
    ap.add_argument("--runs", type=int, default=300, help="максимум запусків на репозиторій")
    ap.add_argument("--with-jobs", type=int, default=0, help="для скількох конвеєрів тягнути задачі")
    ap.add_argument("--with-commits", type=int, default=0, help="для скількох комітів тягнути статистику")
    ap.add_argument("--workflow", default=None, help="фільтр за назвою робочого процесу")
    ap.add_argument("--workflow-file", default=None,
                    help="файл робочого процесу, напр. tests.yml: тягнути лише його запуски")
    ap.add_argument("--list-workflows", action="store_true",
                    help="показати робочі процеси репозиторію і вийти")
    ap.add_argument("--skip-cancelled", action="store_true",
                    help="не зберігати скасовані запуски: це не результат виконання")
    ap.add_argument("--token", default=os.getenv("GITHUB_TOKEN"), help="токен GitHub")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    gh = GitHubClient(args.token)

    try:
        rl, _ = gh.get("/rate_limit")
        core = rl["resources"]["core"]
        print(f"Ліміт запитів GitHub: {core['remaining']} із {core['limit']}"
              f"{' (з токеном)' if args.token else ' (без токена)'}")
        if core["remaining"] < 10:
            print("Замало запитів. Задайте GITHUB_TOKEN або зачекайте відновлення ліміту.")
            return
    except Exception as e:
        print(f"Не вдалося перевірити ліміт: {e}")

    if args.list_workflows:
        for repo in args.repo:
            list_workflows(gh, repo)
        return

    results = []
    for repo in args.repo:
        try:
            results.append(import_repo(
                gh, db, repo, args.runs, args.with_jobs, args.with_commits, args.workflow,
                args.workflow_file, args.skip_cancelled,
            ))
        except RateLimitExhausted as e:
            print(f"[!] {e}")
            break
        except urllib.error.HTTPError as e:
            print(f"[!] {repo}: HTTP {e.code}")

    print("\n" + "=" * 68)
    print("ПІДСУМОК ІМПОРТУ РЕАЛЬНИХ ДАНИХ")
    print("=" * 68)
    for r in results:
        rate = r["failed"] / r["pipelines"] if r["pipelines"] else 0
        print(f"{r['repo']}")
        print(f"   проєкт id={r['project_id']}, конвеєрів {r['pipelines']}, "
              f"невдалих {r['failed']} ({rate:.1%})")
        print(f"   задач {r['jobs']}, комітів зі статистикою {r['commits_enriched']}, "
              f"повторних запусків {r['reruns']}")
    print(f"\nВитрачено запитів: {gh.used}, лишилося {gh.remaining}")
    print("\nДалі: python scripts/train_models.py --project <id>")
    db.close()


if __name__ == "__main__":
    main()
