from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_admin_ids(value: str) -> tuple[int, ...]:
    if not value.strip():
        return ()
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _parse_bool(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


_Number = TypeVar("_Number", int, float)


def _number_from_env(name: str, default: str, kind: type[_Number]) -> _Number:
    try:
        return kind(os.getenv(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a valid number") from None


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str = field(repr=False)
    telegram_proxy_url: str | None = field(repr=False)
    telegram_force_ipv4: bool
    telegram_request_retries: int
    telegram_retry_seconds: int
    database_path: Path
    schedule_page_url: str = field(repr=False)
    admin_telegram_ids: tuple[int, ...]
    check_interval_minutes: int
    pin_sync_interval_minutes: int
    timezone: str
    downloads_path: Path
    backups_path: Path
    backup_retention_count: int
    log_retention_days: int
    analytics_salt: str = field(repr=False)
    analytics_retention_days: int
    schedule_proxy_url: str | None = field(default=None, repr=False)
    schedule_http_max_attempts: int = 4
    schedule_http_retry_max_delay: float = 15.0
    schedule_outage_failure_threshold: int = 3
    telegram_max_concurrent_updates: int = 32
    fsm_state_ttl_seconds: int = 1800
    fsm_max_records: int = 5000

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("TELEGRAM_MAX_CONCURRENT_UPDATES", self.telegram_max_concurrent_updates, 256),
            ("FSM_STATE_TTL_SECONDS", self.fsm_state_ttl_seconds, 86400),
            ("FSM_MAX_RECORDS", self.fsm_max_records, 100000),
        ):
            if not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if not isinstance(self.schedule_http_max_attempts, int) or not (
            1 <= self.schedule_http_max_attempts <= 10
        ):
            raise ValueError("SCHEDULE_HTTP_MAX_ATTEMPTS must be between 1 and 10")
        delay = self.schedule_http_retry_max_delay
        if not math.isfinite(delay) or not 0 <= delay <= 60:
            raise ValueError("SCHEDULE_HTTP_RETRY_MAX_DELAY must be between 0 and 60")
        if not isinstance(self.schedule_outage_failure_threshold, int) or not (
            1 <= self.schedule_outage_failure_threshold <= 100
        ):
            raise ValueError("SCHEDULE_OUTAGE_FAILURE_THRESHOLD must be between 1 and 100")

    @classmethod
    def from_env(cls, *, require_bot_token: bool = True) -> "Settings":
        _load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if require_bot_token and not token:
            raise ValueError("BOT_TOKEN is not set")
        database_path = Path(os.getenv("DATABASE_PATH", "data/schedule.db"))
        return cls(
            bot_token=token,
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
            telegram_force_ipv4=_parse_bool(
                os.getenv("TELEGRAM_FORCE_IPV4", "true"), default=True
            ),
            telegram_request_retries=max(
                1, int(os.getenv("TELEGRAM_REQUEST_RETRIES", "5"))
            ),
            telegram_retry_seconds=max(
                5, int(os.getenv("TELEGRAM_RETRY_SECONDS", "15"))
            ),
            database_path=database_path,
            schedule_page_url=os.getenv(
                "SCHEDULE_PAGE_URL", "https://ptgh.onego.ru/9006/"
            ),
            schedule_proxy_url=os.getenv("SCHEDULE_PROXY_URL", "").strip() or None,
            telegram_max_concurrent_updates=_number_from_env("TELEGRAM_MAX_CONCURRENT_UPDATES", "32", int),
            fsm_state_ttl_seconds=_number_from_env("FSM_STATE_TTL_SECONDS", "1800", int),
            fsm_max_records=_number_from_env("FSM_MAX_RECORDS", "5000", int),
            schedule_http_max_attempts=_number_from_env(
                "SCHEDULE_HTTP_MAX_ATTEMPTS", "4", int
            ),
            schedule_http_retry_max_delay=_number_from_env(
                "SCHEDULE_HTTP_RETRY_MAX_DELAY", "15", float
            ),
            schedule_outage_failure_threshold=_number_from_env(
                "SCHEDULE_OUTAGE_FAILURE_THRESHOLD", "3", int
            ),
            admin_telegram_ids=_parse_admin_ids(
                os.getenv("ADMIN_TELEGRAM_IDS", "")
            ),
            check_interval_minutes=max(
                1, int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
            ),
            pin_sync_interval_minutes=max(
                1, int(os.getenv("PIN_SYNC_INTERVAL_MINUTES", "5"))
            ),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            downloads_path=Path(os.getenv("DOWNLOADS_PATH", "data/downloads")),
            backups_path=Path(os.getenv("BACKUPS_PATH", "data/backups")),
            backup_retention_count=max(
                1, int(os.getenv("BACKUP_RETENTION_COUNT", "14"))
            ),
            log_retention_days=max(
                1, int(os.getenv("LOG_RETENTION_DAYS", "90"))
            ),
            analytics_salt=(
                os.getenv("ANALYTICS_SALT", "").strip()
                or token
                or "schedule-bot-local"
            ),
            analytics_retention_days=max(
                1, int(os.getenv("ANALYTICS_RETENTION_DAYS", "90"))
            ),
        )
