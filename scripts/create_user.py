"""Створення облікового запису або зміна пароля наявного.

Демонстраційні облікові записи з простими паролями створює лише генератор
синтетичних даних, і на сервері ними користуватися не можна. Цей сценарій
створює справжні облікові записи. Пароль не передається аргументом, щоб не
лишатися в історії командного рядка: його запитують з клавіатури або читають
зі стандартного входу.

    python scripts/create_user.py --email lead@company.ua --name "Ірина Бондар" --role lead
    echo "$PASSWORD" | python scripts/create_user.py --email ... --role devops --password-stdin

Для наявної адреси сценарій оновлює ім'я, роль і пароль.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.services.security import hash_password  # noqa: E402

ROLES = {"devops": Role.DEVOPS, "developer": Role.DEVELOPER, "lead": Role.LEAD}
MIN_LENGTH = 10


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Пароль: ")
    if first != getpass.getpass("Ще раз: "):
        raise SystemExit("паролі не збігаються")
    return first


def main() -> int:
    ap = argparse.ArgumentParser(description="Створити обліковий запис або змінити пароль")
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="", help="ім'я, яке показує інтерфейс")
    ap.add_argument("--role", required=True, choices=sorted(ROLES))
    ap.add_argument("--password-stdin", action="store_true", help="прочитати пароль зі стандартного входу")
    args = ap.parse_args()

    password = read_password(args.password_stdin)
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"пароль коротший за {MIN_LENGTH} символів")

    email = args.email.strip().lower()
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == email)).first()
        created = user is None
        if created:
            user = User(email=email, full_name=args.name or email, role=ROLES[args.role],
                        password_hash=hash_password(password))
            db.add(user)
        else:
            user.role = ROLES[args.role]
            user.password_hash = hash_password(password)
            if args.name:
                user.full_name = args.name
        db.commit()
    print(("створено" if created else "оновлено") + f": {email} ({args.role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
