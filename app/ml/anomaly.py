"""Задача 6: виявлення аномалій у роботі конвеєра.

Поєднано два підходи, бо вони ловлять різні типи відхилень:
  * Isolation Forest на багатовимірному векторі характеристик конвеєра -
    ловить незвичні поєднання показників;
  * робастний z-показник на ковзному вікні за тривалістю - ловить різкі
    стрибки навіть тоді, коли решта показників у нормі.

Використано медіану й MAD замість середнього та стандартного відхилення,
бо самі викиди сильно зміщують класичні оцінки.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

from app.config import settings
from app.models import AlertKind, Severity


@dataclass
class AnomalyVerdict:
    is_anomaly: bool
    kind: AlertKind
    severity: Severity
    score: float
    message: str
    details: dict


ANOMALY_FEATURES = ["duration_s", "queue_time_s", "n_jobs", "fail_ratio", "n_tests"]

# Пороги практичної значущості. Самого лише статистичного критерію недостатньо:
# якщо історія дуже стабільна, MAD прямує до нуля і z-показник вибухає навіть
# на мізерному відхиленні. Тоді система засипала б команду хибними тривогами
# через різницю в кілька секунд. Тому відхилення має бути водночас
# статистично значущим І практично помітним.
MIN_RELATIVE_DEVIATION = 0.25   # щонайменше на чверть довше за звичне
MIN_ABSOLUTE_DEVIATION_S = 30   # і щонайменше на півхвилини
MIN_QUEUE_RELATIVE = 1.00       # черга: удвічі довша за звичну
MIN_QUEUE_ABSOLUTE_S = 60


def train_detector(X: np.ndarray, contamination: float | None = None) -> IsolationForest:
    c = contamination if contamination is not None else settings.anomaly_contamination
    det = IsolationForest(
        n_estimators=200, contamination=c, random_state=42, n_jobs=-1
    )
    det.fit(X)
    return det


def robust_z(value: float, history: list[float]) -> float:
    """Робастний z-показник на основі медіани та MAD."""
    if len(history) < 8:
        return 0.0
    arr = np.asarray(history, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    if mad < 1e-9:
        std = float(np.std(arr))
        return (value - med) / std if std > 1e-9 else 0.0
    return 0.6745 * (value - med) / mad


def check_pipeline(
    features: dict[str, float],
    duration_history: list[float],
    queue_history: list[float] | None = None,
    detector: IsolationForest | None = None,
    predicted_duration: float | None = None,
) -> AnomalyVerdict:
    duration = features.get("duration_s", 0.0)
    queue = features.get("queue_time_s", 0.0)

    z_dur = robust_z(duration, duration_history)
    z_queue = robust_z(queue, queue_history or [])

    iso_score = 0.0
    iso_flag = False
    if detector is not None:
        x = np.asarray([[features.get(k, 0.0) for k in ANOMALY_FEATURES]], dtype=float)
        iso_score = float(detector.score_samples(x)[0])
        iso_flag = bool(detector.predict(x)[0] == -1)

    # --- стрибок тривалості
    med = float(np.median(duration_history)) if duration_history else duration
    abs_dev = duration - med
    rel_dev = abs_dev / med if med > 0 else 0.0

    significant = (
        z_dur > 3.5
        and rel_dev >= MIN_RELATIVE_DEVIATION
        and abs_dev >= MIN_ABSOLUTE_DEVIATION_S
    )
    if significant:
        # Рівень терміновості визначається практичним масштабом відхилення,
        # а не лише статистикою: удвічі довший конвеєр - це критично,
        # на третину довший - привід придивитися.
        sev = Severity.CRITICAL if rel_dev >= 1.0 else Severity.WARNING
        return AnomalyVerdict(
            True, AlertKind.DURATION_SPIKE, sev, round(z_dur, 2),
            f"Pipeline took {duration:.0f}s against a usual {med:.0f}s "
            f"({rel_dev:.0%} longer)",
            {"z_score": round(z_dur, 2), "median_s": round(med, 1),
             "deviation_s": round(abs_dev, 1), "deviation_ratio": round(rel_dev, 3),
             "predicted_s": round(predicted_duration, 1) if predicted_duration else None,
             "isolation_score": round(iso_score, 4)},
        )

    # --- затримка в черзі
    q_hist = queue_history or []
    q_med = float(np.median(q_hist)) if q_hist else queue
    q_abs = queue - q_med
    q_rel = q_abs / q_med if q_med > 0 else 0.0
    if (z_queue > 4.0 and queue >= MIN_QUEUE_ABSOLUTE_S
            and q_rel >= MIN_QUEUE_RELATIVE):
        return AnomalyVerdict(
            True, AlertKind.QUEUE_DELAY, Severity.WARNING, round(z_queue, 2),
            f"Queue wait {queue:.0f}s against a usual {q_med:.0f}s",
            {"z_score": round(z_queue, 2), "median_s": round(q_med, 1),
             "deviation_ratio": round(q_rel, 3), "isolation_score": round(iso_score, 4)},
        )

    # незвичне поєднання показників
    if iso_flag:
        return AnomalyVerdict(
            True, AlertKind.FAILURE_RATE, Severity.INFO, round(iso_score, 4),
            "Pipeline shows an unusual combination of metrics",
            {"isolation_score": round(iso_score, 4), "features": features},
        )

    return AnomalyVerdict(False, AlertKind.DURATION_SPIKE, Severity.INFO, round(z_dur, 2), "", {})
