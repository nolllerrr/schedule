from __future__ import annotations

import html
from typing import Any

from aiogram.types import Message
from bs4 import BeautifulSoup


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) > limit:
            plain = html.escape(
                BeautifulSoup(paragraph, "html.parser").get_text("\n")
            )
            while len(plain) > limit:
                chunks.append(plain[:limit])
                plain = plain[limit:]
            paragraph = plain
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def answer_in_chunks(
    message: Message,
    text: str,
    *,
    reply_markup: Any = None,
) -> list[Message]:
    chunks = split_message(text)
    sent: list[Message] = []
    for index, chunk in enumerate(chunks):
        sent.append(
            await message.answer(
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        )
    return sent
