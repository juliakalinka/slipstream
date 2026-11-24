"""Міграції бази даних мають описувати рівно ту саму схему, що й моделі ORM.

Ці перевірки закривають конкретну помилку, що вже траплялася в роботі: стовпці
`order_dependent` і `flaky_category` були додані в моделі та в робочу базу
командою ALTER TABLE вручну, тож на чистій машині схема відтворювалася вже
іншою. Тест ловить будь-яке таке розходження.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic(tmp_db: Path, *args: str) -> subprocess.CompletedProcess:
    """Запустити alembic на тимчасовій базі, не чіпаючи робочу."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "DATABASE_URL": f"sqlite:///{tmp_db}",
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "migration_check.db"


def test_upgrade_creates_every_orm_table(fresh_db: Path) -> None:
    """Після `upgrade head` у базі є всі дванадцять таблиць зі схеми Рис. 4.1."""
    from app.db import Base
    from app import models  # noqa: F401  реєструє таблиці в метаданих

    result = _alembic(fresh_db, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{fresh_db}")
    actual = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()

    assert actual == set(Base.metadata.tables), (
        f"немає в базі: {set(Base.metadata.tables) - actual}; "
        f"зайві в базі: {actual - set(Base.metadata.tables)}"
    )
    assert len(actual) == 13


def test_migrations_match_orm_models(fresh_db: Path) -> None:
    """`alembic check` не бачить різниці між міграціями та моделями.

    Саме ця перевірка не дає повторитися ручному ALTER TABLE: якщо хтось
    змінить модель і не напише міграцію, тест упаде.
    """
    assert _alembic(fresh_db, "upgrade", "head").returncode == 0

    result = _alembic(fresh_db, "check")
    assert result.returncode == 0, (
        "схема ORM розійшлася з міграціями — потрібна нова ревізія:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "No new upgrade operations" in (result.stdout + result.stderr)


def test_downgrade_removes_everything(fresh_db: Path) -> None:
    """Міграції оборотні: `downgrade base` лишає порожню базу."""
    assert _alembic(fresh_db, "upgrade", "head").returncode == 0

    result = _alembic(fresh_db, "downgrade", "base")
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{fresh_db}")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert remaining == set()


def test_order_dependency_columns_arrive_in_second_revision(fresh_db: Path) -> None:
    """Ревізія 0001 ще не знає про залежність від порядку, 0002 її додає."""
    assert _alembic(fresh_db, "upgrade", "0001").returncode == 0

    engine = create_engine(f"sqlite:///{fresh_db}")
    cols_0001 = {c["name"] for c in inspect(engine).get_columns("test_cases")}
    engine.dispose()
    assert "order_dependent" not in cols_0001
    assert "flaky_category" not in cols_0001

    assert _alembic(fresh_db, "upgrade", "head").returncode == 0

    engine = create_engine(f"sqlite:///{fresh_db}")
    cols_head = {c["name"] for c in inspect(engine).get_columns("test_cases")}
    indexes = {i["name"] for i in inspect(engine).get_indexes("test_cases")}
    engine.dispose()
    assert {"order_dependent", "flaky_category"} <= cols_head
    assert "ix_test_cases_order_dependent" in indexes


def test_problem_reports_arrive_in_third_revision(fresh_db: Path) -> None:
    """Ревізія 0003 додає таблицю повідомлень і розширює стовпець маркера."""
    assert _alembic(fresh_db, "upgrade", "0002").returncode == 0

    engine = create_engine(f"sqlite:///{fresh_db}")
    insp = inspect(engine)
    assert "problem_reports" not in insp.get_table_names()
    token_0002 = next(c for c in insp.get_columns("projects") if c["name"] == "access_token")
    engine.dispose()
    assert token_0002["type"].length == 255

    assert _alembic(fresh_db, "upgrade", "head").returncode == 0

    engine = create_engine(f"sqlite:///{fresh_db}")
    insp = inspect(engine)
    assert "problem_reports" in insp.get_table_names()
    token_head = next(c for c in insp.get_columns("projects") if c["name"] == "access_token")
    engine.dispose()
    assert token_head["type"].length == 512
