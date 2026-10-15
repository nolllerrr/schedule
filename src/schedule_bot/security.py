"""Redact credentials while retaining useful network-error diagnostics."""

from __future__ import annotations

import re


_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
_TELEGRAM_TOKEN = re.compile(r"(?<!\d)\d{5,16}(?::|%3[aA])[a-zA-Z0-9_-]{20,}")
_TELEGRAM_PATH = re.compile(r"(/(?:file/)?bot)[^/]+", re.IGNORECASE)
_REDACTED = "[REDACTED]"


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    # Keep surrounding prose/traceback punctuation outside the redacted URL.
    stripped = url.rstrip(").,;}")
    suffix = url[len(stripped) :]
    scheme, remainder = stripped.split("://", 1)
    before_fragment, fragment_marker, _ = remainder.partition("#")
    before_query, query_marker, _ = before_fragment.partition("?")
    authority, path_marker, path = before_query.partition("/")
    if "@" in authority:
        authority = f"{_REDACTED}@{authority.rsplit('@', 1)[1]}"
    hostname = authority.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if hostname == "api.telegram.org":
        path = _TELEGRAM_PATH.sub(
            lambda token_match: f"{token_match.group(1)}{_REDACTED}",
            f"/{path}" if path_marker else "",
        ).lstrip("/")
    result = f"{scheme}://{authority}"
    if path_marker:
        result += f"/{path}"
    if query_marker:
        result += f"?{_REDACTED}"
    if fragment_marker:
        result += f"#{_REDACTED}"
    return result + suffix


def redact_secrets(text: str) -> str:
    """Remove URL credentials, queries/fragments and Telegram bot tokens.

    Redact the entire query and fragment: credentials can use arbitrary parameter
    names, and HTTPX logs requests even when they do not fail. Never inspect the
    environment or dotenv files while formatting an exception.
    """
    return _TELEGRAM_TOKEN.sub(_REDACTED, _URL.sub(_redact_url, text))


def safe_exception_text(error: BaseException) -> str:
    """Return a safe exception message; callers can prepend its type."""
    return redact_secrets(str(error))
