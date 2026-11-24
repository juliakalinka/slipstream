"""Розв'язання політик оптимізації для конкретної гілки."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Policy, PolicyType, Project


@dataclass
class ResolvedPolicy:
    """Підсумкові правила, що діють для цієї гілки цього проєкту."""
    skip_docs_only: bool = True
    keep_lint_on_skip: bool = True
    test_selection: bool = False
    # Бюджет задається двома способами одночасно: відносним (частка від повної
    # тривалості набору) і абсолютним. Діє суворіший з них. Відносний бюджет
    # переносимо між проєктами без переналаштування, абсолютний обмежує
    # дуже великі набори.
    budget_ratio: float = 0.35
    budget_s: int = 900
    min_keep_ratio: float = 0.12
    # Наскільки відбір має вкоротити критичний шлях ПОВЕРХ розподілу між
    # виконавцями, щоб його взагалі застосовувати. Нуль означає «завжди»,
    # і саме так система поводилася раніше - без огляду на те, чи дає
    # відбір хоч щось. Вимірювання показало, що найчастіше не дає.
    min_selection_gain: float = 0.10
    protect_default_branch: bool = True
    sharding: bool = True
    max_shards: int = 4
    target_shard_s: int = 90
    fail_fast: bool = True
    risk_threshold: float = 0.45
    flaky_quarantine: bool = True
    flaky_threshold: float = 0.20


def _matches(pattern: str, branch: str) -> bool:
    if not pattern or pattern == "*":
        return True
    return any(fnmatch.fnmatch(branch, p.strip()) for p in pattern.split(",") if p.strip())


def resolve(db: Session, project: Project, branch: str) -> ResolvedPolicy:
    rp = ResolvedPolicy(
        budget_s=settings.default_test_budget_sec,
        max_shards=settings.default_shards,
        risk_threshold=settings.high_risk_threshold,
        flaky_threshold=settings.flaky_threshold,
    )
    rules = db.scalars(select(Policy).where(Policy.project_id == project.id)).all()

    # Вимкнена політика має вимикати сам керуючий вплив. Раніше вимкнений
    # запис просто не враховувався, і діяли типові значення - а розподіл між
    # виконавцями, пропуск конвеєра, впорядкування за ризиком і карантин типово
    # ввімкнені. Перемикач у консолі через це нічого не вимикав.
    applied: set[PolicyType] = set()
    switched_off: set[PolicyType] = set()

    for r in rules:
        if not _matches(r.branch_pattern, branch):
            continue
        if not r.enabled:
            switched_off.add(r.type)
            continue
        applied.add(r.type)
        p = r.params or {}
        if r.type == PolicyType.SKIP_DOCS_ONLY:
            rp.skip_docs_only = True
            rp.keep_lint_on_skip = bool(p.get("keep_lint", True))
        elif r.type == PolicyType.TEST_SELECTION:
            rp.test_selection = True
            rp.budget_s = int(p.get("budget_s", rp.budget_s))
            rp.budget_ratio = float(p.get("budget_ratio", rp.budget_ratio))
            rp.min_keep_ratio = float(p.get("min_keep_ratio", rp.min_keep_ratio))
            rp.min_selection_gain = float(p.get("min_selection_gain", rp.min_selection_gain))
            rp.protect_default_branch = bool(p.get("protect_default_branch", True))
        elif r.type == PolicyType.SHARDING:
            rp.sharding = True
            rp.max_shards = int(p.get("max_shards", rp.max_shards))
            rp.target_shard_s = int(p.get("target_s", rp.target_shard_s))
        elif r.type == PolicyType.FAIL_FAST:
            rp.fail_fast = True
            rp.risk_threshold = float(p.get("risk_threshold", rp.risk_threshold))
        elif r.type == PolicyType.FLAKY_QUARANTINE:
            rp.flaky_quarantine = True
            rp.flaky_threshold = float(p.get("threshold", rp.flaky_threshold))

    # Якщо для гілки є лише вимкнені записи певного типу, вплив вимкнено.
    # Тип, якого в проєкті немає зовсім, лишається з типовим значенням.
    for kind in switched_off - applied:
        if kind == PolicyType.SKIP_DOCS_ONLY:
            rp.skip_docs_only = False
        elif kind == PolicyType.TEST_SELECTION:
            rp.test_selection = False
        elif kind == PolicyType.SHARDING:
            rp.sharding = False
        elif kind == PolicyType.FAIL_FAST:
            rp.fail_fast = False
        elif kind == PolicyType.FLAKY_QUARANTINE:
            rp.flaky_quarantine = False
    return rp
