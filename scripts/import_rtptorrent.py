"""Імпорт відкритого датасету RTPTorrent у базу системи.

ЧОМУ ЦЕ ПОТРІБНО. Синтетичний генератор (scripts/seed_synthetic.py) закладає
в дані ті самі залежності, які потім відновлює модель, тому результати на ньому
не можуть слугувати доказом корисності методу. RTPTorrent містить РЕАЛЬНІ
прогони тестів із TravisCI по 20 відкритих Java-проєктах за дев'ять років
розвитку, з реальними падіннями, що виникли в ході еволюції проєкту, а не були
внесені штучно.

    Toni Mattis, Patrick Rein, Falco Dürsch, Robert Hirschfeld.
    RTPTorrent: An Open-source Dataset for Evaluating Regression Test
    Prioritization. MSR '20. doi:10.5281/zenodo.3610998

СТРУКТУРА ДАТАСЕТУ (рис. 1 статті):
    <project>.csv       job_id, test, index, count, failures, errors, skipped[, duration]
    built_commits.csv   job_id, commit_id (sha1)
    repos/<project>     повна історія Git, звідки беремо автора, повідомлення,
                        час і перелік змінених файлів

ВІДПОВІДНІСТЬ СУТНОСТЕЙ:
    складання (build job) -> Pipeline + один Job
    тестовий клас         -> TestCase (suite = пакет, name = клас)
    рядок результату      -> TestResult
    коміт із Git          -> Commit із реальними changed_paths

ЗАСТЕРЕЖЕННЯ ПРО ГІЛКИ. Датасет не містить назви гілки складання, тому
запобіжник «не скорочувати набір на захищеній гілці» на цих даних вимкнено:
тут оцінюється сам метод відбору, а не операційна політика. Для проєктів,
імпортованих цим сценарієм, політика відбору створюється з branch_pattern="*"
і protect_default_branch=False. У пояснювальній записці це треба зазначити.
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import insert, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    CIProvider, Commit, Job, Pipeline, PlanMode, PolicyType, Policy, Project, Role,
    RunStatus, TestCase, TestResult, TestStatus, User,
)
from app.services.security import hash_password  # noqa: E402

CHUNK = 5000


# --------------------------------------------------------------- пошук файлів
@dataclass
class Layout:
    results_csv: Path
    commits_csv: Path
    repo: Path | None


def discover(root: Path, project: str) -> Layout:
    """Знайти файли датасету.

    Реальна структура архіву v1.1:
        rtp-torrent/<owner@repo>/<owner@repo>.csv   результати тестів
        rtp-torrent/tr_all_built_commits.csv        зв'язок складання -> коміт
        rtp-torrent/repo/<owner@repo>/              ГОЛИЙ репозиторій Git
    """
    results = [p for p in root.rglob(f"{project}.csv") if p.parent.name == project]
    if not results:
        results = [p for p in root.rglob(f"{project}.csv")]
    if not results:
        raise SystemExit(f"не знайдено {project}.csv у {root}")
    results_csv = min(results, key=lambda p: len(p.parts))

    commits = list(root.rglob("tr_all_built_commits.csv")) or list(root.rglob("built_commits.csv"))
    if not commits:
        raise SystemExit(f"не знайдено tr_all_built_commits.csv у {root}")
    commits_csv = min(commits, key=lambda p: len(p.parts))

    # репозиторії голі: ознака - файл HEAD і каталог objects поруч
    repo = None
    for cand in root.rglob(project):
        if cand.is_dir() and (cand / "HEAD").exists() and (cand / "objects").is_dir():
            repo = cand
            break
        if cand.is_dir() and (cand / ".git").exists():
            repo = cand
            break
    return Layout(results_csv, commits_csv, repo)


def list_projects(root: Path) -> list[str]:
    skip = {"built_commits", "builds", "projects", "baseline"}
    names = set()
    for p in root.rglob("*.csv"):
        if p.stem not in skip and p.stat().st_size > 100_000:
            names.add(p.stem)
    return sorted(names)


# ------------------------------------------------------------------ історія Git
@dataclass
class CommitInfo:
    sha: str
    author: str = "unknown"
    message: str = ""
    committed_at: datetime | None = None
    paths: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0


def load_git_history(repo: Path, limit_paths: int = 60) -> tuple[dict[str, CommitInfo], dict[str, str]]:
    """Один прохід по історії: автор, час, повідомлення і статистика змін.

    Викликати git на кожен коміт окремо надто повільно для десятків тисяч
    комітів, тому розбираємо суцільний вивід `git log --numstat`.
    """
    sep = "\x1e"
    cmd = [
        "git", "-C", str(repo), "log", "--all", "--numstat", "--no-renames",
        f"--format={sep}%H%x09%an%x09%aI%x09%s",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        print(f"   ! git log завершився з кодом {proc.returncode}: {proc.stderr[:200]}")
        return {}, {}

    out: dict[str, CommitInfo] = {}
    # Індекс «клас -> шлях» збираємо з усіх шляхів, що колись змінювалися.
    # Знімок HEAD для цього не годиться: проєкт міг перейти на іншу мову або
    # перебудувати структуру каталогів уже після періоду, який ми імпортуємо.
    java_idx: dict[str, str] = {}
    cur: CommitInfo | None = None
    for raw in proc.stdout.split(sep):
        if not raw.strip():
            continue
        lines = raw.splitlines()
        head = lines[0].split("\t")
        if len(head) < 3:
            continue
        sha, author, iso = head[0], head[1], head[2]
        msg = head[3] if len(head) > 3 else ""
        try:
            ts = datetime.fromisoformat(iso).astimezone(timezone.utc)
        except ValueError:
            ts = None
        cur = CommitInfo(sha=sha, author=author[:80], message=msg[:500], committed_at=ts)
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            cur.additions += int(add) if add.isdigit() else 0
            cur.deletions += int(dele) if dele.isdigit() else 0
            if len(cur.paths) < limit_paths:
                cur.paths.append(path)
            if path.endswith(".java"):
                cls = Path(path).stem
                if cls not in java_idx or "/test/" in path:
                    java_idx[cls] = path
        out[sha] = cur
    return out, java_idx


def index_java_files(repo: Path) -> dict[str, str]:
    """Відповідність «ім'я класу -> шлях у репозиторії» для поля file_path."""
    # У голому репозиторії немає робочої копії, тому ls-files не працює.
    proc = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        return {}
    idx: dict[str, str] = {}
    for path in proc.stdout.splitlines():
        if not path.endswith(".java"):
            continue
        cls = Path(path).stem
        # перший знайдений шлях; тестові каталоги мають пріоритет
        if cls not in idx or "/test/" in path:
            idx[cls] = path
    return idx


