#!/usr/bin/env python3
"""Створення історії комітів для демонстраційного репозиторію стенда.

Історія має бути такою, щоб на ній було видно зв'язок «змінений модуль —
перевірка, що впала». Тому дефекти тут СПРАВЖНІ: кожен із них є реальною
зміною коду, після якої набір перевірок дійсно падає. Це можна перевірити
запуском pytest на будь-якому коміті.

Кожен дефект згодом виправляється, як це відбувається в реальному проєкті:
розробник ламає, конвеєр показує, розробник лагодить.
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent / "demo-repo"


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, check=check)
    return r.stdout.strip()


@dataclass
class Edit:
    """Одна зміна у файлі: пошук -> заміна."""
    path: str
    old: str
    new: str

    def apply(self) -> None:
        f = REPO / self.path
        s = f.read_text()
        if self.old not in s:
            raise SystemExit(f"не знайдено фрагмент у {self.path}: {self.old!r}")
        f.write_text(s.replace(self.old, self.new, 1))

    def revert(self) -> Edit:
        return Edit(self.path, self.new, self.old)


# --- нешкідливі зміни: конвеєр лишається зеленим
SAFE = [
    Edit("src/auth/tokens.py", '"""Перевірка маркерів доступу."""',
         '"""Перевірка маркерів доступу (HMAC-SHA256)."""'),
    Edit("src/billing/invoice.py", "TAX_RATES = {", "# ставки податку на додану вартість\nTAX_RATES = {"),
    Edit("src/catalog/search.py", "def normalise(query: str) -> str:",
         "def normalise(query: str) -> str:\n    # зведення регістру й пробілів"),
    Edit("src/shipping/rates.py", "FREE_FROM = Decimal(\"150.00\")",
         "# поріг безкоштовної доставки\nFREE_FROM = Decimal(\"150.00\")"),
    Edit("README.md", "Запуск перевірок локально:", "Запуск перевірок локально (потрібен Python 3.11+):"),
]

# --- справжні дефекти: після них pytest падає
BUGS = [
    Edit("src/billing/invoice.py", '"UA": Decimal("0.20")', '"UA": Decimal("0.18")'),
    Edit("src/shipping/rates.py", '1: Decimal("3.00")', '1: Decimal("3.50")'),
    Edit("src/catalog/search.py", "key=lambda p: (-p.stock, p.price, p.sku)",
         "key=lambda p: (p.price, -p.stock, p.sku)"),
    Edit("src/auth/passwords.py", "return strength(password) >= 3", "return strength(password) >= 2"),
    Edit("src/billing/discounts.py", 'if total_qty >= 50:\n        return Decimal("10")',
         'if total_qty >= 60:\n        return Decimal("10")'),
]

AUTHORS = [
    ("Олена Ткач", "olena@example.com"),
    ("Андрій Мельник", "andrii@example.com"),
    ("Ірина Бондар", "iryna@example.com"),
    ("Павло Шевчук", "pavlo@example.com"),
]

MESSAGES = {
    "src/auth": ["refactor: clarify token handling", "docs: note the signature scheme",
                 "fix: tighten password policy"],
    "src/billing": ["feat: adjust tax table", "refactor: document VAT rates",
                    "fix: correct discount tiers"],
    "src/catalog": ["refactor: tidy query normalisation", "feat: change result ordering"],
    "src/shipping": ["feat: revise base rates", "docs: annotate free-shipping threshold"],
    "README.md": ["docs: update the readme"],
}


def module_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def commit(edit: Edit, author: tuple[str, str], message: str) -> str:
    edit.apply()
    git("add", "-A")
    git("-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}",
        "commit", "-q", "-m", message)
    return git("rev-parse", "HEAD")


def main() -> None:
    ap = argparse.ArgumentParser(description="Історія комітів демонстраційного репозиторію")
    ap.add_argument("--commits", type=int, default=40)
    ap.add_argument("--bug-every", type=int, default=6, help="кожен такий коміт вносить дефект")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args()

    if not REPO.exists():
        raise SystemExit(f"немає каталогу {REPO}")

    rng = random.Random(args.seed)

    if not (REPO / ".git").exists():
        git("init", "-q", "-b", "main")
        (REPO / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n*.pyc\n")
        git("add", "-A")
        git("-c", "user.name=stand", "-c", "user.email=stand@example.com",
            "commit", "-q", "-m", "chore: initial import of demo-orders")
        print("репозиторій створено")

    pending_fix: Edit | None = None
    bugs = list(BUGS)
    rng.shuffle(bugs)
    made = defects = 0

    for i in range(1, args.commits + 1):
        author = rng.choice(AUTHORS)

        if pending_fix is not None and i % args.bug_every == 2:
            # Виправлення дефекту, внесеного кілька комітів тому.
            mod = module_of(pending_fix.path)
            commit(pending_fix, author, f"fix: restore correct behaviour in {mod}")
            pending_fix = None
            made += 1
            continue

        if i % args.bug_every == 0 and bugs and pending_fix is None:
            bug = bugs.pop()
            mod = module_of(bug.path)
            msg = rng.choice(MESSAGES.get(mod, ["chore: update"]))
            commit(bug, author, msg)
            pending_fix = bug.revert()
            made += 1
            defects += 1
            continue

        safe = rng.choice(SAFE)
        # нешкідливі зміни можна застосовувати лише раз, тому чергуємо з відкотом
        try:
            mod = module_of(safe.path)
            commit(safe, author, rng.choice(MESSAGES.get(mod, ["chore: update"])))
            SAFE[SAFE.index(safe)] = safe.revert()
            made += 1
        except SystemExit:
            continue

    if pending_fix is not None:
        commit(pending_fix, rng.choice(AUTHORS), "fix: restore correct behaviour")
        made += 1

    print(f"комітів створено: {made}, з них із дефектом: {defects}")
    print("перевірка поточного стану:")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=REPO,
                       capture_output=True, text=True)
    print("  ", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.returncode)


if __name__ == "__main__":
    main()
