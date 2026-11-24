from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_project
from app.db import get_db
from app.models import (
    Commit, Job, Pipeline, Project, RunStatus, TestCase, TestResult, TestStatus, User,
)
from app.schemas import JobOut, PipelineDetail, PipelineOut
from app.services.plan_builder import ACTIVE_WINDOW_BUILDS
from app.services.predictions import compare_with_fact

router = APIRouter(tags=["Конвеєри"])

# Наскільки далеко в часі можуть стояти запит плану і його виконання.
LINK_WINDOW = timedelta(hours=2)


def _test_counts(db: Session, ids: list[int]) -> dict[int, tuple[int, int]]:
    """Прогін -> (виконано перевірок, з них невдалих)."""
    if not ids:
        return {}
    rows = db.execute(
        select(
            Job.pipeline_id, func.count(TestResult.id),
            func.sum(case((TestResult.status == TestStatus.FAILED, 1), else_=0)),
        )
        .join(TestResult, TestResult.job_id == Job.id)
        .where(Job.pipeline_id.in_(ids))
        .group_by(Job.pipeline_id)
    ).all()
    return {pid: (int(n), int(f or 0)) for pid, n, f in rows}


def _pairs(db: Session, runs: list[Pipeline]) -> dict[int, Pipeline]:
    """Прогін -> парний прогін того самого коміту й гілки.

    Сервер GitLab виконує план дочірнім конвеєром, тому система отримує дві
    події: батьківський конвеєр містить лише задачу запиту плану, дочірній -
    самі перевірки. Парою вважається найближчий у часі прогін того самого
    коміту й гілки в межах LINK_WINDOW.
    """
    if not runs:
        return {}
    shas = {r.commit_sha for r in runs}
    project_ids = {r.project_id for r in runs}
    candidates = db.scalars(
        select(Pipeline).where(Pipeline.project_id.in_(project_ids), Pipeline.commit_sha.in_(shas))
    ).all()
    out: dict[int, Pipeline] = {}
    for r in runs:
        if r.started_at is None:
            continue
        near = [
            c for c in candidates
            if c.id != r.id and c.ref == r.ref and c.project_id == r.project_id
            and c.started_at is not None and abs(c.started_at - r.started_at) <= LINK_WINDOW
        ]
        if near:
            out[r.id] = min(near, key=lambda c: abs(c.started_at - r.started_at))
    return out


def _enrich(out: PipelineOut, run: Pipeline, counts: dict, pairs: dict) -> PipelineOut:
    out.tests_run, out.tests_failed = counts.get(run.id, (0, 0))
    other = pairs.get(run.id)
    if other is not None:
        has_plan = bool(run.plan_snapshot)
        other_has_plan = bool(other.plan_snapshot)
        if other_has_plan and not has_plan:
            out.linked_role = "plan"          # план цього виконання видано там
        elif has_plan and not other_has_plan:
            out.linked_role = "execution"     # цей план виконано там
        if out.linked_role:
            out.linked_run_id = other.id
            out.linked_optimized = bool(other.optimized)
    return out


def _checks(db: Session, run: Pipeline) -> tuple[int, list[dict], list[str]]:
    """Які перевірки проєкту виконано в прогоні, а які ні, у розрізі наборів.

    Переліком перевірок проєкту вважаються ті, що виконувалися хоча б раз за
    останні ACTIVE_WINDOW_BUILDS прогонів із результатами до цього прогону
    включно: так само визначає множину кандидатів і сам метод.
    """
    has_results = exists(
        select(TestResult.id).join(Job, TestResult.job_id == Job.id)
        .where(Job.pipeline_id == Pipeline.id)
    )
    recent = (
        select(Pipeline.id)
        .where(Pipeline.project_id == run.project_id, Pipeline.started_at <= run.started_at, has_results)
        .order_by(Pipeline.started_at.desc())
        .limit(ACTIVE_WINDOW_BUILDS)
    )
    catalog = set(db.scalars(
        select(TestResult.test_case_id).join(Job, TestResult.job_id == Job.id)
        .where(Job.pipeline_id.in_(recent)).distinct()
    ))
    here = db.execute(
        select(TestResult.test_case_id, TestResult.status)
        .join(Job, TestResult.job_id == Job.id).where(Job.pipeline_id == run.id)
    ).all()
    ran = {tid for tid, _ in here}
    failed = {tid for tid, st in here if st == TestStatus.FAILED}
    ids = catalog | ran
    cases = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_(ids)))}

    suites: dict[str, dict] = {}
    for tid in ids:
        c = cases.get(tid)
        key = (c.suite or c.file_path) if c else "?"
        row = suites.setdefault(key, {"suite": key, "ran": 0, "not_run": 0, "failed": 0})
        if tid in ran:
            row["ran"] += 1
            row["failed"] += tid in failed
        else:
            row["not_run"] += 1
    by_suite = sorted(suites.values(), key=lambda r: r["suite"])
    failed_names = sorted(cases[t].name for t in failed if t in cases)[:20]
    return len(ids), by_suite, failed_names


