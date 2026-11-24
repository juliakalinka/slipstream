"""Розподіл перевірок між паралельними виконавцями.

Задача планування P||Cmax, NP-складна. Застосовано жадібний алгоритм LPT
(Longest Processing Time first): найдовша одиниця призначається виконавцю з
найменшим навантаженням. Розклад не гірший за оптимальний більш ніж у
4/3 - 1/(3m) раза. Вага перевірки - оцінка її тривалості за попередніми
прогонами (ковзне середнє).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Shard:
    index: int
    test_ids: list[int]
    est_duration_s: float


@dataclass
class ShardingResult:
    shards: list[Shard]
    makespan_s: float          # найдовший виконавець, тобто критичний шлях
    sequential_s: float        # сумарна тривалість за послідовного виконання
    imbalance: float           # нерівномірність: (max - min) / max

    @property
    def speedup(self) -> float:
        return self.sequential_s / self.makespan_s if self.makespan_s else 1.0


def lpt_shard(durations: dict[int, float], n_shards: int) -> ShardingResult:
    """Розподілити окремі перевірки: ідентифікатор -> тривалість, с."""
    n_shards = max(1, n_shards)
    buckets: list[list[int]] = [[] for _ in range(n_shards)]
    loads = [0.0] * n_shards

    for tid, d in sorted(durations.items(), key=lambda kv: kv[1], reverse=True):
        k = min(range(n_shards), key=lambda i: loads[i])
        buckets[k].append(tid)
        loads[k] += d

    shards = [Shard(i, buckets[i], round(loads[i], 2)) for i in range(n_shards)]
    makespan = max(loads) if loads else 0.0
    used = [l for l in loads if l > 0]
    imbalance = (max(used) - min(used)) / max(used) if len(used) > 1 else 0.0
    return ShardingResult(
        shards=shards,
        makespan_s=round(makespan, 2),
        sequential_s=round(sum(loads), 2),
        imbalance=round(imbalance, 4),
    )


def lpt_shard_groups(
    groups: dict[str, tuple[list[int], float]], n_shards: int
) -> ShardingResult:
    """Розподілити неподільні групи перевірок.

    Перевірки, залежні від порядку виконання, проходять лише разом зі своїм
    набором в одному процесі, тому група потрапляє до одного виконавця цілком.

    groups: ідентифікатор групи -> (перелік перевірок, сумарна тривалість, с)
    """
    n_shards = max(1, n_shards)
    buckets: list[list[int]] = [[] for _ in range(n_shards)]
    loads = [0.0] * n_shards

    # правило LPT: найдовша група - першою, на найменш завантаженого виконавця
    by_duration = sorted(groups.values(), key=lambda g: g[1], reverse=True)
    for ids, total in by_duration:
        k = min(range(n_shards), key=lambda i: loads[i])
        buckets[k].extend(ids)
        loads[k] += total

    shards = [Shard(i, buckets[i], round(loads[i], 2)) for i in range(n_shards)]
    makespan = max(loads) if loads else 0.0
    used = [l for l in loads if l > 0]
    imbalance = (max(used) - min(used)) / max(used) if len(used) > 1 else 0.0
    return ShardingResult(
        shards=shards,
        makespan_s=round(makespan, 2),
        sequential_s=round(sum(loads), 2),
        imbalance=round(imbalance, 4),
    )


def suggest_shard_count(
    total_s: float, target_s: float, max_shards: int = 16
) -> int:
    """Скільки виконавців потрібно, щоб кожен уклався в цільовий час."""
    if target_s <= 0:
        return 1
    return max(1, min(max_shards, int(-(-total_s // target_s))))
