"""Конфігурація застосунку (12-factor: усе через змінні середовища)."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Slipstream"
    api_prefix: str = "/api/v1"

    # сховище
    database_url: str = "sqlite:///./data/cicd.db"
    models_dir: str = "./models_store"

    # безпека
    secret_key: str = "dev-secret-key-change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480

    # захист входу від перебору пароля: невдачі рахуються в рухомому вікні
    # окремо за адресою джерела й окремо за обліковим записом
    login_max_failures: int = 8
    login_account_max_failures: int = 20
    login_window_s: int = 900
    login_lockout_s: int = 900
    # скільком ОСТАННІМ елементам X-Forwarded-For можна довіряти, тобто
    # скільки власних зворотних проксі стоїть перед застосунком:
    # 0 — жодного (заголовок ігнорується), 1 — Caddy у складі служб
    trusted_proxies: int = 0

    # політики оптимізації за замовчуванням
    default_test_budget_sec: int = 600
    default_shards: int = 4
    min_confidence: float = 0.55
    high_risk_threshold: float = 0.45
    flaky_threshold: float = 0.20
    min_runs_for_flaky: int = 15
    anomaly_contamination: float = 0.05

    # скільки конвеєрів мінімально потрібно, щоб узагалі вмикати оптимізацію
    min_pipelines_to_optimize: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
