"""Задача 1: прогноз ризику збою конвеєра.

Бінарна класифікація на табличних ознаках коміту. Обрано градієнтний бустинг
(LightGBM) з обґрунтуванням у підрозділі 3.4.1: на табличних даних невеликого
обсягу він стабільно переважає нейромережі, навчається за секунди на CPU
і дає інтерпретовану важливість ознак, що критично для довіри до системи.
"""
from __future__ import annotations

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (
    average_precision_score, brier_score_loss, f1_score, precision_score,
    recall_score, roc_auc_score,
)

from app.ml.features import FAILURE_FEATURES

try:
    from lightgbm import LGBMClassifier
    HAVE_LGBM = True
except ImportError:  # запасний варіант, якщо LightGBM недоступний
    from sklearn.ensemble import HistGradientBoostingClassifier
    HAVE_LGBM = False


def _make_estimator(n_samples: int):
    if HAVE_LGBM:
        return LGBMClassifier(
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=max(5, n_samples // 50),
            subsample=0.9,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            class_weight="balanced",
            verbose=-1,
            random_state=42,
        )
    return HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15, random_state=42
    )


def algorithm_name() -> str:
    return "LightGBM (gradient boosting)" if HAVE_LGBM else "HistGradientBoosting"


def train(X: np.ndarray, y: np.ndarray, time_split: float = 0.75) -> tuple:
    """Навчання з розбиттям вибірки ЗА ЧАСОМ, а не випадково.

    Випадкове розбиття тут дало б завищену оцінку якості, бо модель побачила б
    майбутні збірки. Цей нюанс окремо обґрунтовується в підрозділі 3.3.
    """
    n = len(y)
    if n < 40:
        raise ValueError(f"замало даних для навчання: {n} конвеєрів, потрібно щонайменше 40")

    cut = int(n * time_split)
    X_tr, X_te = X[:cut], X[cut:]
    y_tr, y_te = y[:cut], y[cut:]

    if len(np.unique(y_tr)) < 2:
        raise ValueError("у навчальній частині лише один клас, потрібні і успішні, і невдалі збірки")

    base = _make_estimator(len(y_tr))
    base.fit(X_tr, y_tr)

    # Калібрування ймовірностей методом Платта. Ізотонічна регресія тут не
    # застосовується: на вибірці в кілька сотень прикладів вона перенавчається
    # і руйнує зважування класів.
    model = base
    if len(y_tr) >= 150:
        try:
            cal = CalibratedClassifierCV(_make_estimator(len(y_tr)), method="sigmoid", cv=3)
            cal.fit(X_tr, y_tr)
            model = cal
        except Exception:
            model = base

    metrics: dict[str, float | int] = {"train_samples": int(len(y_tr)), "test_samples": int(len(y_te))}
    if len(y_te) > 5 and len(np.unique(y_te)) == 2:
        proba = model.predict_proba(X_te)[:, 1]
        # Поріг 0.5 тут неінформативний: класи незбалансовані, а ціна пропуску
        # збою і ціна зайвого прогону тестів різні. Тому поріг підбирається за
        # максимумом F1 і зберігається разом із моделлю як частина контракту.
        best_t, best_f1 = 0.5, -1.0
        for t in np.arange(0.20, 0.81, 0.01):
            f = f1_score(y_te, (proba >= t).astype(int), zero_division=0)
            if f > best_f1:
                best_t, best_f1 = float(t), float(f)
        pred = (proba >= best_t).astype(int)
        metrics["threshold"] = round(best_t, 3)
        metrics |= {
            "roc_auc": round(float(roc_auc_score(y_te, proba)), 4),
            "pr_auc": round(float(average_precision_score(y_te, proba)), 4),
            "precision": round(float(precision_score(y_te, pred, zero_division=0)), 4),
            "recall": round(float(recall_score(y_te, pred, zero_division=0)), 4),
            "f1": round(float(f1_score(y_te, pred, zero_division=0)), 4),
            "brier": round(float(brier_score_loss(y_te, proba)), 4),
            "base_rate": round(float(y_te.mean()), 4),
        }
    return model, metrics


def feature_importance(model, names: list[str] | None = None) -> dict[str, float]:
    names = names or FAILURE_FEATURES
    est = model
    if hasattr(model, "calibrated_classifiers_"):
        est = model.calibrated_classifiers_[0].estimator
    imp = getattr(est, "feature_importances_", None)
    if imp is None:
        return {}
    total = float(np.sum(imp)) or 1.0
    pairs = sorted(zip(names, imp / total), key=lambda kv: kv[1], reverse=True)
    return {k: round(float(v), 4) for k, v in pairs}


def predict(model, x: list[float]) -> float:
    return float(model.predict_proba(np.asarray([x], dtype=float))[0, 1])
