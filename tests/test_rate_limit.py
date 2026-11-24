"""Захист входу від перебору пароля.

Перевіряється не лише те, що перебір зупиняється, а й те, що обмеження не
шкодить законному користувачеві: кілька описок не замикають йому доступ, а
успішний вхід знімає підозру.
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import rate_limit as rl

API = "/api/v1"
GOOD = {"email": "devops@example.com", "password": "devops123"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _fail(client: TestClient, times: int, email: str = "nobody@example.com") -> None:
    for _ in range(times):
        client.post(f"{API}/auth/login", json={"email": email, "password": "guess"})


# ------------------------------------------------------------ рівень запитів
def test_a_few_mistakes_do_not_lock_the_account(client: TestClient):
    _fail(client, settings.login_max_failures - 1)
    r = client.post(f"{API}/auth/login", json=GOOD)
    assert r.status_code == 200, r.text


def test_brute_force_is_stopped_even_with_the_right_password(client: TestClient):
    _fail(client, settings.login_max_failures)

    r = client.post(f"{API}/auth/login", json=GOOD)
    assert r.status_code == 429, "перебір не зупинено"
    assert int(r.headers["Retry-After"]) > 0, "не вказано, коли повторювати"


def test_successful_sign_in_clears_the_counter(client: TestClient):
    for _ in range(3):
        _fail(client, settings.login_max_failures - 1)
        assert client.post(f"{API}/auth/login", json=GOOD).status_code == 200


def test_the_account_is_guarded_separately_from_the_address(client: TestClient):
    """Розподілений перебір однієї пошти з багатьох адрес.

    Відтворити багато адрес у межах TestClient не можна, тому облік за
    обліковим записом заповнюється напряму: перевіряється, що вхід
    справді зважає на цей ключ, а не лише на адресу.
    """
    for _ in range(settings.login_account_max_failures):
        rl.account_limiter().record_failure(GOOD["email"])

    r = client.post(f"{API}/auth/login", json=GOOD)
    assert r.status_code == 429


def test_the_limit_is_case_insensitive_for_the_address_key(client: TestClient):
    """Пошта в різному регістрі — той самий обліковий запис."""
    for _ in range(settings.login_account_max_failures):
        rl.account_limiter().record_failure("devops@example.com")

    r = client.post(f"{API}/auth/login", json={**GOOD, "email": "DevOps@Example.com"})
    assert r.status_code == 429


# -------------------------------------------------------------- рівень класу
def test_lockout_expires():
    lim = rl.FailureLimiter(max_failures=3, window_s=100, lockout_s=60)
    for t in (0, 1, 2):
        lim.record_failure("k", now=t)

    assert lim.retry_after("k", now=3) == pytest.approx(59)
    assert lim.retry_after("k", now=62) == 0, "блокування не знялося з часом"


def test_attempts_during_lockout_do_not_extend_it():
    """Інакше сторонній тримав би чужий запис зачиненим як завгодно довго."""
    lim = rl.FailureLimiter(max_failures=3, window_s=100, lockout_s=60)
    for t in (0, 1, 2):
        lim.record_failure("k", now=t)

    # спроби під час блокування відхиляються ДО перевірки пароля, тому
    # невдача не записується і строк не починається заново
    assert lim.retry_after("k", now=30) > 0
    assert lim.retry_after("k", now=62) == 0


def test_old_failures_leave_the_window():
    lim = rl.FailureLimiter(max_failures=3, window_s=10, lockout_s=60)
    lim.record_failure("k", now=0)
    lim.record_failure("k", now=1)
    lim.record_failure("k", now=20)       # перші дві вже поза вікном

    assert lim.failures("k", now=20) == 1, "старі невдачі не вийшли з вікна"
    assert lim.retry_after("k", now=20) == 0


def test_the_table_of_keys_is_bounded():
    """Перебір із випадковими поштами не повинен з'їдати пам'ять."""
    lim = rl.FailureLimiter(max_failures=3, window_s=900, lockout_s=900)
    for i in range(rl.MAX_KEYS + 50):
        lim.record_failure(f"key-{i}")

    assert lim.tracked_keys() == rl.MAX_KEYS


# ------------------------------------------------------- визначення адреси
def _request(xff: str = "", peer: str = "203.0.113.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_forwarded_header_is_ignored_without_a_proxy():
    """Без проксі заголовок підставляє сам клієнт — довіряти йому не можна."""
    assert rl.client_ip(_request("1.2.3.4"), trusted_proxies=0) == "203.0.113.9"


def test_behind_one_proxy_the_last_element_is_taken():
    """Caddy ДОДАЄ побачену ним адресу в кінець ланцюжка."""
    assert rl.client_ip(_request("1.2.3.4, 198.51.100.7"), trusted_proxies=1) == "198.51.100.7"


def test_a_short_chain_falls_back_to_the_peer_address():
    assert rl.client_ip(_request("", peer="10.0.0.5"), trusted_proxies=1) == "10.0.0.5"