@router.get("/projects/{project_id}/pipelines", response_model=list[PipelineOut],
            summary="Історія конвеєрів проєкту")
def list_pipelines(
    response: Response,
    project: Project = Depends(get_project),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: RunStatus | None = Query(None, alias="status"),
    optimized: bool | None = Query(None),
    q: str | None = Query(None, max_length=64),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Сторінка переліку прогонів, від найновіших.

    q - номер прогону, початок ідентифікатора коміту або частина назви гілки.
    Загальна кількість прогонів, що відповідають умовам, повертається в
    заголовку X-Total-Count: за нею інтерфейс будує перехід між сторінками.
    """
    cond = [Pipeline.project_id == project.id]
    if status_filter:
        cond.append(Pipeline.status == status_filter)
    if optimized is not None:
        cond.append(Pipeline.optimized == optimized)
    term = (q or "").strip().lstrip("#")
    if term:
        match = [
            Pipeline.commit_sha.startswith(term.lower(), autoescape=True),
            Pipeline.ref.icontains(term, autoescape=True),
        ]
        if term.isdigit() and len(term) <= 9:
            match.append(Pipeline.id == int(term))
        cond.append(or_(*match))

    total = db.scalar(select(func.count(Pipeline.id)).where(*cond)) or 0
    response.headers["X-Total-Count"] = str(total)
    stmt = (
        select(Pipeline).where(*cond)
        .order_by(Pipeline.started_at.desc(), Pipeline.id.desc())
    )
    runs = db.scalars(stmt.offset(offset).limit(limit)).all()
    counts = _test_counts(db, [r.id for r in runs])
    pairs = _pairs(db, runs)
    return [_enrich(PipelineOut.model_validate(r), r, counts, pairs) for r in runs]


@router.get("/pipelines/{pipeline_id}", response_model=PipelineDetail,
            summary="Картка конвеєра")
def get_pipeline(
    pipeline_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    p = db.get(Pipeline, pipeline_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline run not found")
    jobs = db.scalars(select(Job).where(Job.pipeline_id == p.id).order_by(Job.id)).all()
    commit = db.get(Commit, p.commit_sha)
    detail = PipelineDetail.model_validate(p)
    detail.jobs = [JobOut.model_validate(j) for j in jobs]
    detail.prediction_rows = compare_with_fact(db, p)
    pairs = _pairs(db, [p])
    _enrich(detail, p, _test_counts(db, [p.id]), pairs)
    if detail.linked_role == "plan" and not p.plan_snapshot:
        # виконання дочірнього конвеєра: показуємо план, який для нього видано
        detail.plan_snapshot = dict(pairs[p.id].plan_snapshot or {})
        detail.plan_from_run = pairs[p.id].id
    if commit:
        detail.commit_author = commit.author
        detail.commit_message = commit.message
        detail.files_changed = commit.files_changed
        detail.changed_paths = list(commit.changed_paths or [])[:30]
    if detail.tests_run:
        detail.tests_total, detail.checks_by_suite, detail.failed_checks = _checks(db, p)
    return detail
