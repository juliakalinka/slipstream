"""Захист періодичного перенавчання від тихого погіршення моделі.

Реєстр робить кожну нову версію активною одразу. Для запуску вручну це
нормально: людина бачить метрики. Але за розкладом навчання відбувається
вночі, і гірша модель стала б робочою сама собою — саме це й перевіряється.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ModelType, ModelVersion, Project
from scripts import retrain_all as ra

PROJECT_NAME = "__test_retrain_guard"


@pytest.fixture
def project():
    db = SessionLocal()
    p = Project(name=PROJECT_NAME, repo_url="http://example.com/x",
                ci_provider="gitlab", default_branch="main")
    db.add(p)
    db.commit()
    db.refresh(p)
    yield db, p

    for mv in db.scalars(select(ModelVersion).where(ModelVersion.project_id == p.id)).all():
        db.delete(mv)
    db.delete(p)
    db.commit()
    db.close()


def _version(db, project_id: int, version: str, roc_auc: float, active: bool) -> ModelVersion:
    mv = ModelVersion(project_id=project_id, type=ModelType.FAILURE, version=version,
                      algorithm="LightGBM", metrics={"roc_auc": roc_auc},
                      feature_names=["a"], train_samples=100, is_active=active)
    db.add(mv)
    db.commit()
    db.refresh(mv)
    return mv


def test_a_worse_model_is_rolled_back(project):
    db, p = project
    old = _version(db, p.id, "v1", 0.90, active=False)
    before = {ModelType.FAILURE: (old.id, 0.90)}
    new = _version(db, p.id, "v2", 0.70, active=True)

    reverted = ra._guard(db, p.id, before, tolerance=0.03)

    db.refresh(old); db.refresh(new)
    assert old.is_active is True, "попередню версію не повернено"
    assert new.is_active is False, "гірша версія лишилася активною"
    assert reverted and "v1" in reverted[0]


def test_a_small_drop_is_tolerated(project):
    """Коливання метрики на нових даних — не підстава для відкату."""
    db, p = project
    old = _version(db, p.id, "v1", 0.90, active=False)
    before = {ModelType.FAILURE: (old.id, 0.90)}
    new = _version(db, p.id, "v2", 0.885, active=True)

    assert ra._guard(db, p.id, before, tolerance=0.03) == []

    db.refresh(new)
    assert new.is_active is True


def test_a_better_model_stays(project):
    db, p = project
    old = _version(db, p.id, "v1", 0.90, active=False)
    before = {ModelType.FAILURE: (old.id, 0.90)}
    new = _version(db, p.id, "v2", 0.95, active=True)

    assert ra._guard(db, p.id, before, tolerance=0.03) == []

    db.refresh(new)
    assert new.is_active is True


def test_the_first_training_has_nothing_to_compare_with(project):
    db, p = project
    new = _version(db, p.id, "v1", 0.51, active=True)

    assert ra._guard(db, p.id, {}, tolerance=0.03) == []

    db.refresh(new)
    assert new.is_active is True, "перше навчання не має відкочуватися"


def test_new_runs_are_counted_from_the_last_training(project):
    """Без приросту історії перенавчання не має сенсу — це і перевіряється."""
    db, p = project
    _version(db, p.id, "v1", 0.90, active=True)

    count, last_at = ra._new_pipelines_since_last_training(db, p.id)

    assert count == 0, "конвеєрів після навчання не додавалося"
    assert last_at != "ніколи"
