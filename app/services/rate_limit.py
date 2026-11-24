"""Обмеження частоти невдалих спроб входу.

Система доступна з мережі, а пошти демонстраційних користувачів видно на
самій сторінці входу, тому єдине, що стоїть між стороннім і обліковим
записом, — пароль. Без обмеження кількості спроб його підбирають
перебором: програмний інтерфейс відповідає за мілісекунди й жодної
затримки не має.

Обмеження накладається на дві величини одночасно, бо вони захищають від
різних нападів:

* **адреса джерела** — зупиняє звичайний перебір з однієї машини;
* **обліковий запис** — зупиняє розподілений перебір, коли ту саму пошту
  пробують з багатьох адрес.

Поріг для облікового запису вищий, ніж для адреси: заблокувати чужий
обліковий запис навмисне не повинно бути дешево.

Облік ведеться в пам'яті процесу — для одного робочого процесу цього
достатньо. За кількох процесів потрібне спільне сховище (наприклад Redis);
це межа застосовності, а не недогляд.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from fastapi import Request

from app.config import settings

# Межа кількості ключів: інакше перебір з випадковими поштами або адресами
# повільно з'їдав би пам'ять процесу. Витісняється найдавніший ключ.
MAX_KEYS = 4096


class FailureLimiter:
    """Лічильник невдач у рухомому вікні з блокуванням після перевищення.

    Блокування НЕ продовжується від спроб, зроблених під час блокування:
    інакше сторонній тримав би обліковий запис зачиненим як завгодно довго,
    просто надсилаючи запити. Тому той, хто звертається, має спершу
    запитати `retry_after` і записувати невдачу лише тоді, коли спроба
    була дозволена.
    """

    def __init__(self, max_failures: int, window_s: float, lockout_s: float):
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- запити
    def retry_after(self, key: str, now: float | None = None) -> float:
        """Скільки секунд лишилося до розблокування; 0 — спроба дозволена."""
        now = time.monotonic() if now is None else now
        with self._lock:
            stamps = self._prune(key, now)
            if len(stamps) < self.max_failures:
                return 0.0
            return max(0.0, stamps[-1] + self.lockout_s - now)

    def failures(self, key: str, now: float | None = None) -> int:
        """Кількість невдач у вікні. Потрібна для журналу та перевірок."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, now))

    def tracked_keys(self) -> int:
        """Скільки ключів зараз під обліком. Обмежено згори MAX_KEYS."""
        with self._lock:
            return len(self._fails)

    # ---------------------------------------------------------------- зміни
    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            stamps = self._prune(key, now)
            stamps.append(now)
            self._fails[key] = stamps
            self._fails.move_to_end(key)
            while len(self._fails) > MAX_KEYS:
                self._fails.popitem(last=False)

    def reset(self, key: str) -> None:
        """Успішний вхід знімає підозру з ключа."""
        with self._lock:
            self._fails.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()

    # ------------------------------------------------------------ внутрішнє
    def _prune(self, key: str, now: float) -> list[float]:
        """Відкинути невдачі, що вийшли з вікна. Викликається під замком."""
        stamps = [t for t in self._fails.get(key, ()) if now - t < self.window_s]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps


def client_ip(request: Request, trusted_proxies: int | None = None) -> str:
    """Адреса джерела запиту з урахуванням зворотних проксі.

    Заголовок `X-Forwarded-For` формує не клієнт, а проксі: Caddy ДОДАЄ в
    кінець ланцюжка адресу, з якої запит надійшов до нього. Тому довіряти
    можна лише стільком останнім елементам, скільки власних проксі стоїть
    перед застосунком (`TRUSTED_PROXIES`). Якщо проксі немає, заголовок
    ігнорується зовсім — інакше будь-хто підставив би собі довільну адресу
    й обмеження не коштувало б нічого.
    """
    hops = settings.trusted_proxies if trusted_proxies is None else trusted_proxies
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(chain) >= hops:
        return chain[-hops]
    return peer


_ip_limiter: FailureLimiter | None = None
_account_limiter: FailureLimiter | None = None


def ip_limiter() -> FailureLimiter:
    global _ip_limiter
    if _ip_limiter is None:
        _ip_limiter = FailureLimiter(
            settings.login_max_failures, settings.login_window_s, settings.login_lockout_s)
    return _ip_limiter


def account_limiter() -> FailureLimiter:
    global _account_limiter
    if _account_limiter is None:
        _account_limiter = FailureLimiter(
            settings.login_account_max_failures, settings.login_window_s,
            settings.login_lockout_s)
    return _account_limiter


def reset_all() -> None:
    """Очистити облік. Використовується тестами між перевірками."""
    ip_limiter().clear()
    account_limiter().clear()
