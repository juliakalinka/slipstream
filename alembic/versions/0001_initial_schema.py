"""Початкова схема бази даних: дванадцять таблиць (Рис. 4.1)

Revision ID: 0001
Revises: 
Create Date: 2026-09-24 16:16:31.328607

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Створити схему з нуля."""
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=120), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('DEVOPS', 'DEVELOPER', 'LEAD', name='role'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_email'), ['email'], unique=True)

    op.create_table('projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('repo_url', sa.String(length=255), nullable=False),
    sa.Column('ci_provider', sa.Enum('GITLAB', 'GITHUB', 'JENKINS', 'SYNTHETIC', name='ciprovider'), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=False),
    sa.Column('default_branch', sa.String(length=60), nullable=False),
    sa.Column('access_token', sa.String(length=255), nullable=False),
    sa.Column('webhook_secret', sa.String(length=64), nullable=False),
    sa.Column('plan_mode', sa.Enum('OFF', 'DRY_RUN', 'ACTIVE', name='planmode'), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_projects_name'), ['name'], unique=False)

    op.create_table('commits',
    sa.Column('sha', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('author', sa.String(length=80), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('branch', sa.String(length=120), nullable=False),
    sa.Column('files_changed', sa.Integer(), nullable=False),
    sa.Column('additions', sa.Integer(), nullable=False),
    sa.Column('deletions', sa.Integer(), nullable=False),
    sa.Column('changed_paths', sa.JSON(), nullable=False),
    sa.Column('committed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('sha')
    )
    with op.batch_alter_table('commits', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_commits_author'), ['author'], unique=False)
        batch_op.create_index(batch_op.f('ix_commits_committed_at'), ['committed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_commits_project_id'), ['project_id'], unique=False)

    op.create_table('model_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.Enum('FAILURE', 'DURATION', 'TEST_RANK', 'ANOMALY', name='modeltype'), nullable=False),
    sa.Column('version', sa.String(length=20), nullable=False),
    sa.Column('algorithm', sa.String(length=60), nullable=False),
    sa.Column('artifact_path', sa.String(length=255), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('feature_names', sa.JSON(), nullable=False),
    sa.Column('train_samples', sa.Integer(), nullable=False),
    sa.Column('trained_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('model_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_versions_is_active'), ['is_active'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_versions_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_versions_type'), ['type'], unique=False)

    op.create_table('policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('type', sa.Enum('SKIP_DOCS_ONLY', 'TEST_SELECTION', 'SHARDING', 'FAIL_FAST', 'FLAKY_QUARANTINE', name='policytype'), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('branch_pattern', sa.String(length=120), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('policies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_policies_project_id'), ['project_id'], unique=False)

    op.create_table('test_cases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('suite', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('file_path', sa.String(length=255), nullable=False),
    sa.Column('avg_duration_ms', sa.Float(), nullable=False),
    sa.Column('total_runs', sa.Integer(), nullable=False),
    sa.Column('total_failures', sa.Integer(), nullable=False),
    sa.Column('flaky_score', sa.Float(), nullable=False),
    sa.Column('quarantined', sa.Boolean(), nullable=False),
    sa.Column('last_failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'suite', 'name', name='uq_test_case')
    )
    with op.batch_alter_table('test_cases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_test_cases_flaky_score'), ['flaky_score'], unique=False)
        batch_op.create_index(batch_op.f('ix_test_cases_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_test_cases_quarantined'), ['quarantined'], unique=False)
        batch_op.create_index(batch_op.f('ix_test_cases_suite'), ['suite'], unique=False)

    op.create_table('pipelines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('commit_sha', sa.String(length=40), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=False),
    sa.Column('ref', sa.String(length=120), nullable=False),
    sa.Column('status', sa.Enum('SUCCESS', 'FAILED', 'CANCELED', 'RUNNING', name='runstatus'), nullable=False),
    sa.Column('trigger', sa.String(length=40), nullable=False),
    sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_s', sa.Integer(), nullable=False),
    sa.Column('queue_time_s', sa.Integer(), nullable=False),
    sa.Column('optimized', sa.Boolean(), nullable=False),
    sa.Column('plan_snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['commit_sha'], ['commits.sha'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pipelines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pipelines_commit_sha'), ['commit_sha'], unique=False)
        batch_op.create_index(batch_op.f('ix_pipelines_optimized'), ['optimized'], unique=False)
        batch_op.create_index(batch_op.f('ix_pipelines_project_id'), ['project_id'], unique=False)
        batch_op.create_index('ix_pipelines_project_started', ['project_id', 'started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_pipelines_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_pipelines_status'), ['status'], unique=False)

    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('pipeline_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.Enum('DURATION_SPIKE', 'QUEUE_DELAY', 'FAILURE_RATE', 'FLAKY_BURST', name='alertkind'), nullable=False),
    sa.Column('severity', sa.Enum('INFO', 'WARNING', 'CRITICAL', name='severity'), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('acknowledged', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['pipeline_id'], ['pipelines.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alerts_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alerts_project_id'), ['project_id'], unique=False)

    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pipeline_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=False),
    sa.Column('status', sa.Enum('SUCCESS', 'FAILED', 'CANCELED', 'RUNNING', name='runstatus'), nullable=False),
    sa.Column('runner', sa.String(length=60), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_s', sa.Integer(), nullable=False),
    sa.Column('pred_duration_s', sa.Float(), nullable=True),
    sa.Column('pred_fail_prob', sa.Float(), nullable=True),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('shard_index', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['pipeline_id'], ['pipelines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_name'), ['name'], unique=False)
        batch_op.create_index(batch_op.f('ix_jobs_pipeline_id'), ['pipeline_id'], unique=False)

    op.create_table('predictions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pipeline_id', sa.Integer(), nullable=False),
    sa.Column('model_version_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.Enum('FAILURE', 'DURATION', 'TEST_RANK', 'ANOMALY', name='modeltype'), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'], ),
    sa.ForeignKeyConstraint(['pipeline_id'], ['pipelines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('predictions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_predictions_pipeline_id'), ['pipeline_id'], unique=False)

    op.create_table('recommendations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('pipeline_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.Enum('QUARANTINE_FLAKY', 'INCREASE_SHARDS', 'REORDER_JOBS', 'ENABLE_CACHE', 'SPLIT_SLOW_JOB', 'REDUCE_TEST_SCOPE', name='recotype'), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('expected_gain_s', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('NEW', 'APPLIED', 'REJECTED', name='recostatus'), nullable=False),
    sa.Column('applied_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['applied_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['pipeline_id'], ['pipelines.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recommendations_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recommendations_status'), ['status'], unique=False)

    op.create_table('test_results',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('test_case_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('PASSED', 'FAILED', 'SKIPPED', name='teststatus'), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('retries', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['test_case_id'], ['test_cases.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('test_results', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_test_results_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_test_results_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_test_results_test_case_id'), ['test_case_id'], unique=False)



def downgrade() -> None:
    """Прибрати всю схему."""
    with op.batch_alter_table('test_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_test_results_test_case_id'))
        batch_op.drop_index(batch_op.f('ix_test_results_status'))
        batch_op.drop_index(batch_op.f('ix_test_results_job_id'))

    op.drop_table('test_results')
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recommendations_status'))
        batch_op.drop_index(batch_op.f('ix_recommendations_project_id'))

    op.drop_table('recommendations')
    with op.batch_alter_table('predictions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_predictions_pipeline_id'))

    op.drop_table('predictions')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_pipeline_id'))
        batch_op.drop_index(batch_op.f('ix_jobs_name'))

    op.drop_table('jobs')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alerts_project_id'))
        batch_op.drop_index(batch_op.f('ix_alerts_created_at'))

    op.drop_table('alerts')
    with op.batch_alter_table('pipelines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pipelines_status'))
        batch_op.drop_index(batch_op.f('ix_pipelines_started_at'))
        batch_op.drop_index('ix_pipelines_project_started')
        batch_op.drop_index(batch_op.f('ix_pipelines_project_id'))
        batch_op.drop_index(batch_op.f('ix_pipelines_optimized'))
        batch_op.drop_index(batch_op.f('ix_pipelines_commit_sha'))

    op.drop_table('pipelines')
    with op.batch_alter_table('test_cases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_test_cases_suite'))
        batch_op.drop_index(batch_op.f('ix_test_cases_quarantined'))
        batch_op.drop_index(batch_op.f('ix_test_cases_project_id'))
        batch_op.drop_index(batch_op.f('ix_test_cases_flaky_score'))

    op.drop_table('test_cases')
    with op.batch_alter_table('policies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_policies_project_id'))

    op.drop_table('policies')
    with op.batch_alter_table('model_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_model_versions_type'))
        batch_op.drop_index(batch_op.f('ix_model_versions_project_id'))
        batch_op.drop_index(batch_op.f('ix_model_versions_is_active'))

    op.drop_table('model_versions')
    with op.batch_alter_table('commits', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_commits_project_id'))
        batch_op.drop_index(batch_op.f('ix_commits_committed_at'))
        batch_op.drop_index(batch_op.f('ix_commits_author'))

    op.drop_table('commits')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_name'))

    op.drop_table('projects')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_email'))

    op.drop_table('users')
