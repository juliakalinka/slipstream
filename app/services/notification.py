"""Надсилання сповіщень про аномалії та рекомендації.

Канали реалізовані через єдиний інтерфейс, тому додавання нового каналу
не змінює бізнес-логіки. За замовчуванням увімкнено лише журнальний канал:
у навчальному стенді це дає змогу побачити всі сповіщення без зовнішніх
служб і без витоку даних назовні.
"""
from __future__ import annotations

import json
import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx

from app.models import Alert, Recommendation, Severity

logger = logging.getLogger("cicd.notify")

SEVERITY_MARK = {
    Severity.INFO: "Info",
    Severity.WARNING: "Warning",
    Severity.CRITICAL: "Critical",
}


@dataclass
class Message:
    title: str
    body: str
    severity: Severity = Severity.INFO
    link: str = ""
    fields: dict = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [f"[{SEVERITY_MARK.get(self.severity, '')}] {self.title}", self.body]
        for k, v in self.fields.items():
            lines.append(f"  {k}: {v}")
        if self.link:
            lines.append(f"  More: {self.link}")
        return "\n".join(lines)


class Channel(ABC):
    name = "generic"

    @abstractmethod
    def send(self, msg: Message) -> bool:
        """Повертає True, якщо повідомлення доставлено."""


class LogChannel(Channel):
    """Канал за замовчуванням: пише у журнал застосунку."""
    name = "log"

    def send(self, msg: Message) -> bool:
        level = {
            Severity.INFO: logging.INFO,
            Severity.WARNING: logging.WARNING,
            Severity.CRITICAL: logging.ERROR,
        }.get(msg.severity, logging.INFO)
        logger.log(level, msg.as_text())
        return True


class SlackChannel(Channel):
    """Надсилання через вхідний вебхук Slack."""
    name = "slack"

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, msg: Message) -> bool:
        if not self.webhook_url:
            return False
        blocks = [
            {"type": "header",
             "text": {"type": "plain_text", "text": msg.title[:150]}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": msg.body[:2900]}},
        ]
        if msg.fields:
            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
                    for k, v in list(msg.fields.items())[:10]
                ],
            })
        try:
            r = httpx.post(self.webhook_url, json={"blocks": blocks}, timeout=self.timeout)
            return r.status_code < 300
        except Exception as exc:
            logger.warning("Slack недоступний: %s", exc)
            return False


class WebhookChannel(Channel):
    """Універсальний канал: надсилає JSON на довільну адресу."""
    name = "webhook"

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, msg: Message) -> bool:
        if not self.url:
            return False
        try:
            r = httpx.post(self.url, json={
                "title": msg.title, "body": msg.body,
                "severity": msg.severity.value, "link": msg.link,
                "fields": msg.fields,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }, timeout=self.timeout)
            return r.status_code < 300
        except Exception as exc:
            logger.warning("Вебхук недоступний: %s", exc)
            return False


class EmailChannel(Channel):
    """Надсилання електронною поштою через сервер SMTP.

    Пошта потрібна тим, хто не сидить у месенджері команди: зведення за
    період читає керівник, і лист у скриньці для нього зручніший за канал.
    З'єднання відкривається на кожне повідомлення: сповіщень небагато, а
    тримати з'єднання з поштовим сервером відкритим заради них не варто.
    """
    name = "email"

    def __init__(self, host: str, port: int, sender: str, recipients: list[str],
                 username: str = "", password: str = "", starttls: bool = True,
                 timeout: float = 10.0):
        self.host, self.port = host, port
        self.sender, self.recipients = sender, recipients
        self.username, self.password = username, password
        self.starttls, self.timeout = starttls, timeout

    def send(self, msg: Message) -> bool:
        if not self.host or not self.recipients:
            return False
        mail = EmailMessage()
        mail["Subject"] = f"[{SEVERITY_MARK.get(msg.severity, 'Info')}] {msg.title}"[:200]
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.recipients)
        mail.set_content(msg.as_text())
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(mail)
            return True
        except Exception as exc:
            logger.warning("Поштовий сервер недоступний: %s", exc)
            return False


