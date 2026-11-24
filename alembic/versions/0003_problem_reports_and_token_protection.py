"""Повідомлення про проблеми та зашифроване зберігання маркера доступу

Дві зміни, які з'явилися під час зіставлення системи з переліком варіантів
використання та нефункціональних вимог.

Таблиця `problem_reports` зберігає повідомлення користувачів про хибні
спрацювання: сповіщення про аномалію, план конвеєра чи рекомендацію, які
людина вважає помилковими. Без неї така оцінка лишалася б у листуванні й до
системи не поверталася.

Стовпець `projects.access_token` розширено з 255 до 512 символів: маркер
тепер лежить у базі зашифрованим, а шифротекст довший за відкритий текст
приблизно в півтора раза. Самі значення міграція не чіпає: запис без позначки
шифрування читається як є і шифрується під час першого збереження.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Створити таблицю повідомлень і розширити стовпець маркера доступу."""
    op.create_table(
        "problem_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("problem_reports", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_problem_reports_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_problem_reports_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_problem_reports_resolved"), ["resolved"], unique=False)

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.alter_column(
            "access_token",
            existing_type=sa.String(length=255),
            type_=sa.String(length=512),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Повернути схему до стану без повідомлень про проблеми."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.alter_column(
            "access_token",
            existing_type=sa.String(length=512),
            type_=sa.String(length=255),
            existing_nullable=False,
        )

    with op.batch_alter_table("problem_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_problem_reports_resolved"))
        batch_op.drop_index(batch_op.f("ix_problem_reports_project_id"))
        batch_op.drop_index(batch_op.f("ix_problem_reports_created_at"))
    op.drop_table("problem_reports")
