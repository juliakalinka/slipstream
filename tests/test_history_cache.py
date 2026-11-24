"""Кеш зібраної історії проєкту.

Видача плану — блокуючий виклик: конвеєр стоїть і чекає. Збирання історії
означає переграти всі завершені прогони від початку, і ця робота однакова
для всіх запитів між двома завершеннями конвеєрів. Кеш її знімає, але
створює ризик, гірший за повільність: прогноз на застарілій історії. Тут
перевіряється саме знецінення кешу — у кожному випадку, коли історія
змінилася, а кількість рядків могла й не змінитися.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.ml.features import clear_history_cache, load_history
from app.models import Commit, Pipeline, Project, RunStatus

NAME = "__test_history_cache"
T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    db = SessionLocal()
    p = Project(name=NAME, repo_url="http://example.com/h",
                ci_provider="gitlab", default_branch="main")
    db.add(p)
    db.commit()
    db.refresh(p)
    clear_history_cache()
    yield db, p

    for pl in db.scalars(select(Pipeline).where(Pipeline.project_id == p.id)).all():
        db.delete(pl)
    db.commit()
    for c in db.scalars(select(Commit).where(Commit.project_id == p.id)).all():
        db.delete(c)
    db.delete(p)
    db.commit()
    db.close()
    clear_history_cache()


def _run(db, project_id: int, n: int, author: str, status: RunStatus,
         paths: list[str] | None = None) -> Pipeline:
    sha = f"histcache_{n:04d}"
    started = T0 + timedelta(hours=n)
    db.add(Commit(sha=sha, project_id=project_id, author=author, branch="main",
                  changed_paths=paths or ["src/auth/tokens.py"], additions=10,
                  deletions=2, files_changed=1, committed_at=started))
    pl = Pipeline(project_id=project_id, commit_sha=sha, ref="main", status=status,
                  started_at=started, finished_at=started + timedelta(minutes=5))
    db.add(pl)
    db.commit()
    db.refresh(pl)
    return pl


def test_a_new_run_invalidates_the_cache(repo):
    db, p = repo
    _run(db, p.id, 1, "olena", RunStatus.SUCCESS)
    assert load_history(db, p.id).author_runs == {"olena": 1}

    _run(db, p.id, 2, "andrii", RunStatus.SUCCESS)

    hist = load_history(db, p.id)
    assert hist.author_runs == {"olena": 1, "andrii": 1}, "кеш віддав застарілу історію"


def test_a_changed_outcome_invalidates_the_cache(repo):
    """Найпідступніший випадок: рядків не більшає, змінюється наслідок.

    Так буває при повторному запуску й при уточненні від CI-сервера. Якби
    відпечаток складався лише з кількості прогонів і позначок часу, частота
    падінь автора лишилася б хибною до наступного прогону.
    """
    db, p = repo
    pl = _run(db, p.id, 1, "olena", RunStatus.SUCCESS)
    _run(db, p.id, 2, "olena", RunStatus.SUCCESS)
    assert load_history(db, p.id).author_fails == {"olena": 0}

    pl.status = RunStatus.FAILED
    db.commit()

    assert load_history(db, p.id).author_fails == {"olena": 1}, \
        "зміну наслідку прогону кеш не помітив"


def test_the_caller_gets_a_copy(repo):
    """Історія змінювана: `observe` доповнює її.

    Якби повертався сам кешований об'єкт, одне спостереження того, хто
    звертається, зіпсувало б історію всім наступним запитам.
    """
    db, p = repo
    pl = _run(db, p.id, 1, "olena", RunStatus.SUCCESS)

    first = load_history(db, p.id)
    first.observe(db.get(Commit, pl.commit_sha), pl)        # навмисна домішка
    assert first.author_runs["olena"] == 2

    assert load_history(db, p.id).author_runs["olena"] == 1, "кеш зіпсовано збоку"


def test_a_cutoff_slice_is_never_served_from_the_cache(repo):
    """Зріз історії належить навчанню: саме він не дає витоку майбутнього."""
    db, p = repo
    _run(db, p.id, 1, "olena", RunStatus.SUCCESS)
    _run(db, p.id, 5, "andrii", RunStatus.SUCCESS)

    full = load_history(db, p.id)                            # повна -> у кеш
    sliced = load_history(db, p.id, until=T0 + timedelta(hours=3))

    assert full.author_runs == {"olena": 1, "andrii": 1}
    assert sliced.author_runs == {"olena": 1}, "зріз віддано з кешу повної історії"


def test_the_cache_gives_the_same_answer_as_a_full_replay(repo):
    """Кеш не має права змінювати ЗНАЧЕННЯ, лише час відповіді."""
    db, p = repo
    for i, (author, status) in enumerate(
            [("olena", RunStatus.SUCCESS), ("andrii", RunStatus.FAILED),
             ("olena", RunStatus.FAILED), ("iryna", RunStatus.SUCCESS)], start=1):
        _run(db, p.id, i, author, status, paths=[f"src/mod{i}/a.py", "README.md"])

    cached = load_history(db, p.id)
    clear_history_cache()
    replayed = load_history(db, p.id)

    assert cached.author_runs == replayed.author_runs
    assert cached.author_fails == replayed.author_fails
    assert cached.module_runs == replayed.module_runs
    assert cached.module_fails == replayed.module_fails
    assert cached.module_churn == replayed.module_churn
    assert cached.recent_results == replayed.recent_results
    assert cached.branch_last_status == replayed.branch_last_status
