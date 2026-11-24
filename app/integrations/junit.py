"""Розбір звітів тестів у форматі JUnit XML.

Це єдиний спосіб отримати результати ОКРЕМИХ тестів, а не лише підсумок
конвеєра. Формат JUnit XML є фактичним стандартом: його вміють виводити
pytest, JUnit, Jest, PHPUnit, Go test і більшість інших засобів тестування.

Саме ці дані потрібні для моделі відбору тестів і для виявлення нестабільних
тестів. API GitHub Actions результатів окремих тестів не віддає, тому для
цих двох задач потрібен або стенд GitLab із звітами JUnit, або відкритий
датасет результатів тестів.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedTest:
    suite: str
    name: str
    status: str          # passed | failed | skipped
    duration_ms: int
    file_path: str = ""
    error: str = ""
    retries: int = 0


@dataclass
class ParsedReport:
    tests: list[ParsedTest] = field(default_factory=list)
    total_time_ms: int = 0

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if t.status == "failed")

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.status == "passed")

    def to_payload(self, job_name: str = "") -> list[dict]:
        """Перетворити у формат, який приймає вебхук системи."""
        return [
            {
                "suite": t.suite, "name": t.name, "status": t.status,
                "duration_ms": t.duration_ms, "file_path": t.file_path,
                "retries": t.retries, "error": t.error[:500],
                **({"job": job_name} if job_name else {}),
            }
            for t in self.tests
        ]


def _seconds_to_ms(value: str | None) -> int:
    try:
        return int(float(value or 0) * 1000)
    except (TypeError, ValueError):
        return 0


def _status_of(case: ET.Element) -> tuple[str, str]:
    """Статус тесту визначається наявністю дочірніх елементів."""
    if case.find("failure") is not None:
        el = case.find("failure")
        return "failed", (el.get("message") or el.text or "")[:1000]
    if case.find("error") is not None:
        el = case.find("error")
        return "failed", (el.get("message") or el.text or "")[:1000]
    if case.find("skipped") is not None:
        return "skipped", ""
    return "passed", ""


def parse_string(xml_text: str) -> ParsedReport:
    """Розібрати вміст JUnit XML.

    Підтримуються обидва варіанти кореня: <testsuites> зі вкладеними
    <testsuite> і одиничний <testsuite>.
    """
    report = ParsedReport()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return report

    suites = root.findall(".//testsuite") if root.tag != "testsuite" else [root]
    if not suites and root.tag == "testsuites":
        suites = list(root)

    for suite in suites:
        suite_name = suite.get("name") or "default"
        for case in suite.findall("testcase"):
            status, error = _status_of(case)
            # pytest кладе шлях у classname, Jest - у file
            classname = case.get("classname") or ""
            file_path = case.get("file") or classname.replace(".", "/")
            if file_path and not file_path.endswith((".py", ".js", ".ts", ".java", ".go")):
                file_path = f"{file_path}.py"

            # Набором вважаємо модуль, у якому лежить тест: для pytest це
            # останній сегмент classname (tests.auth.test_login -> test_login).
            if classname:
                parts = [x for x in classname.split(".") if x]
                suite_label = parts[-1] if parts else suite_name
            else:
                suite_label = suite_name

            report.tests.append(ParsedTest(
                suite=suite_label[:80],
                name=(case.get("name") or "unknown")[:160],
                status=status,
                duration_ms=_seconds_to_ms(case.get("time")),
                file_path=file_path[:255],
                error=error,
                # GitLab і pytest-rerunfailures позначають повтори окремим атрибутом
                retries=int(case.get("retries") or 0),
            ))
        report.total_time_ms += _seconds_to_ms(suite.get("time"))

    return report


def parse_file(path: str | Path) -> ParsedReport:
    p = Path(path)
    if not p.exists():
        return ParsedReport()
    return parse_string(p.read_text(encoding="utf-8", errors="ignore"))


def parse_directory(directory: str | Path, pattern: str = "*.xml") -> ParsedReport:
    """Зібрати всі звіти з каталогу в один результат."""
    merged = ParsedReport()
    for f in sorted(Path(directory).glob(pattern)):
        r = parse_file(f)
        merged.tests.extend(r.tests)
        merged.total_time_ms += r.total_time_ms
    return merged
