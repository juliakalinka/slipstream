#!/usr/bin/env python3
"""CLI-плагін cicd-opt: виконується як службова задача всередині CI-конвеєра.

Саме цей плагін з'єднує інформаційну систему з CI-сервером. Задача «plan»
викликає його, він збирає відомості про коміт, звертається до системи за
планом і записує опис дочірнього конвеєра у файл.

ГОЛОВНА ВИМОГА: плагін НІКОЛИ не має ламати конвеєр. Якщо система недоступна,
повільна або повернула помилку, плагін мовчки формує запасний план із повним
набором тестів і завершується успішно. Недоступність системи оптимізації не
може бути причиною зупинки доставки.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

_FULL_RUN_TEMPLATE = """# {reason}
# Running the complete test suite.
stages:
  - build
  - test

build:
  stage: build
  script:
    - make build

test:all:
  stage: test
  script:
    - make test
  # Повний прогін теж повертає результати: інакше система ніколи не
  # накопичить даних рівня окремої перевірки й не зможе навчитися.
  # Саме з таких прогонів починається впровадження.
  after_script:
    - >
      if [ -n "$CICD_OPT_API" ] && [ -n "$CICD_OPT_SECRET" ]; then
      for f in junit-*.xml; do
      [ -f "$f" ] || continue;
      curl --silent --show-error --retry 2 -X POST
      -H "X-CICD-Token: $CICD_OPT_SECRET"
      -H "Content-Type: application/xml"
      --data-binary "@$f"
      "$CICD_OPT_API/webhooks/junit/$CICD_OPT_PROJECT?external_id=$CI_PIPELINE_ID&commit_sha=$CI_COMMIT_SHA&ref=$CI_COMMIT_REF_NAME"
      || echo "report failed for $f"; done; fi
  artifacts:
    when: always
    paths: ["junit-*.xml"]
    reports:
      junit: "junit-*.xml"
"""


def full_run_yaml(reason: str) -> str:
    """Опис конвеєра з повним набором тестів і ЧЕСНОЮ причиною.

    Повний набір буває з двох різних причин, і плутати їх не можна. Або
    система недоступна - тоді це аварія, і її треба помітити. Або система
    доступна й свідомо повернула повний набір, бо проєкт ще не в бойовому
    режимі чи спрацював запобіжник - тоді все гаразд, і фальшиве
    повідомлення про недоступність змусило б інженера шукати проблему,
    якої немає.
    """
    return _FULL_RUN_TEMPLATE.format(reason=reason)


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=20, check=True
        ).stdout.strip()
    except Exception:
        return ""


def changed_paths(base: str, sha: str) -> list[str]:
    """Перелік змінених файлів. Спершу пробуємо порівняння з базовою гілкою,
    далі відкочуємося до одного коміту."""
    for args in (
        ["diff", "--name-only", f"{base}...{sha}"],
        ["diff", "--name-only", f"{sha}~1", sha],
        ["show", "--name-only", "--pretty=format:", sha],
    ):
        out = git(*args)
        if out:
            return [p for p in out.splitlines() if p.strip()]
    return []


def diff_stats(base: str, sha: str) -> tuple[int, int]:
    out = git("diff", "--numstat", f"{base}...{sha}") or git("diff", "--numstat", f"{sha}~1", sha)
    add = dele = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                add += int(parts[0])
                dele += int(parts[1])
            except ValueError:
                pass  # бінарні файли позначаються дефісом
    return add, dele


def main(argv: list[str] | None = None) -> int:
    """argv приймається окремим аргументом, щоб плагін можна було
    перевірити тестом, а не лише запуском процесу."""
    ap = argparse.ArgumentParser(prog="cicd-opt", description="Плагін оптимізації CI/CD конвеєра")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="отримати оптимізований план конвеєра")
    pl.add_argument("--api", default=os.getenv("CICD_OPT_API", "http://localhost:8000/api/v1"))
    pl.add_argument("--project", type=int, default=int(os.getenv("CICD_OPT_PROJECT", "1")))
    pl.add_argument("--sha", default=os.getenv("CI_COMMIT_SHA", "HEAD"))
    pl.add_argument("--ref", default=os.getenv("CI_COMMIT_REF_NAME", "main"))
    pl.add_argument("--base", default=os.getenv("CI_DEFAULT_BRANCH", "origin/main"))
    pl.add_argument("--output", default="pipeline.yml")
    pl.add_argument("--timeout", type=float, default=10.0,
                    help="максимальний час очікування відповіді системи")
    pl.add_argument("--json-output", default="", help="додатково зберегти план у JSON")

    args = ap.parse_args(argv)
    if args.cmd != "plan":
        return 0

    sha = git("rev-parse", args.sha) or args.sha
    paths = changed_paths(args.base, sha)
    add, dele = diff_stats(args.base, sha)
    author = git("log", "-1", "--pretty=%ae", sha)
    message = git("log", "-1", "--pretty=%s", sha)

    print(f"[cicd-opt] commit {sha[:10]} on {args.ref}")
    print(f"[cicd-opt] changed files: {len(paths)} (+{add}/-{dele})")

    payload = {
        "project_id": args.project,
        "commit_sha": sha,
        "ref": args.ref,
        "author": author,
        "message": message,
        "changed_paths": paths,
        "additions": add,
        "deletions": dele,
        "format": "json",
    }

    out = Path(args.output)
    if httpx is None:
        print("[cicd-opt] httpx is not installed; running the full suite")
        out.write_text(full_run_yaml("Fallback: the httpx client is not installed."))
        return 0

    try:
        with httpx.Client(timeout=args.timeout) as c:
            r = c.post(f"{args.api}/pipelines/plan", json=payload)
            r.raise_for_status()
            plan = r.json()

            yaml_payload = dict(payload, format="gitlab-yaml")
            ry = c.post(f"{args.api}/pipelines/plan", json=yaml_payload)
            ry.raise_for_status()
            yaml_text = ry.text
    except Exception as exc:
        # Ключовий запобіжник: конвеєр продовжує роботу в повному обсязі.
        print(f"[cicd-opt] optimiser unavailable ({exc}); running the full suite")
        out.write_text(full_run_yaml(f"Fallback: the optimiser was unreachable ({exc})."))
        return 0

    print(f"[cicd-opt] mode: {plan['mode']}")
    print(f"[cicd-opt] failure risk: {plan['risk']:.0%} (threshold {plan['risk_threshold']})")
    print(f"[cicd-opt] tests: {plan['selected_tests']} of {plan['total_tests']}, shards: {plan['shards']}")
    print(f"[cicd-opt] estimated {plan['est_duration_s'] / 60:.1f} min "
          f"instead of {plan['baseline_duration_s'] / 60:.1f} min "
          f"(saving {plan['saving_ratio']:.0%})")
    for line in plan.get("rationale", []):
        print(f"[cicd-opt]   + {line}")
    for line in plan.get("fallbacks", []):
        print(f"[cicd-opt]   ! {line}")

    if not plan.get("applied", False):
        # Це НЕ збій: система відповіла, але проєкт ще не в бойовому режимі.
        note = plan.get("note") or f"project is in \u00ab{plan.get('mode', 'dry run')}\u00bb mode"
        print(f"[cicd-opt] plan not applied: {note}")
        out.write_text(full_run_yaml(f"Plan computed but not applied: {note}"))
    else:
        out.write_text(yaml_text)

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"[cicd-opt] plan written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
