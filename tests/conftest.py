"""Спільне налаштування тестів.

Набір тестів працює з тією самою базою, що й стенд: частина перевірок
спирається на накопичену історію синтетичного проєкту, і відтворювати її
для кожного запуску було б надто довго.

Платою за це є сміття: тести створюють власні коміти й прогони, які потім
з'являються в інтерфейсі поряд зі справжніми даними. На демонстрації це
виглядає як несправність, а в списку конвеєрів такі записи витісняють
реальні, бо мають найсвіжішу позначку часу.

Тому після завершення набору штучні записи прибираються. Розпізнати їх
просто: справжній ідентифікатор коміту завжди є сорока шістнадцятковими
символами, тоді як тести користуються читабельними позначками на кшталт
«perf000...» або «guard_main_0001».
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (Alert, Commit, ModelVersion, Pipeline, Project,
                        Recommendation)
from app.services.rate_limit import reset_all

REAL_SHA = re.compile(r"[0-9a-f]{40}")


def _synthetic_commits(db) -> list[Commit]:
    return [
        c for c in db.scalars(select(Commit))
        if not REAL_SHA.fullmatch(c.sha or "")
    ]


def _drop_test_projects(db) -> int:
    """Прибрати проєкти, створені перевірками вебхуків.

    Їхні імена починаються з «__test». Каскадом вилучаються прогони,
    політики й перелік перевірок; решта посилань на проєкт каскаду не має,
    тому прибирається явно. Інакше такі проєкти лишаються у списку вибору
    поряд зі справжніми — зокрема й на знімках інтерфейсу для записки.
    """
    projects = db.scalars(select(Project).where(Project.name.like("__test%"))).all()
    for pr in projects:
        for model in (ModelVersion, Recommendation, Alert):
            for row in db.scalars(select(model).where(model.project_id == pr.id)).all():
                db.delete(row)
        for c in db.scalars(select(Commit).where(Commit.project_id == pr.id)).all():
            db.delete(c)
        db.delete(pr)
    return len(projects)


@pytest.fixture(scope="session", autouse=True)
def clean_up_test_artifacts():
    """Прибрати за тестами створені ними коміти, прогони й проєкти."""
    yield

    db = SessionLocal()
    try:
        removed_pipelines = 0
        commits = _synthetic_commits(db)
        for c in commits:
            for p in db.scalars(select(Pipeline).where(Pipeline.commit_sha == c.sha)):
                db.delete(p)
                removed_pipelines += 1
            db.delete(c)
        removed_projects = _drop_test_projects(db)
        if commits or removed_projects:
            db.commit()
            print(f"\n[conftest] прибрано {len(commits)} тестових комітів, "
                  f"{removed_pipelines} прогонів і {removed_projects} проєктів")
    except Exception as exc:                      # прибирання не має валити набір
        db.rollback()
        print(f"\n[conftest] не вдалося прибрати тестові дані: {exc}")
    finally:
        db.close()


@pytest.fixture(autouse=True)
def reset_login_limits():
    """Обнулити облік невдалих входів перед кожною перевіркою.

    Усі перевірки звертаються до застосунку з одного й того самого ключа
    (TestClient), тому без обнулення невдалі спроби однієї перевірки
    замикали б вхід для наступних.
    """
    reset_all()
    yield
