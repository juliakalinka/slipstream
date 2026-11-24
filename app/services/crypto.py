"""Зберігання таємниць у базі в зашифрованому вигляді.

Маркер доступу до репозиторію дає змогу читати чужий код, тому лежати в базі
відкритим текстом він не може: резервна копія, вивантаження таблиці чи запит
адміністратора показали б його будь-кому. Стовпець типу EncryptedString
шифрує значення перед записом і розшифровує під час читання, тож решта коду
працює зі звичайним рядком і про шифрування не знає.

Ключ виводиться із SECRET_KEY застосунку. Окремого ключа свідомо немає: одна
таємниця, яку треба берегти, краща за дві, а втрата SECRET_KEY і так вимагає
перевипуску всіх маркерів доступу користувачів.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.config import settings

# Позначка зашифрованого значення. За нею відрізняються записи, збережені
# до появи шифрування: вони читаються як є і шифруються під час першого запису.
PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    key = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    """Зашифрувати рядок; порожній лишається порожнім."""
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """Розшифрувати рядок; значення без позначки повертається як є."""
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Ключ змінено: старе значення вже не прочитати. Порожній рядок
        # означає «маркера немає», і адаптер попросить задати його заново.
        return ""


class EncryptedString(TypeDecorator):
    """Рядок, який у базі лежить зашифрованим."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value) if isinstance(value, str) else value

    def process_result_value(self, value, dialect):
        return decrypt(value) if isinstance(value, str) else value
