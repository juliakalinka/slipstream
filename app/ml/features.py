"""Формування ознак для моделей прогнозування.

Ключовий принцип, який окремо описується в підрозділі 3.3 записки: для коміту,
зробленого в момент t, використовуються ЛИШЕ дані про конвеєри, що завершилися
до моменту t. Це унеможливлює витік майбутньої інформації в навчальну вибірку.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import PurePosixPath

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Commit, Job, Pipeline, RunStatus, TestCase, TestResult, TestStatus

DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_DIRS = {"docs", "doc", "documentation", "changelog"}
CONFIG_SUFFIXES = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".json"}
TEST_MARKERS = ("test", "spec", "__tests__")

# Назви ознак у фіксованому порядку. Порядок є частиною контракту моделі
# і зберігається разом із нею у реєстрі версій.
FAILURE_FEATURES = [
    "files_changed", "churn", "additions", "deletions",
    "n_modules", "docs_ratio", "config_ratio", "test_ratio", "code_ratio",
    "is_docs_only", "touches_tests",
    "author_fail_rate", "author_experience", "author_recent_fails",
    "module_fail_rate", "module_churn_30d",
    "prev_pipeline_failed", "project_fail_rate_7d",
    "hour_of_day", "day_of_week", "is_off_hours",
    "hours_since_prev_pipeline", "is_default_branch",
]


# Розбір шляху — чиста функція від рядка, і вона виявилася найгарячішим
# місцем видачі плану: переграючи історію, система розбирає ті самі кілька
# сотень шляхів тисячі разів. Запам'ятовування знімає цю роботу повністю.
@lru_cache(maxsize=16384)
def module_of(path: str) -> str:
    """Модуль = перші два сегменти шляху. Дає розумну гранулярність для більшості репозиторіїв."""
    parts = PurePosixPath(path).parts
    if not parts:
        return "<root>"
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


@lru_cache(maxsize=16384)
def classify_path(path: str) -> str:
    """Груба класифікація файлу: документація, конфігурація, тест або код."""
    p = PurePosixPath(path.lower())
    if p.suffix in DOC_SUFFIXES or any(d in p.parts for d in DOC_DIRS):
        return "docs"
    if any(m in path.lower() for m in TEST_MARKERS):
        return "test"
    if p.suffix in CONFIG_SUFFIXES:
        return "config"
    return "code"


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite повертає naive datetime; приводимо все до UTC-aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class HistoryStats:
    """Передрахована історія проєкту до заданого моменту часу.

    Рахується один раз на весь навчальний прохід і оновлюється інкрементально,
    щоб не робити O(n^2) запитів до БД.
    """
    author_runs: dict[str, int] = field(default_factory=dict)
    author_fails: dict[str, int] = field(default_factory=dict)
    author_recent: dict[str, list[int]] = field(default_factory=dict)
    module_runs: dict[str, int] = field(default_factory=dict)
    module_fails: dict[str, int] = field(default_factory=dict)
    module_churn: dict[str, int] = field(default_factory=dict)
    branch_last_status: dict[str, int] = field(default_factory=dict)
    branch_last_time: dict[str, datetime] = field(default_factory=dict)
    recent_results: list[int] = field(default_factory=list)

    def observe(self, commit: Commit, pipeline: Pipeline) -> None:
        """Додати завершений конвеєр в історію."""
        failed = 1 if pipeline.status == RunStatus.FAILED else 0
        a = commit.author
        self.author_runs[a] = self.author_runs.get(a, 0) + 1
        self.author_fails[a] = self.author_fails.get(a, 0) + failed
        self.author_recent.setdefault(a, []).append(failed)
        if len(self.author_recent[a]) > 10:
            self.author_recent[a].pop(0)
        for path in commit.changed_paths or []:
            m = module_of(path)
            self.module_runs[m] = self.module_runs.get(m, 0) + 1
            self.module_fails[m] = self.module_fails.get(m, 0) + failed
            self.module_churn[m] = self.module_churn.get(m, 0) + commit.churn
        self.branch_last_status[pipeline.ref] = failed
        self.branch_last_time[pipeline.ref] = _aware(pipeline.started_at) or _aware(commit.committed_at)
        self.recent_results.append(failed)
        if len(self.recent_results) > 50:
            self.recent_results.pop(0)

    def copy(self) -> "HistoryStats":
        """Незалежна копія.

        Потрібна тому, що зібрана історія кешується (див. `load_history`), а
        об'єкт змінюваний: `observe` доповнює його. Якби той, хто звертається,
        одержував сам кешований об'єкт, одне випадкове спостереження
        зіпсувало б історію для всіх наступних запитів.
        """
        c = HistoryStats()
        c.author_runs = dict(self.author_runs)
        c.author_fails = dict(self.author_fails)
        c.author_recent = {k: list(v) for k, v in self.author_recent.items()}
        c.module_runs = dict(self.module_runs)
        c.module_fails = dict(self.module_fails)
        c.module_churn = dict(self.module_churn)
        c.branch_last_status = dict(self.branch_last_status)
        c.branch_last_time = dict(self.branch_last_time)
        c.recent_results = list(self.recent_results)
        return c


def build_commit_features(
    commit: Commit,
    branch: str,
    hist: HistoryStats,
    default_branch: str = "main",
) -> dict[str, float]:
    """Вектор ознак рівня коміту для моделі прогнозу збою."""
    paths = commit.changed_paths or []
    n = max(len(paths), 1)
    kinds = [classify_path(p) for p in paths]
    modules = {module_of(p) for p in paths}

    docs = kinds.count("docs") / n
    config = kinds.count("config") / n
    tests = kinds.count("test") / n
    code = kinds.count("code") / n

    a = commit.author
    a_runs = hist.author_runs.get(a, 0)
    a_fails = hist.author_fails.get(a, 0)
    a_recent = hist.author_recent.get(a, [])

    m_runs = sum(hist.module_runs.get(m, 0) for m in modules)
    m_fails = sum(hist.module_fails.get(m, 0) for m in modules)
    m_churn = sum(hist.module_churn.get(m, 0) for m in modules)

    ts = _aware(commit.committed_at) or datetime.now(timezone.utc)
    prev_time = hist.branch_last_time.get(branch)
    hours_since = (ts - prev_time).total_seconds() / 3600 if prev_time else 24.0

    recent = hist.recent_results
    return {
        "files_changed": float(commit.files_changed),
        "churn": float(commit.churn),
        "additions": float(commit.additions),
        "deletions": float(commit.deletions),
        "n_modules": float(len(modules)),
        "docs_ratio": docs,
        "config_ratio": config,
        "test_ratio": tests,
        "code_ratio": code,
        "is_docs_only": 1.0 if docs == 1.0 else 0.0,
        "touches_tests": 1.0 if tests > 0 else 0.0,
        "author_fail_rate": (a_fails / a_runs) if a_runs else 0.25,
        "author_experience": math.log1p(a_runs),
        "author_recent_fails": (sum(a_recent) / len(a_recent)) if a_recent else 0.25,
        "module_fail_rate": (m_fails / m_runs) if m_runs else 0.25,
        "module_churn_30d": math.log1p(m_churn),
        "prev_pipeline_failed": float(hist.branch_last_status.get(branch, 0)),
        "project_fail_rate_7d": (sum(recent) / len(recent)) if recent else 0.25,
        "hour_of_day": float(ts.hour),
        "day_of_week": float(ts.weekday()),
        "is_off_hours": 1.0 if (ts.hour < 9 or ts.hour > 19 or ts.weekday() >= 5) else 0.0,
        "hours_since_prev_pipeline": min(hours_since, 168.0),
        "is_default_branch": 1.0 if branch == default_branch else 0.0,
    }


def features_to_vector(feats: dict[str, float], names: list[str] | None = None) -> list[float]:
    names = names or FAILURE_FEATURES
    return [float(feats.get(k, 0.0)) for k in names]


# ---------------------------------------------------------------- ознаки тестів
DURATION_FEATURES = [
    "n_tests", "hist_mean_duration", "hist_std_duration", "hist_max_duration",
    "files_changed", "churn", "cache_hit", "stage_build", "stage_test",
    "stage_lint", "stage_deploy", "is_sharded",
]


def build_job_features(
    job_name: str,
    stage: str,
    n_tests: int,
    commit: Commit,
    job_hist: dict[str, list[float]],
    cache_hit: bool = False,
    is_sharded: bool = False,
) -> dict[str, float]:
    """Вектор ознак рівня задачі для регресійної моделі тривалості."""
    h = job_hist.get(job_name, [])
    mean = sum(h) / len(h) if h else 60.0
    var = sum((x - mean) ** 2 for x in h) / len(h) if len(h) > 1 else 0.0
    return {
        "n_tests": float(n_tests),
        "hist_mean_duration": mean,
        "hist_std_duration": math.sqrt(var),
        "hist_max_duration": max(h) if h else mean,
        "files_changed": float(commit.files_changed),
        "churn": float(commit.churn),
        "cache_hit": 1.0 if cache_hit else 0.0,
        "stage_build": 1.0 if stage == "build" else 0.0,
        "stage_test": 1.0 if stage == "test" else 0.0,
        "stage_lint": 1.0 if stage == "lint" else 0.0,
        "stage_deploy": 1.0 if stage == "deploy" else 0.0,
        "is_sharded": 1.0 if is_sharded else 0.0,
    }


# ────────────────────────────────────────────── кеш зібраної історії
#
# ЧОМУ ВІН ПОТРІБЕН. Видача плану — блокуючий виклик: конвеєр стоїть і чекає
# на відповідь. А зібрати історію означає переграти ВСІ завершені конвеєри
# проєкту від початку, бо ознаки автора й модуля є накопичувальними. Робота
# однакова для всіх запитів між двома завершеннями конвеєрів, тому
# переграється вона марно: на історії з кількох тисяч прогонів це секунди.
#
# ЧИМ КЕШ ЗНЕЦІНЮЄТЬСЯ. Не часом, а відпечатком стану: кількістю завершених
# прогонів, найсвіжішими позначками часу й кількістю невдалих. Перші три
# ловлять появу нового прогону, остання — зміну наслідку вже наявного
# (повторний запуск, уточнення від CI-сервера). Відпечаток — один
# агрегувальний запит, він на три порядки дешевший за переграння.
#
# НАВЧАННЯ КЕШЕМ НЕ КОРИСТУЄТЬСЯ. Там історія потрібна станом на момент
# кожного коміту (`until`), і саме це не дає майбутнім даним потрапити у
# вибірку. Кешується лише повна історія, тобто випадок видачі плану.
_HISTORY_CACHE: "OrderedDict[int, tuple[tuple, HistoryStats]]" = OrderedDict()
_JOB_CACHE: "OrderedDict[int, tuple[tuple, dict[str, list[float]]]]" = OrderedDict()
_CACHE_PROJECTS = 32


def _history_fingerprint(db: Session, project_id: int) -> tuple:
    row = db.execute(
        select(
            func.count(Pipeline.id),
            func.max(Pipeline.started_at),
            func.max(Pipeline.finished_at),
            func.sum(case((Pipeline.status == RunStatus.FAILED, 1), else_=0)),
        )
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.status.in_([RunStatus.SUCCESS, RunStatus.FAILED]))
    ).one()
    return tuple(str(v) for v in row)


def _job_fingerprint(db: Session, project_id: int) -> tuple:
    row = db.execute(
        select(func.count(Job.id), func.max(Job.id), func.sum(Job.duration_s))
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project_id)
    ).one()
    return tuple(str(v) for v in row)


def _remember(cache: OrderedDict, project_id: int, key: tuple, value) -> None:
    cache[project_id] = (key, value)
    cache.move_to_end(project_id)
    while len(cache) > _CACHE_PROJECTS:
        cache.popitem(last=False)


def clear_history_cache() -> None:
    """Скинути кеш історії. Потрібне тестам і після масового імпорту."""
    _HISTORY_CACHE.clear()
    _JOB_CACHE.clear()


def _replay_history(db: Session, project_id: int, until: datetime | None) -> HistoryStats:
    stmt = (
        select(Pipeline, Commit)
        .join(Commit, Pipeline.commit_sha == Commit.sha)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.status.in_([RunStatus.SUCCESS, RunStatus.FAILED]))
        .order_by(Pipeline.started_at)
    )
    if until is not None:
        stmt = stmt.where(Pipeline.finished_at < until)
    hist = HistoryStats()
    for pipeline, commit in db.execute(stmt).all():
        hist.observe(commit, pipeline)
    return hist


def load_history(db: Session, project_id: int, until: datetime | None = None) -> HistoryStats:
    """Відтворити історію проєкту станом на момент `until` (або повну).

    Повна історія кешується до появи нових даних; зріз на момент `until`
    рахується завжди, бо він належить навчанню.
    """
    if until is not None:
        return _replay_history(db, project_id, until)

    key = _history_fingerprint(db, project_id)
    cached = _HISTORY_CACHE.get(project_id)
    if cached is not None and cached[0] == key:
        return cached[1].copy()

    hist = _replay_history(db, project_id, None)
    _remember(_HISTORY_CACHE, project_id, key, hist)
    return hist.copy()


def _replay_job_durations(db: Session, project_id: int,
                          until: datetime | None) -> dict[str, list[float]]:
    stmt = (
        select(Job.name, Job.duration_s)
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project_id)
        .where(Job.duration_s > 0)
        .order_by(Job.id)
    )
    if until is not None:
        stmt = stmt.where(Pipeline.finished_at < until)
    out: dict[str, list[float]] = {}
    for name, dur in db.execute(stmt).all():
        base = name.split(" ")[0]  # "unit-tests 2/4" -> "unit-tests"
        out.setdefault(base, []).append(float(dur))
    return out


def job_duration_history(db: Session, project_id: int,
                         until: datetime | None = None) -> dict[str, list[float]]:
    """Історичні тривалості за назвою задачі.

    Кешується з тих самих причин і за тим самим правилом, що й історія
    конвеєрів: для видачі плану потрібні всі задачі проєкту, а змінюються
    вони лише з появою нових прогонів.
    """
    if until is not None:
        return _replay_job_durations(db, project_id, until)

    key = _job_fingerprint(db, project_id)
    cached = _JOB_CACHE.get(project_id)
    if cached is not None and cached[0] == key:
        return {k: list(v) for k, v in cached[1].items()}

    out = _replay_job_durations(db, project_id, None)
    _remember(_JOB_CACHE, project_id, key, out)
    return {k: list(v) for k, v in out.items()}