# ----------------------------------------------------------------- читання CSV
def load_built_commits(path: Path, wanted: set[int]) -> dict[int, str]:
    """job_id -> sha коміту. Файл спільний для всіх проєктів і великий,
    тому в пам'яті лишаємо лише потрібні складання."""
    out: dict[int, str] = {}
    with path.open(newline="", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (rdr.fieldnames or [])}
        job_c = cols.get("tr_job_id") or cols.get("job_id") or cols.get("travisjobid") or cols.get("job")
        sha_c = cols.get("git_commit_id") or cols.get("commit_id") or cols.get("commit") or cols.get("sha")
        if not job_c or not sha_c:
            raise SystemExit(f"{path.name}: не розпізнано колонки {rdr.fieldnames}")
        for row in rdr:
            try:
                jid = int(float(row[job_c]))
            except (ValueError, TypeError, KeyError):
                continue
            if jid not in wanted:
                continue
            sha = (row[sha_c] or "").strip()
            if sha:
                out[jid] = sha
    return out


@dataclass
class ResultRow:
    test: str
    index: int
    count: int
    failures: int
    errors: int
    skipped: int
    duration_s: float

    @property
    def status(self) -> TestStatus:
        if self.failures > 0 or self.errors > 0:
            return TestStatus.FAILED
        if self.count > 0 and self.skipped >= self.count:
            return TestStatus.SKIPPED
        return TestStatus.PASSED


