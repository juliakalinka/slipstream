"""Тести запобіжників планувальника.

Це найважливіший набір тестів для захисту роботи. Питання «а якщо модель
помилиться і не запустить потрібний тест» має підкріплену відповідь: кожен
запобіжник перевіряється окремо і автоматично.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    CIProvider, Commit, PlanMode, Policy, PolicyType, Project, TestCase, User,
)
from app.services.plan_builder import build_plan
from app.services.policy import ResolvedPolicy, resolve


@pytest.fixture()
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def project(db) -> Project:
    p = db.scalars(select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)).first()
    assert p is not None, "потрібен синтетичний стенд: scripts/seed_synthetic.py"
    return p


def commit_with(db, project, paths: list[str], sha: str, add=60, dele=15) -> Commit:
    c = db.get(Commit, sha)
    if c is None:
        c = Commit(
            sha=sha, project_id=project.id, author="tester", message="safeguard check",
            branch="feature/guard", files_changed=len(paths), additions=add,
            deletions=dele, changed_paths=paths, committed_at=datetime.now(timezone.utc),
        )
        db.add(c)
        db.commit()
    return c


# ------------------------------------------------------- запобіжник: гілка
def test_default_branch_never_gets_selective_run(db, project):
    """Основна гілка завжди проганяє повний набір тестів."""
    c = commit_with(db, project, ["src/billing/service_1.py"], "guard_main_0001")
    plan = build_plan(db, project, c, ref=project.default_branch)
    assert plan.mode != "selective"
    assert plan.fallbacks, "план має пояснити, чому відбору не було"


def test_feature_branch_allows_selection(db, project):
    saved = project.plan_mode
    project.plan_mode = PlanMode.ACTIVE
    db.commit()
    try:
        c = commit_with(db, project, ["src/auth/service_2.py"], "guard_feat_0001")
        plan = build_plan(db, project, c, ref="feature/task-101")
        assert plan.selected_tests > 0
        assert plan.selected_tests <= plan.total_tests
    finally:
        project.plan_mode = saved
        db.commit()


# ------------------------------------------------------- запобіжник: режим
def test_observation_mode_disables_optimization(db, project):
    saved = project.plan_mode
    project.plan_mode = PlanMode.OFF
    db.commit()
    try:
        c = commit_with(db, project, ["src/api/handlers_1.py"], "guard_off_0001")
        plan = build_plan(db, project, c, ref="feature/task-102")
        assert any("observe-only" in f for f in plan.fallbacks)
    finally:
        project.plan_mode = saved
        db.commit()


def test_dry_run_reports_plan_but_marks_it_inactive(db, project):
    saved = project.plan_mode
    project.plan_mode = PlanMode.DRY_RUN
    db.commit()
    try:
        c = commit_with(db, project, ["src/ui/models_3.py"], "guard_dry_0001")
        plan = build_plan(db, project, c, ref="feature/task-103")
        assert any("dry run" in f.lower() for f in plan.fallbacks)
    finally:
        project.plan_mode = saved
        db.commit()


# ------------------------------------------------- запобіжник: документація
def test_docs_only_change_skips_pipeline(db, project):
    c = commit_with(db, project, ["docs/guide_1.md", "docs/faq_2.md"], "guard_docs_0001")
    plan = build_plan(db, project, c, ref="feature/docs")
    assert plan.mode == "skip"
    assert plan.selected_tests == 0
    assert plan.est_duration_s < plan.baseline_duration_s


def test_mixed_change_does_not_skip(db, project):
    c = commit_with(db, project, ["docs/guide_1.md", "src/core/models_1.py"], "guard_mix_0001")
    plan = build_plan(db, project, c, ref="feature/mixed")
    assert plan.mode != "skip"
    assert plan.selected_tests > 0


# --------------------------------------------- запобіжник: залежність від порядку
def test_order_dependent_suites_are_not_split(db, project):
    od = db.scalars(
        select(TestCase).where(TestCase.project_id == project.id,
                               TestCase.order_dependent.is_(True))
    ).all()
    if not od:
        pytest.skip("у стенді немає тестів, залежних від порядку")

    saved = project.plan_mode
    project.plan_mode = PlanMode.ACTIVE
    db.commit()
    try:
        c = commit_with(db, project, ["src/auth/service_1.py"], "guard_od_0001")
        plan = build_plan(db, project, c, ref="feature/task-104")

        suite_of = {t.id: t.suite for t in od}
        placement: dict[str, set[str]] = {}
        for job in plan.jobs:
            for tid in job.test_ids:
                if tid in suite_of:
                    placement.setdefault(suite_of[tid], set()).add(job.name)

        for suite, jobs in placement.items():
            assert len(jobs) == 1, f"набір {suite} розірвано між задачами {jobs}"
    finally:
        project.plan_mode = saved
        db.commit()


def test_order_dependent_tests_are_pulled_in_whole(db, project):
    """Якщо відібрано один залежний тест, беруться всі такі тести його набору."""
    od = db.scalars(
        select(TestCase).where(TestCase.project_id == project.id,
                               TestCase.order_dependent.is_(True))
    ).all()
    if not od:
        pytest.skip("у стенді немає тестів, залежних від порядку")

    saved = project.plan_mode
    project.plan_mode = PlanMode.ACTIVE
    db.commit()
    try:
        c = commit_with(db, project, ["src/auth/handlers_2.py"], "guard_od_0002")
        plan = build_plan(db, project, c, ref="feature/task-105")
        chosen = {t for j in plan.jobs for t in j.test_ids}

        by_suite: dict[str, list[int]] = {}
        for t in od:
            by_suite.setdefault(t.suite, []).append(t.id)

        for suite, ids in by_suite.items():
            picked = [i for i in ids if i in chosen]
            if picked:
                assert len(picked) == len(ids), (
                    f"набір {suite}: відібрано {len(picked)} із {len(ids)} залежних тестів"
                )
    finally:
        project.plan_mode = saved
        db.commit()


# ------------------------------------------------------ план завжди пояснює себе
def test_plan_always_explains_itself(db, project):
    c = commit_with(db, project, ["src/api/repo_4.py"], "guard_expl_0001")
    plan = build_plan(db, project, c, ref="feature/task-106")
    assert plan.rationale or plan.fallbacks, "план без пояснення неприйнятний"
    assert plan.baseline_duration_s > 0
    assert plan.est_duration_s <= plan.baseline_duration_s + 1e-6
    assert 0.0 <= plan.risk <= 1.0


def test_plan_serializes_to_valid_gitlab_yaml(db, project):
    c = commit_with(db, project, ["src/core/utils_2.py"], "guard_yaml_0001")
    plan = build_plan(db, project, c, ref="feature/task-107")
    text = plan.to_gitlab_yaml()
    assert "stages:" in text
    assert "script:" in text
    for job in plan.jobs:
        assert f"{job.name}:" in text


# ---------------------------------------------------------- розв'язання політик
def test_branch_pattern_matching():
    p = Project(id=1, name="x", default_branch="main")
    db = SessionLocal()
    try:
        rp = resolve(db, p, "main")
        assert isinstance(rp, ResolvedPolicy)
    finally:
        db.close()


def test_policy_applies_only_to_matching_branches(db, project):
    rules = db.scalars(
        select(Policy).where(Policy.project_id == project.id,
                             Policy.type == PolicyType.TEST_SELECTION)
    ).all()
    if not rules:
        pytest.skip("політику відбору не налаштовано")
    pattern = rules[0].branch_pattern
    if pattern == "*":
        pytest.skip("політика діє на всіх гілках")

    on_feature = resolve(db, project, "feature/task-1")
    on_main = resolve(db, project, "main")
    assert on_feature.test_selection is True
    assert on_main.test_selection is False


# ------------------------------------- доцільність відбору перед його ризиком
def _selection_policy(db, project):
    """Політика відбору перевірок цього проєкту."""
    pol = db.scalars(
        select(Policy).where(Policy.project_id == project.id,
                             Policy.type == PolicyType.TEST_SELECTION)
    ).first()
    assert pol is not None, "у проєкті немає політики відбору перевірок"
    return pol


def test_selection_is_skipped_when_it_does_not_shorten_the_pipeline(db, project):
    """Відбір застосовується лише тоді, коли справді вкорочує конвеєр.

    Відбір - єдиний керуючий вплив, здатний пропустити дефект, і він
    конкурує з розподілом між виконавцями за той самий виграш. Якщо поверх
    шардів він не дає нічого, виконується повний набір: той самий час без
    ризику. Виміряне обґрунтування - у підрозділі 4.7.1 записки.
    """
    saved_mode = project.plan_mode
    pol = _selection_policy(db, project)
    saved_params = dict(pol.params or {})
    try:
        project.plan_mode = PlanMode.ACTIVE
        # Поріг свідомо завищений: жоден відбір його не подолає.
        pol.params = {**saved_params, "min_selection_gain": 0.99}
        db.commit()

        c = commit_with(db, project, ["src/auth/service_1.py"], "guard_gain_0001")
        plan = build_plan(db, project, c, ref="feature/guard")

        assert plan.mode != "selective", "відбір застосовано попри відсутність виграшу"
        assert any("full suite" in r for r in plan.rationale), \
            "план не пояснив, чому відбору не було"
    finally:
        pol.params = saved_params
        project.plan_mode = saved_mode
        db.commit()


def test_selection_still_applies_when_the_gain_is_worth_it(db, project):
    """Нульовий поріг повертає попередню поведінку: відбір застосовується завжди."""
    saved_mode = project.plan_mode
    pol = _selection_policy(db, project)
    saved_params = dict(pol.params or {})
    try:
        project.plan_mode = PlanMode.ACTIVE
        pol.params = {**saved_params, "min_selection_gain": 0.0}
        db.commit()

        c = commit_with(db, project, ["src/auth/service_1.py"], "guard_gain_0002")
        plan = build_plan(db, project, c, ref="feature/guard")

        assert plan.mode == "selective", "за нульового порога відбір має застосовуватися"
        assert plan.selected_tests < plan.total_tests
    finally:
        pol.params = saved_params
        project.plan_mode = saved_mode
        db.commit()


def test_the_decision_is_taken_by_measurement_not_by_guess(db, project):
    """Поріг читається з політики, а не зашитий у код."""
    pol = _selection_policy(db, project)
    saved = dict(pol.params or {})
    try:
        pol.params = {**saved, "min_selection_gain": 0.42}
        db.commit()
        rp = resolve(db, project, "feature/guard")
        assert rp.min_selection_gain == pytest.approx(0.42)
    finally:
        pol.params = saved
        db.commit()


# ----------------------------------------- вимкнена політика справді вимикає
@pytest.mark.parametrize("kind, flag", [
    (PolicyType.SHARDING, "sharding"),
    (PolicyType.SKIP_DOCS_ONLY, "skip_docs_only"),
    (PolicyType.FAIL_FAST, "fail_fast"),
    (PolicyType.FLAKY_QUARANTINE, "flaky_quarantine"),
    (PolicyType.TEST_SELECTION, "test_selection"),
])
def test_switching_a_policy_off_switches_the_action_off(db, project, kind, flag):
    """Перемикач політики в консолі має вимикати керуючий вплив.

    Раніше вимкнений запис просто не враховувався, і поверталися типові
    значення, а більшість впливів типово ввімкнено: розподіл між виконавцями
    тривав і після того, як інженер його вимкнув.
    """
    pol = db.scalars(
        select(Policy).where(Policy.project_id == project.id, Policy.type == kind)
    ).first()
    if pol is None:
        pytest.skip(f"у проєкті немає політики {kind.value}")
    branch = "feature/guard"
    was = pol.enabled
    try:
        pol.enabled = True
        db.commit()
        assert getattr(resolve(db, project, branch), flag) is True
        pol.enabled = False
        db.commit()
        assert getattr(resolve(db, project, branch), flag) is False
    finally:
        pol.enabled = was
        db.commit()


def test_plan_without_sharding_runs_tests_in_one_job(db, project):
    pol = db.scalars(
        select(Policy).where(Policy.project_id == project.id, Policy.type == PolicyType.SHARDING)
    ).first()
    if pol is None:
        pytest.skip("у проєкті немає політики розподілу")
    c = commit_with(db, project, ["src/api/handlers_1.py"], "guard_noshard_0001")
    try:
        pol.enabled = False
        db.commit()
        plan = build_plan(db, project, c, ref="main")
        assert plan.shards == 1
        assert len([j for j in plan.jobs if j.stage == "test"]) == 1
    finally:
        pol.enabled = True
        db.commit()
