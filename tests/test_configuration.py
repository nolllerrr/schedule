from pathlib import Path

import pytest

from schedule_bot import config


def test_example_environment_does_not_contain_secrets() -> None:
    values: dict[str, str] = {}
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    assert values["BOT_TOKEN"] == ""
    assert values["ADMIN_TELEGRAM_IDS"] == ""
    assert values["SCHEDULE_HTTP_MAX_ATTEMPTS"] == "4"
    assert values["SCHEDULE_HTTP_RETRY_MAX_DELAY"] == "15"
    assert values["SCHEDULE_OUTAGE_FAILURE_THRESHOLD"] == "3"


@pytest.fixture
def clean_settings_env(monkeypatch) -> None:
    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    for name in (
        "SCHEDULE_HTTP_MAX_ATTEMPTS",
        "SCHEDULE_HTTP_RETRY_MAX_DELAY",
        "SCHEDULE_OUTAGE_FAILURE_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_schedule_resilience_defaults_are_backward_compatible(clean_settings_env):
    settings = config.Settings.from_env(require_bot_token=False)
    assert settings.schedule_http_max_attempts == 4
    assert settings.schedule_http_retry_max_delay == 15.0
    assert settings.schedule_outage_failure_threshold == 3


def test_schedule_resilience_settings_from_env(clean_settings_env, monkeypatch):
    monkeypatch.setenv("SCHEDULE_HTTP_MAX_ATTEMPTS", "6")
    monkeypatch.setenv("SCHEDULE_HTTP_RETRY_MAX_DELAY", "12.5")
    monkeypatch.setenv("SCHEDULE_OUTAGE_FAILURE_THRESHOLD", "2")
    settings = config.Settings.from_env(require_bot_token=False)
    assert settings.schedule_http_max_attempts == 6
    assert settings.schedule_http_retry_max_delay == 12.5
    assert settings.schedule_outage_failure_threshold == 2


@pytest.mark.parametrize(
    "name,value",
    [
        ("SCHEDULE_HTTP_MAX_ATTEMPTS", "0"),
        ("SCHEDULE_HTTP_MAX_ATTEMPTS", "11"),
        ("SCHEDULE_HTTP_MAX_ATTEMPTS", "1.5"),
        ("SCHEDULE_HTTP_MAX_ATTEMPTS", "private-secret"),
        ("SCHEDULE_HTTP_RETRY_MAX_DELAY", "-1"),
        ("SCHEDULE_HTTP_RETRY_MAX_DELAY", "61"),
        ("SCHEDULE_HTTP_RETRY_MAX_DELAY", "nan"),
        ("SCHEDULE_HTTP_RETRY_MAX_DELAY", "inf"),
        ("SCHEDULE_HTTP_RETRY_MAX_DELAY", "private-secret"),
        ("SCHEDULE_OUTAGE_FAILURE_THRESHOLD", "0"),
        ("SCHEDULE_OUTAGE_FAILURE_THRESHOLD", "101"),
        ("SCHEDULE_OUTAGE_FAILURE_THRESHOLD", "private-secret"),
    ],
)
def test_invalid_resilience_settings_are_rejected_safely(
    clean_settings_env, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as raised:
        config.Settings.from_env(require_bot_token=False)
    assert name in str(raised.value)
    assert "private-secret" not in str(raised.value)


def test_settings_repr_hides_tokens_proxy_credentials_and_salt(
    clean_settings_env, monkeypatch
):
    monkeypatch.setenv("BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://name:telegram-secret@proxy.test")
    monkeypatch.setenv("SCHEDULE_PROXY_URL", "http://name:schedule-secret@proxy.test")
    monkeypatch.setenv("ANALYTICS_SALT", "analytics-secret")
    monkeypatch.setenv("SCHEDULE_PAGE_URL", "https://user:page-secret@source.test/?token=query-secret")
    settings = config.Settings.from_env()
    for secret in ("bot-secret", "telegram-secret", "schedule-secret", "analytics-secret", "page-secret", "query-secret"):
        assert secret not in repr(settings)