def load_results(path: Path, min_tests: int) -> dict[int, list[ResultRow]]:
    """job_id -> рядки результатів. Читається потоково: файл великий."""
    per_job: dict[int, list[ResultRow]] = defaultdict(list)
    with path.open(newline="", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (rdr.fieldnames or [])}

        def col(*names, default=None):
            for n in names:
                if n in cols:
                    return cols[n]
            return default

        job_c = col("travisjobid", "job_id", "tr_job_id", "job")
        test_c = col("testname", "test", "test_name", "name")
        if not job_c or not test_c:
            raise SystemExit(f"{path.name}: не розпізнано колонки {rdr.fieldnames}")
        idx_c = col("index", "position", "order")
        cnt_c = col("count", "tests", "total")
        fail_c = col("failures", "failed")
        err_c = col("errors", "error")
        skip_c = col("skipped", "skip")
        dur_c = col("duration", "time", "elapsed", "duration_s")

        def num(row, c, default=0.0):
            if not c:
                return default
            v = (row.get(c) or "").strip()
            if not v:
                return default
            try:
                return float(v)
            except ValueError:
                return default

        for row in rdr:
            try:
                jid = int(float(row[job_c]))
            except (ValueError, TypeError, KeyError):
                continue
            name = (row.get(test_c) or "").strip()
            if not name:
                continue
            per_job[jid].append(ResultRow(
                test=name,
                index=int(num(row, idx_c, len(per_job[jid]))),
                count=int(num(row, cnt_c, 1)),
                failures=int(num(row, fail_c)),
                errors=int(num(row, err_c)),
                skipped=int(num(row, skip_c)),
                duration_s=num(row, dur_c, 0.0),
            ))

    return {j: rows for j, rows in per_job.items() if len(rows) >= min_tests}


# ------------------------------------------------------------------- допоміжне
def split_fqn(fqn: str) -> tuple[str, str]:
    """Java-клас -> (пакет, клас). Пакет обрізаємо справа: хвіст інформативніший."""
    fqn = fqn.strip()
    if "." in fqn:
        pkg, cls = fqn.rsplit(".", 1)
    else:
        pkg, cls = "<default>", fqn
    return pkg[-80:], cls[:160]


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


