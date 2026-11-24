"""Ознака залежності тесту від порядку виконання

Додано після аналізу відкритого каталогу нестабільних тестів: близько
п'ятої частини з них падають не самі по собі, а лише за певного порядку
запуску. Система переставляє та розподіляє тести на шарди, тому без цієї
ознаки вона створювала б збої власними діями. Стовпець `order_dependent`
змушує планувальник тримати такі тести в межах одного шарда і в
незмінному порядку, а `flaky_category` зберігає тип нестабільності
з каталогу для звітності.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Додати ознаку залежності від порядку та категорію нестабільності."""
    # SQLite не підтримує ALTER COLUMN, тому batch_alter_table перебудовує таблицю
    with op.batch_alter_table("test_cases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "order_dependent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "flaky_category",
                sa.String(length=24),
                nullable=False,
                server_default="",
            )
        )
        batch_op.create_index(
            batch_op.f("ix_test_cases_order_dependent"),
            ["order_dependent"],
            unique=False,
        )


def downgrade() -> None:
    """Повернути схему до стану без урахування порядку виконання."""
    with op.batch_alter_table("test_cases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_test_cases_order_dependent"))
        batch_op.drop_column("flaky_category")
        batch_op.drop_column("order_dependent")
