"""Задача 2: прогноз тривалості задачі конвеєра.

Регресія потрібна двічі: для рівномірного розподілу тестів на шарди
(вага в задачі пакування) і як базова лінія для виявлення аномалій.
"""
from __future__ import annotations

import numpy as np
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, r2_score

from app.ml.features import DURATION_FEATURES

try:
    from lightgbm import LGBMRegressor
    HAVE_LGBM = True
except ImportError:
    from sklearn.ensemble import HistGradientBoostingRegressor
    HAVE_LGBM = False


def _make_estimator(n: int):
    if HAVE_LGBM:
        return LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=15,
            min_child_samples=max(5, n // 50), subsample=0.9, subsample_freq=1,
            colsample_bytree=0.8, reg_lambda=1.0, verbose=-1, random_state=42,
        )
    return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=42)


def algorithm_name() -> str:
    return "LightGBM Regressor" if HAVE_LGBM else "HistGradientBoostingRegressor"


def train(X: np.ndarray, y: np.ndarray, time_split: float = 0.75) -> tuple:
    n = len(y)
    if n < 40:
        raise ValueError(f"замало даних для навчання регресії: {n} задач")

    cut = int(n * time_split)
    X_tr, X_te, y_tr, y_te = X[:cut], X[cut:], y[:cut], y[cut:]

    model = _make_estimator(len(y_tr))
    model.fit(X_tr, y_tr)

    metrics: dict[str, float | int] = {"train_samples": int(len(y_tr)), "test_samples": int(len(y_te))}
    if len(y_te) > 5:
        pred = np.clip(model.predict(X_te), 1, None)
        naive = np.full_like(y_te, float(np.median(y_tr)), dtype=float)
        metrics |= {
            "mae_s": round(float(mean_absolute_error(y_te, pred)), 2),
            "mape": round(float(mean_absolute_percentage_error(y_te, pred)), 4),
            "r2": round(float(r2_score(y_te, pred)), 4),
            "baseline_mae_s": round(float(mean_absolute_error(y_te, naive)), 2),
        }
        if metrics["baseline_mae_s"]:
            metrics["mae_improvement"] = round(
                1 - metrics["mae_s"] / metrics["baseline_mae_s"], 4
            )
    return model, metrics


def predict(model, x: list[float]) -> float:
    return max(1.0, float(model.predict(np.asarray([x], dtype=float))[0]))


def predict_batch(model, X: list[list[float]]) -> list[float]:
    if not X:
        return []
    return [max(1.0, float(v)) for v in model.predict(np.asarray(X, dtype=float))]