def ensure_project(db, name: str, owner_id: int) -> Project:
    project = db.scalars(select(Project).where(Project.name == name)).first()
    if project:
        return project
    project = Project(
        name=name,
        repo_url=f"https://github.com/{name}",
        ci_provider=CIProvider.GITHUB,
        external_id=f"rtptorrent:{name}",
        default_branch="master",
        plan_mode=PlanMode.DRY_RUN,
        owner_id=owner_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add_all([
        Policy(project_id=project.id, name="Пропускати конвеєр для змін лише в документації",
               type=PolicyType.SKIP_DOCS_ONLY, params={"keep_lint": True}, branch_pattern="*"),
        # Гілка в датасеті невідома, тому захист основної гілки вимкнено:
        # на історичних даних оцінюється метод відбору, а не операційна політика.
        Policy(project_id=project.id, name="Відбір тестів за бюджетом часу",
               type=PolicyType.TEST_SELECTION,
               params={"budget_ratio": 0.30, "budget_s": 900, "min_keep_ratio": 0.12,
                       "protect_default_branch": False},
               branch_pattern="*"),
        Policy(project_id=project.id, name="Розподіл тестів на шарди",
               type=PolicyType.SHARDING, params={"max_shards": 4, "target_s": 120},
               branch_pattern="*"),
        Policy(project_id=project.id, name="Впорядкування задач за ризиком",
               type=PolicyType.FAIL_FAST, params={"risk_threshold": 0.45}, branch_pattern="*"),
    ])
    db.commit()
    return project


# --------------------------------------------------------------------- імпорт
def reset_project(db, name: str) -> None:
    """Видалити проєкт разом з усіма даними: імпорт має бути відтворюваним."""
    project = db.scalars(select(Project).where(Project.name == name)).first()
    if project is None:
        return
    db.execute(Commit.__table__.delete().where(Commit.project_id == project.id))
    db.delete(project)      # конвеєри, задачі, тести й політики видаляються каскадом
    db.commit()
    print(f"   попередній імпорт видалено")


def import_project(db, root: Path, name: str, max_jobs: int, min_tests: int) -> None:
    lay = discover(root, name)
    print(f"   результати: {lay.results_csv}")
    print(f"   коміти:     {lay.commits_csv}")
    print(f"   репозиторій:{lay.repo or ' не знайдено (changed_paths будуть порожні)'}")

    per_job = load_results(lay.results_csv, min_tests)
    print(f"   складань із результатами: {len(per_job)}")

    job_sha = load_built_commits(lay.commits_csv, set(per_job))
    print(f"   зв'язок складання->коміт: {len(job_sha)}")

    hist, java_idx = load_git_history(lay.repo) if lay.repo else ({}, {})
    if not java_idx and lay.repo:
        java_idx = index_java_files(lay.repo)
    print(f"   комітів у Git: {len(hist)}, файлів .java: {len(java_idx)}")

    owner = ensure_users(db)
    project = ensure_project(db, name, owner.id)

    # прибираємо складання без відомого коміту: без змінених файлів немає ознак.
    # Обрізання за кількістю - саме тут, бо зв'язок із комітами покриває не всі
    # складання: якби ми брали найновіші наосліп, більшість відпала б.
    jobs = sorted(j for j in per_job if job_sha.get(j) in hist)
    print(f"   складань із відомим комітом: {len(jobs)}")
    if max_jobs and len(jobs) > max_jobs:
        jobs = jobs[-max_jobs:]
        print(f"   обмежено найновішими: {len(jobs)}")
    if not jobs:
        print("   ! немає жодного складання зі зв'язком до Git — пропущено")
        return

    # ---- тестові класи
    existing = {(tc.suite, tc.name): tc.id for tc in
                db.scalars(select(TestCase).where(TestCase.project_id == project.id))}
    seen: dict[tuple[str, str], list] = {}
    for j in jobs:
        for r in per_job[j]:
            key = split_fqn(r.test)
            seen.setdefault(key, []).append(r.duration_s)

    new_cases = []
    for (suite, cls), durs in seen.items():
        if (suite, cls) in existing:
            continue
        pos = [d for d in durs if d > 0]
        new_cases.append({
            "project_id": project.id, "suite": suite, "name": cls,
            "file_path": java_idx.get(cls, ""),
            "avg_duration_ms": (sum(pos) / len(pos) * 1000.0) if pos else 500.0,
            "total_runs": 0, "total_failures": 0,
        })
    if new_cases:
        db.execute(insert(TestCase), new_cases)
        db.commit()
    case_id = {(tc.suite, tc.name): tc.id for tc in
               db.scalars(select(TestCase).where(TestCase.project_id == project.id))}
    print(f"   тестових класів: {len(case_id)}")

    # ---- коміти, конвеєри, задачі, результати
    #
    # ОДИН КОМІТ - ОДИН КОНВЕЄР. У TravisCI один коміт породжує кілька
    # складань (матриця платформ і серверних частин). Якщо перетворювати
    # кожне складання на окремий конвеєр, вони отримують однакову позначку
    # часу, впорядкування стає неоднозначним, а результати складань-побратимів
    # того самого коміту протікають одне в одне як «минуле». Тому складання
    # одного коміту зводяться в один конвеєр з кількома задачами - саме так,
    # як це подано в моделі даних системи, і саме на цьому рівні формується
    # план.
    known_commits = set(db.scalars(select(Commit.sha).where(Commit.project_id == project.id)))
    runs = defaultdict(int)
    fails = defaultdict(int)
    last_fail: dict[int, datetime] = {}
    results_buf: list[dict] = []
    made = 0

    by_commit: dict[str, list[int]] = defaultdict(list)
    for j in jobs:
        by_commit[job_sha[j]].append(j)
    ordered = sorted(
        by_commit,
        key=lambda sha: (hist[sha].committed_at or datetime(2013, 1, 1, tzinfo=timezone.utc)),
    )
    print(f"   конвеєрів після зведення за комітами: {len(ordered)}")

    for n, sha in enumerate(ordered, 1):
        ci = hist[sha]
        job_ids = sorted(by_commit[sha])

        if sha not in known_commits:
            db.add(Commit(
                sha=sha, project_id=project.id, author=ci.author,
                message=ci.message, branch="master",
                files_changed=len(ci.paths), additions=ci.additions,
                deletions=ci.deletions, changed_paths=ci.paths,
                committed_at=ci.committed_at,
            ))
            known_commits.add(sha)
            db.flush()

        started = (ci.committed_at or datetime(2013, 1, 1, tzinfo=timezone.utc)) + timedelta(minutes=2)
        job_durs = []
        for j in job_ids:
            d = sum(r.duration_s for r in per_job[j])
            job_durs.append(d if d > 0 else len(per_job[j]) * 2.0)
        # Задачі матриці виконуються паралельно, тому тривалість конвеєра -
        # це найдовша задача, а не сума.
        dur = max(job_durs) if job_durs else 0.0
        failed = any(r.status == TestStatus.FAILED for j in job_ids for r in per_job[j])

        pipe = Pipeline(
            project_id=project.id, commit_sha=sha, external_id=str(job_ids[0]),
            ref="master", status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
            trigger="push", queued_at=ci.committed_at, started_at=started,
            finished_at=started + timedelta(seconds=dur),
            duration_s=int(dur), queue_time_s=120, optimized=False,
        )
        db.add(pipe)
        db.flush()

        for k, j in enumerate(job_ids):
            rows = per_job[j]
            j_failed = any(r.status == TestStatus.FAILED for r in rows)
            job = Job(
                pipeline_id=pipe.id, name=f"test:{k + 1}", stage="test",
                status=RunStatus.FAILED if j_failed else RunStatus.SUCCESS,
                runner="travis", started_at=started,
                finished_at=started + timedelta(seconds=job_durs[k]),
                duration_s=int(job_durs[k]),
            )
            db.add(job)
            db.flush()

            for r in sorted(rows, key=lambda x: x.index):
                cid = case_id.get(split_fqn(r.test))
                if cid is None:
                    continue
                st = r.status
                runs[cid] += 1
                if st == TestStatus.FAILED:
                    fails[cid] += 1
                    last_fail[cid] = started
                results_buf.append({
                    "job_id": job.id, "test_case_id": cid, "status": st,
                    "duration_ms": int(max(0.0, r.duration_s) * 1000),
                    "retries": 0, "error_message": "",
                })

        made += 1
        if len(results_buf) >= CHUNK:
            db.execute(insert(TestResult), results_buf)
            results_buf.clear()
            db.commit()
        if n % 200 == 0:
            print(f"      ...{n}/{len(ordered)} конвеєрів")

    if results_buf:
        db.execute(insert(TestResult), results_buf)
    db.commit()

    # ---- агрегати тестових класів
    for cid, r in runs.items():
        tc = db.get(TestCase, cid)
        if tc is None:
            continue
        tc.total_runs = r
        tc.total_failures = fails.get(cid, 0)
        tc.last_failed_at = last_fail.get(cid)
    db.commit()

    total_res = sum(runs.values())
    total_fail = sum(fails.values())
    print(f"   готово: {made} конвеєрів, {total_res} результатів, "
          f"{total_fail} падінь ({total_fail / total_res * 100:.2f}% від усіх запусків)")
    pf = sum(
        1 for sha in ordered
        if any(r.status == TestStatus.FAILED for j in by_commit[sha] for r in per_job[j])
    )
    print(f"   конвеєрів із падінням: {pf} ({pf / len(ordered) * 100:.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Імпорт датасету RTPTorrent")
    ap.add_argument("--root", required=True, help="каталог розпакованого датасету")
    ap.add_argument("--project", action="append", help="назва проєкту (можна кілька разів)")
    ap.add_argument("--list", action="store_true", help="показати доступні проєкти")
    ap.add_argument("--max-jobs", type=int, default=2000,
                    help="обмеження на кількість складань (беруться найновіші)")
    ap.add_argument("--min-tests", type=int, default=5,
                    help="пропускати складання, де менше за стільки тестів")
    ap.add_argument("--reset", action="store_true",
                    help="видалити попередній імпорт цього проєкту перед новим")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if args.list:
        for p in list_projects(root):
            print(" ", p)
        return
    if not args.project:
        raise SystemExit("вкажіть --project або --list")

    init_db()
    db = SessionLocal()
    try:
        for name in args.project:
            print(f"\n=== {name} ===")
            if args.reset:
                reset_project(db, name)
            import_project(db, root, name, args.max_jobs, args.min_tests)
    finally:
        db.close()


if __name__ == "__main__":
    main()
