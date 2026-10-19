from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
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


def _parse_grok_chat_ids(value: str) -> tuple[int, ...]:
    try:
        chat_ids = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip()))
    except ValueError:
        raise ValueError("GROK_REACTION_CHAT_IDS must contain negative Telegram chat IDs separated by commas") from None
    if any(chat_id >= 0 for chat_id in chat_ids):
        raise ValueError("GROK_REACTION_CHAT_IDS must contain negative Telegram chat IDs separated by commas")
    return chat_ids


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


def _previous_analytics_salts(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        salts = json.loads(value)
        if (not isinstance(salts, list) or len(salts) > 16
                or any(not isinstance(salt, str) or not salt.strip() for salt in salts)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("ANALYTICS_PREVIOUS_SALTS must be a JSON array of up to 16 non-empty secrets") from None
    return tuple(dict.fromkeys(salts))


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
    publication_lookback_days: int = 14
    publication_lookahead_days: int = 60
    publication_max_files: int = 32
    schedule_stale_after_minutes: int = 120
    outbox_retention_days: int = 90
    downloads_retention_days: int = 90
    analytics_previous_salts: tuple[str, ...] = field(default=(), repr=False)
    analytics_salt_is_explicit: bool = field(default=True, repr=False)
    schedule_signal_bot_username: str = ""
    schedule_signal_chat_id: int = 0
    schedule_signal_cooldown_seconds: int = 60
    grok_reactions_enabled: bool = False
    grok_cli_path: str = "grok"
    grok_timeout_seconds: float = 30.0
    grok_reaction_chat_types: tuple[str, ...] = ("group", "supergroup")
    grok_reaction_chat_ids: tuple[int, ...] = ()

    def resolve_analytics_salt(self) -> str:
        """Resolve the runtime key without changing it when BOT_TOKEN rotates.

        The first automatic runtime start preserves the legacy token-derived
        identity. Settings inspection and CLI parsing never create this file.
        """
        if self.analytics_salt_is_explicit:
            return self.analytics_salt
        path = self.database_path.with_suffix(self.database_path.suffix + ".analytics-salt")
        def read_key() -> str:
            # Corruption must fail closed instead of silently changing identities.
            with path.open("r", encoding="utf-8") as stored:
                key = stored.read(4097).strip()
            if not key or len(key) > 4096:
                raise ValueError("Persistent analytics key is invalid")
            return key
        if path.exists():
            return read_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = self.bot_token or secrets.token_urlsafe(48)
        descriptor, temporary = tempfile.mkstemp(prefix=".analytics-key-", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.chmod(temporary_path, 0o600)
                output.write(key + "\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                # Hard-link publication is atomic and cannot overwrite a key
                # created concurrently by another startup.
                os.link(temporary_path, path)
            except FileExistsError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)
        return read_key()

    def __post_init__(self) -> None:
        if any(type(chat_id) is not int or chat_id >= 0 for chat_id in self.grok_reaction_chat_ids):
            raise ValueError("GROK_REACTION_CHAT_IDS must contain negative Telegram chat IDs")
        if not math.isfinite(self.grok_timeout_seconds) or self.grok_timeout_seconds <= 0:
            raise ValueError("GROK_TIMEOUT_SECONDS must be a positive finite number")
        if (type(self.schedule_signal_chat_id) is not int
                or self.schedule_signal_chat_id > 0
                or (self.schedule_signal_bot_username and not self.schedule_signal_chat_id)):
            raise ValueError("SCHEDULE_SIGNAL_CHAT_ID must be a negative group ID when signals are enabled")
        if self.schedule_signal_bot_username and not re.fullmatch(
            r"@?[A-Za-z][A-Za-z0-9_]{4,31}", self.schedule_signal_bot_username
        ):
            raise ValueError("SCHEDULE_SIGNAL_BOT_USERNAME must be a Telegram username")
        if (type(self.schedule_signal_cooldown_seconds) is not int
                or not 30 <= self.schedule_signal_cooldown_seconds <= 3600):
            raise ValueError("SCHEDULE_SIGNAL_COOLDOWN_SECONDS must be between 30 and 3600")
        for name, value, minimum, maximum in (
            ("PUBLICATION_LOOKBACK_DAYS", self.publication_lookback_days, 0, 366),
            ("PUBLICATION_LOOKAHEAD_DAYS", self.publication_lookahead_days, 1, 366),
            ("PUBLICATION_MAX_FILES", self.publication_max_files, 1, 128),
            ("SCHEDULE_STALE_AFTER_MINUTES", self.schedule_stale_after_minutes, 1, 10080),
            ("OUTBOX_RETENTION_DAYS", self.outbox_retention_days, 7, 3650),
            ("DOWNLOADS_RETENTION_DAYS", self.downloads_retention_days, 7, 3650),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
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
            schedule_signal_bot_username=os.getenv("SCHEDULE_SIGNAL_BOT_USERNAME", "").strip(),
            schedule_signal_chat_id=_number_from_env("SCHEDULE_SIGNAL_CHAT_ID", "0", int),
            schedule_signal_cooldown_seconds=_number_from_env(
                "SCHEDULE_SIGNAL_COOLDOWN_SECONDS", "60", int
            ),
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
            publication_lookback_days=_number_from_env("PUBLICATION_LOOKBACK_DAYS", "14", int),
            publication_lookahead_days=_number_from_env("PUBLICATION_LOOKAHEAD_DAYS", "60", int),
            publication_max_files=_number_from_env("PUBLICATION_MAX_FILES", "32", int),
            schedule_stale_after_minutes=_number_from_env("SCHEDULE_STALE_AFTER_MINUTES", "120", int),
            outbox_retention_days=_number_from_env("OUTBOX_RETENTION_DAYS", "90", int),
            downloads_retention_days=_number_from_env("DOWNLOADS_RETENTION_DAYS", "90", int),
            analytics_previous_salts=_previous_analytics_salts(os.getenv("ANALYTICS_PREVIOUS_SALTS", "")),
            analytics_salt_is_explicit=bool(os.getenv("ANALYTICS_SALT", "").strip()),
            analytics_salt=(
                os.getenv("ANALYTICS_SALT", "").strip()
                or token
                or "schedule-bot-local"
            ),
            analytics_retention_days=max(
                1, int(os.getenv("ANALYTICS_RETENTION_DAYS", "90"))
            ),
            grok_reactions_enabled=_parse_bool(
                os.getenv("GROK_REACTIONS_ENABLED", "false"), default=False
            ),
            grok_cli_path=os.getenv("GROK_CLI_PATH", "grok").strip() or "grok",
            grok_timeout_seconds=_number_from_env("GROK_TIMEOUT_SECONDS", "30", float),
            grok_reaction_chat_types=tuple(
                item.strip()
                for item in os.getenv("GROK_REACTION_CHAT_TYPES", "group,supergroup").split(",")
                if item.strip()
            ),
            grok_reaction_chat_ids=_parse_grok_chat_ids(os.getenv("GROK_REACTION_CHAT_IDS", "")),
        )
