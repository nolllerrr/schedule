from types import SimpleNamespace

import pytest

from schedule_bot.bot.app import private_help
from schedule_bot.bot.groups import group_help


class FakeMessage:
    def __init__(self, user_id: int = 123) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_private_help_lists_user_commands() -> None:
    message = FakeMessage()
    settings = SimpleNamespace(admin_telegram_ids=())

    await private_help(message, settings)  # type: ignore[arg-type]

    text = message.answers[0]
    assert "/start" in text
    assert "/menu" in text
    assert "/help" in text
    assert "/privacy" in text
    assert "/delete_me" in text
    assert "/update" not in text


@pytest.mark.asyncio
async def test_private_help_adds_admin_commands_for_owner() -> None:
    message = FakeMessage(user_id=42)
    settings = SimpleNamespace(admin_telegram_ids=(42,))

    await private_help(message, settings)  # type: ignore[arg-type]

    assert "/update" in message.answers[0]
    assert "/stats 30" in message.answers[0]


@pytest.mark.asyncio
async def test_group_help_lists_only_group_commands() -> None:
    message = FakeMessage()

    await group_help(message)  # type: ignore[arg-type]

    text = message.answers[0]
    assert "/menu" in text
    assert "/setup" in text
    assert "/help" in text
    assert "/next" not in text
    assert "/pin" not in text