class NotificationService:
    """Розсилає повідомлення всіма налаштованими каналами.

    Відмова одного каналу не зупиняє решту: сповіщення не є критичним
    шляхом і не може впливати на роботу конвеєра.
    """

    def __init__(self, channels: list[Channel] | None = None):
        self.channels = channels or [LogChannel()]

    def send(self, msg: Message) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for ch in self.channels:
            try:
                results[ch.name] = ch.send(msg)
            except Exception as exc:
                logger.warning("Канал %s відмовив: %s", ch.name, exc)
                results[ch.name] = False
        return results

    # --------------------------------------------------- типові повідомлення
    def alert(self, a: Alert, project_name: str = "") -> dict[str, bool]:
        return self.send(Message(
            title=f"Pipeline anomaly: {project_name}".strip(": "),
            body=a.message,
            severity=a.severity,
            fields={
                "Kind": a.kind.value,
                "Run": a.pipeline_id or "—",
                **{k: v for k, v in list((a.details or {}).items())[:5] if v is not None},
            },
        ))

    def recommendation(self, r: Recommendation, project_name: str = "") -> dict[str, bool]:
        gain = (f"{r.expected_gain_s}s" if r.expected_gain_s < 120
                else f"{r.expected_gain_s / 60:.1f} min")
        return self.send(Message(
            title=f"New recommendation: {r.title}",
            body=r.description,
            severity=Severity.INFO,
            fields={"Project": project_name, "Estimated gain": gain, "Kind": r.type.value},
        ))

    def digest(self, project_name: str, metrics: dict, period: str = "") -> dict[str, bool]:
        """Зведення за період для керівника."""
        return self.send(Message(
            title=f"Digest for {project_name}",
            body=(f"Pipeline health for {period}." if period
                  else "Pipeline health for the selected period."),
            severity=Severity.INFO,
            fields={
                "Runs": metrics.get("total", 0),
                "Success rate": f"{metrics.get('success_rate', 0):.1%}",
                "Mean duration": f"{metrics.get('mean_duration_s', 0) / 60:.1f} min",
                "Optimised share": f"{metrics.get('optimized_share', 0):.1%}",
                **{k: v for k, v in metrics.items()
                   if k not in ("total", "success_rate", "mean_duration_s", "optimized_share")},
            },
        ))


_service: NotificationService | None = None


def get_service() -> NotificationService:
    """Сервіс сповіщень застосунку. Збирається один раз при першому зверненні."""
    global _service
    if _service is None:
        _service = build_from_env()
    return _service


def set_service(service: NotificationService | None) -> None:
    """Підмінити сервіс. Використовується тестами та під час налаштування."""
    global _service
    _service = service


def notify_alert(alert: "Alert", project_name: str = "") -> None:
    """Надіслати сповіщення про аномалію.

    Сповіщення не належить до критичного шляху: жодна помилка каналу не
    повинна вплинути на приймання події конвеєра, тому виняток гаситься тут.
    """
    try:
        get_service().alert(alert, project_name)
    except Exception as exc:                      # pragma: no cover - захисна гілка
        logger.warning("Не вдалося надіслати сповіщення про аномалію: %s", exc)


def notify_recommendation(reco: "Recommendation", project_name: str = "") -> None:
    """Надіслати сповіщення про нову рекомендацію."""
    try:
        get_service().recommendation(reco, project_name)
    except Exception as exc:                      # pragma: no cover - захисна гілка
        logger.warning("Не вдалося надіслати сповіщення про рекомендацію: %s", exc)


def build_from_env() -> NotificationService:
    """Зібрати сервіс із каналів, налаштованих змінними середовища."""
    import os
    channels: list[Channel] = [LogChannel()]
    if url := os.getenv("SLACK_WEBHOOK_URL"):
        channels.append(SlackChannel(url))
    if url := os.getenv("NOTIFY_WEBHOOK_URL"):
        channels.append(WebhookChannel(url))
    recipients = [a.strip() for a in os.getenv("NOTIFY_EMAIL_TO", "").split(",") if a.strip()]
    if (host := os.getenv("SMTP_HOST")) and recipients:
        channels.append(EmailChannel(
            host=host, port=int(os.getenv("SMTP_PORT", "587")),
            sender=os.getenv("NOTIFY_EMAIL_FROM") or os.getenv("SMTP_USER", "slipstream@localhost"),
            recipients=recipients,
            username=os.getenv("SMTP_USER", ""), password=os.getenv("SMTP_PASSWORD", ""),
            starttls=os.getenv("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no"),
        ))
    return NotificationService(channels)
