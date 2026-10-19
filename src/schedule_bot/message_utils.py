from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from html.parser import HTMLParser

if TYPE_CHECKING:
    from aiogram.types import Message


def _utf16_size(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _MessageHTML(HTMLParser):
    """Normalize supported Telegram markup into tag and decoded text events."""
    tags = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
            "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji"}
    attributes = {"a": {"href"}, "span": {"class"}, "code": {"class"},
                  "blockquote": {"expandable"}, "tg-emoji": {"emoji-id"}}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
        elif tag in self.tags:
            safe_attrs = "".join(
                f' {name}="{html.escape(value or "", quote=True)}"'
                for name, value in attrs if name in self.attributes.get(tag, set())
            )
            self.events.append(("start", tag, f"<{tag}{safe_attrs}>"))

    def handle_endtag(self, tag):
        if tag in self.tags:
            self.events.append(("end", tag, ""))

    def handle_data(self, data):
        self.events.append(("text", "", data))


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Split HTML with balanced tags and a UTF-16 budget for rendered text.

    Telegram applies its text limit after parsing entities. A code point is
    never cut in half; entities are decoded before splitting and escaped again.
    """
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("Message limit must be a positive integer")
    parser = _MessageHTML()
    parser.feed(text)
    parser.close()
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []
    current: list[str] = []
    used = 0

    def flush():
        nonlocal current, used
        if used:
            chunks.append("".join(current) + "".join(f"</{tag}>" for tag, _ in reversed(stack)))
        current = [opening for _, opening in stack]
        used = 0

    for kind, tag, value in parser.events:
        if kind == "start":
            stack.append((tag, value))
            current.append(value)
        elif kind == "end":
            matching = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == tag), None)
            if matching is not None:
                while len(stack) > matching:
                    closing, _ = stack.pop()
                    current.append(f"</{closing}>")
        else:
            for character in value:
                width = _utf16_size(character)
                if width > limit:
                    raise ValueError("Message limit cannot hold one Unicode character")
                if used + width > limit:
                    flush()
                current.append(html.escape(character, quote=False))
                used += width
    flush()
    return chunks or [""]


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
