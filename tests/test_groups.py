from aiogram.enums import ChatType

from schedule_bot.bot.groups import chat_type_value


def test_chat_type_value_accepts_string_from_aiogram_model() -> None:
    assert chat_type_value("supergroup") == "supergroup"


def test_chat_type_value_accepts_enum_for_compatibility() -> None:
    assert chat_type_value(ChatType.GROUP) == "group"
