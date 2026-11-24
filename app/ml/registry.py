"""Реєстр версій моделей: збереження артефактів і облік у БД.

Відповідає підрозділу 4.3 записки. Кожна навчена модель отримує версію,
зберігається на диск і реєструється рядком у таблиці model_versions разом
із метриками якості та переліком ознак.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ModelType, ModelVersion


def _dir() -> Path:
    p = Path(settings.models_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def next_version(db: Session, project_id: int | None, mtype: ModelType) -> str:
    n = db.scalars(
        select(ModelVersion).where(
            ModelVersion.project_id == project_id, ModelVersion.type == mtype
        )
    ).all()
    return f"v{len(n) + 1}"


def save(
    db: Session,
    project_id: int | None,
    mtype: ModelType,
    estimator: Any,
    feature_names: list[str],
    metrics: dict,
    algorithm: str,
    train_samples: int,
    activate: bool = True,
) -> ModelVersion:
    version = next_version(db, project_id, mtype)
    path = _dir() / f"p{project_id or 0}_{mtype.value}_{version}.joblib"
    joblib.dump({"estimator": estimator, "features": feature_names}, path)

    if activate:
        for old in db.scalars(
            select(ModelVersion).where(
                ModelVersion.project_id == project_id,
                ModelVersion.type == mtype,
                ModelVersion.is_active.is_(True),
            )
        ).all():
            old.is_active = False

    mv = ModelVersion(
        project_id=project_id,
        type=mtype,
        version=version,
        algorithm=algorithm,
        artifact_path=str(path),
        metrics=metrics,
        feature_names=feature_names,
        train_samples=train_samples,
        trained_at=datetime.now(timezone.utc),
        is_active=activate,
    )
    db.add(mv)
    db.commit()
    db.refresh(mv)
    return mv


def active(db: Session, project_id: int | None, mtype: ModelType) -> ModelVersion | None:
    return db.scalars(
        select(ModelVersion)
        .where(
            ModelVersion.project_id == project_id,
            ModelVersion.type == mtype,
            ModelVersion.is_active.is_(True),
        )
        .order_by(ModelVersion.trained_at.desc())
    ).first()


_cache: dict[str, tuple[Any, list[str]]] = {}


def load(mv: ModelVersion) -> tuple[Any, list[str]]:
    """Завантажити артефакт моделі з кешуванням у пам'яті процесу."""
    key = mv.artifact_path
    if key not in _cache:
        blob = joblib.load(key)
        _cache[key] = (blob["estimator"], blob["features"])
    return _cache[key]


def clear_cache() -> None:
    _cache.clear()
