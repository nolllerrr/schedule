"""Allowlisted administrator diagnostics without exception text or URLs."""
from __future__ import annotations

import re
import sqlite3
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

import httpx

from schedule_bot.downloader import ScheduleDownloadError
from schedule_bot.import_validation import ImportQuarantinedError
from schedule_bot.parser import ScheduleParseError

_STAGES = {
    "update": "проверка и импорт", "source": "источник", "page": "страница источника",
    "workbook": "загрузка Excel", "fetch": "загрузка источника", "parse": "разбор Excel",
    "import": "импорт расписания", "database": "база данных", "delivery": "доставка уведомлений",
    "pin": "закрепление расписания", "pins": "закрепление расписания",
}
_TIMEOUTS = {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "timeout",
             "connect_timeout", "read_timeout"}
_NETWORK = {"ConnectError", "ReadError", "WriteError", "CloseError", "RemoteProtocolError",
            "connect_error", "network_error", "remote_protocol_error"}


def safe_update_diagnostic(error: BaseException, *, stage: str = "update") -> str:
    """Classify exception types/controlled metadata; never format user data."""
    chain = []
    current = error
    while current is not None and len(chain) < 8 and all(current is not old for old in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    declared = next((getattr(item, "schedule_stage", None) for item in chain
                     if getattr(item, "schedule_stage", None) in _STAGES), None)
    stage = declared or (stage if stage in _STAGES else "update")
    code, reason = "INTERNAL_ERROR", "внутренняя ошибка; подробности в журнале"
    if any(isinstance(item, sqlite3.Error) for item in chain):
        code, reason, stage = "DATABASE_ERROR", "ошибка доступа к базе данных", "database"
    elif any(isinstance(item, ImportQuarantinedError) for item in chain):
        code, reason, stage = "IMPORT_QUARANTINED", "файл изолирован проверкой безопасности импорта", "import"
    elif any(isinstance(item, (ScheduleParseError, BadZipFile, ParseError)) for item in chain):
        code, reason, stage = "PARSE_ERROR", "Excel не соответствует поддерживаемому формату", "parse"
    elif any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in chain):
        if stage in {"delivery", "pin", "pins"}:
            code, reason = "TELEGRAM_TIMEOUT", "Telegram не ответил в установленный срок"
        else:
            code, reason = "SOURCE_TIMEOUT", "источник не ответил в установленный срок"
        if stage == "update":
            stage = "fetch"
    elif stage in {"delivery", "pin", "pins"} and type(error).__name__ in {
        "TelegramForbiddenError", "TelegramBadRequest", "TelegramNetworkError"
    }:
        code, reason = {
            "TelegramForbiddenError": ("TELEGRAM_FORBIDDEN", "проверьте доступ бота и права в чате"),
            "TelegramBadRequest": ("TELEGRAM_REJECTED", "Telegram отклонил операцию; подробности в журнале"),
            "TelegramNetworkError": ("TELEGRAM_NETWORK_ERROR", "нет соединения с Telegram"),
        }[type(error).__name__]
    else:
        download = next((item for item in chain if isinstance(item, ScheduleDownloadError)), None)
        if download is not None:
            category = download.category
            if re.fullmatch(r"http_[1-5][0-9]{2}", category):
                code, reason = category.upper(), f"источник вернул HTTP {category[-3:]}"
            elif category in _TIMEOUTS:
                code, reason = "SOURCE_TIMEOUT", "источник не ответил в установленный срок"
            elif category in _NETWORK:
                code, reason = "SOURCE_NETWORK_ERROR", "не удалось установить или сохранить соединение"
            else:
                code, reason = "SOURCE_DOWNLOAD_ERROR", "не удалось безопасно получить страницу или Excel"
            if stage == "update":
                stage = "fetch"
    return f"Ошибка обновления: {code}\nЭтап: {_STAGES[stage]}.\nПричина: {reason}."
