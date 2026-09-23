from pathlib import Path

from schedule_bot.analytics import UsageAnalytics, format_stats
from schedule_bot.repository import ScheduleRepository


def test_actor_keys_are_stable_and_do_not_expose_id(tmp_path: Path) -> None:
    analytics = UsageAnalytics(ScheduleRepository(tmp_path / "db.sqlite"), "secret")

    first = analytics.actor_key("user", 123456789)
    second = analytics.actor_key("user", 123456789)

    assert first == second
    assert "123456789" not in first
    assert analytics.actor_key("chat", 123456789) != first


def test_stats_formatter_handles_empty_stats() -> None:
    text = format_stats({}, days=7)

    assert "Статистика за 7 дн." in text
    assert "Просмотров расписания: <b>0</b>" in text
    assert "Пустых результатов: 0.0%" in text
    assert "Доставка уведомлений: 0.0%" in text
