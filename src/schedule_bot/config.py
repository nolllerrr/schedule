from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    telegram_proxy_url: str | None
    telegram_retry_seconds: int
    database_path: Path
    schedule_page_url: str
    admin_telegram_ids: tuple[int, ...]
    check_interval_minutes: int
    timezone: str
    downloads_path: Path

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
            telegram_retry_seconds=max(
                5, int(os.getenv("TELEGRAM_RETRY_SECONDS", "15"))
            ),
            database_path=database_path,
            schedule_page_url=os.getenv(
                "SCHEDULE_PAGE_URL", "https://ptgh.onego.ru/9006/"
            ),
            admin_telegram_ids=_parse_admin_ids(
                os.getenv("ADMIN_TELEGRAM_IDS", "")
            ),
            check_interval_minutes=max(
                1, int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
            ),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            downloads_path=Path(os.getenv("DOWNLOADS_PATH", "data/downloads")),
        )
