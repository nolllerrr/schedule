"""Pure, bounded schedule normalization and local Pillow rendering.

No repository, Telegram or network access belongs here. The caller runs this
CPU-bound module in a worker thread and keeps text available on unsupported data.
"""
from __future__ import annotations

import io
import re
import struct
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from schedule_bot.domain import Lesson, LessonSegment
from schedule_bot.presentation import RUSSIAN_MONTHS, RUSSIAN_WEEKDAYS

TEMPLATE_VERSION = "schedule-cards-v2.1-six"
IMAGE_SIZE = (1080, 1350)
SLOT_COUNT = 6
SLOT_TOP = 470
SLOT_HEIGHT = 116
SLOT_STEP = 128
FONT_DIRECTORY = Path(__file__).parent / "assets" / "fonts"


class CardNotSupported(ValueError):
    """The data cannot fit the fixed card without losing information."""


@dataclass(frozen=True, slots=True)
class CardEntry:
    time_label: str
    segments: tuple[LessonSegment, ...]
    label: str


@dataclass(frozen=True, slots=True)
class CardSlot:
    number: int
    entry: CardEntry | None = None

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return slot_bounds(self.number)


@dataclass(frozen=True, slots=True)
class CardModel:
    role: str
    target: str
    lesson_date: date
    slots: tuple[CardSlot, ...]
    lesson_count: int
    window_count: int
    time_range: str | None
    class_hour: CardEntry | None = None


def slot_bounds(number: int) -> tuple[int, int, int, int]:
    if not 1 <= number <= SLOT_COUNT:
        raise CardNotSupported("Номер пары вне сетки карточки.")
    top = SLOT_TOP + (number - 1) * SLOT_STEP
    return (112, top, 1024, top + SLOT_HEIGHT)


@lru_cache(maxsize=1)
def _supported_characters() -> frozenset[int]:
    """Read the bundled TrueType cmap; reject missing glyphs instead of tofu.

    The bundled Noto Sans uses a Unicode format-4 cmap. Format 12 is also
    supported so replacing the licensed font does not require another library.
    """
    data = (FONT_DIRECTORY / "NotoSans-Regular.ttf").read_bytes()
    count = struct.unpack_from(">H", data, 4)[0]
    cmap = None
    for index in range(count):
        tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if tag == b"cmap":
            cmap = offset
            break
    if cmap is None:
        raise RuntimeError("Bundled font has no cmap")
    characters: set[int] = set()
    tables = struct.unpack_from(">H", data, cmap + 2)[0]
    for index in range(tables):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + index * 8)
        if platform != 0 and not (platform == 3 and encoding in (1, 10)):
            continue
        start = cmap + offset
        fmt = struct.unpack_from(">H", data, start)[0]
        if fmt == 12:
            groups = struct.unpack_from(">I", data, start + 12)[0]
            for group in range(groups):
                first, last, glyph = struct.unpack_from(">III", data, start + 16 + group * 12)
                characters.update(range(first + (glyph == 0), last + 1))
        elif fmt == 4:
            segments = struct.unpack_from(">H", data, start + 6)[0] // 2
            ends = start + 14
            starts = ends + segments * 2 + 2
            deltas = starts + segments * 2
            offsets = deltas + segments * 2
            for segment in range(segments):
                first = struct.unpack_from(">H", data, starts + segment * 2)[0]
                last = struct.unpack_from(">H", data, ends + segment * 2)[0]
                delta = struct.unpack_from(">h", data, deltas + segment * 2)[0]
                relative = struct.unpack_from(">H", data, offsets + segment * 2)[0]
                for codepoint in range(first, min(last, 65534) + 1):
                    glyph = ((codepoint + delta) & 0xffff) if not relative else (
                        struct.unpack_from(">H", data, offsets + segment * 2 + relative + 2 * (codepoint - first))[0]
                    )
                    if glyph:
                        characters.add(codepoint)
    return frozenset(characters)


def _field(value: str | None, *, limit: int = 320) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise CardNotSupported("Слишком длинные или некорректные поля расписания.")
    # Whitespace normalization changes no words or assignment relationships.
    value = " ".join(value.split())
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise CardNotSupported("В расписании есть неподдерживаемые символы.")
    if any(ord(character) not in _supported_characters() for character in value):
        raise CardNotSupported("В расписании есть символы, для которых нет шрифта.")
    return value or None


def _time(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
    ):
        raise CardNotSupported("Не удалось точно отобразить время занятия.")
    return value


def _time_label(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start}–{end}"
    if start:
        return f"с {start}"
    if end:
        return f"до {end}"
    return "время не указано"


def normalize_schedule(
    lessons: Sequence[Lesson], *, role: str, target: str, lesson_date: date,
) -> CardModel:
    """Preserve pair numbers and segment links; never invent slots or times.

    An empty result cannot prove a day off even on a published date, so v1
    deliberately uses the existing text view for every empty result.
    """
    if role != "student":
        raise CardNotSupported("Карточки пока доступны только для учебных групп.")
    target = _field(target, limit=80) or ""
    if not target or not lessons or len(lessons) > SLOT_COUNT + 1:
        raise CardNotSupported("Нет занятий для карточки или день не помещается в сетку.")
    entries: dict[int, CardEntry] = {}
    class_hour: CardEntry | None = None
    numbered: dict[int, Lesson] = {}
    for lesson in lessons:
        if lesson.lesson_date != lesson_date or lesson.group_name != target:
            raise CardNotSupported("Данные занятия не соответствуют выбранному дню и группе.")
        label = _field(lesson.lesson_label, limit=80) or ""
        subject = _field(lesson.subject) or ""
        start, end = _time(lesson.start_time), _time(lesson.end_time)
        if start and end and start >= end:
            raise CardNotSupported("Некорректный интервал занятия.")
        if len(lesson.segments) > 2:
            raise CardNotSupported("Эта пара содержит слишком много подгрупп для карточки.")
        parts = tuple(
            LessonSegment(
                _field(part.subject) or "Предмет не указан",
                _field(part.teacher, limit=160), _field(part.room, limit=100),
                _field(part.subgroup, limit=40),
            )
            for part in lesson.effective_segments()
        )
        entry = CardEntry(_time_label(start, end), parts, label)
        is_class_hour = any(
            "".join(value.casefold().split()).replace(".", "") in {"клчас", "классныйчас"}
            for value in (label, subject)
        )
        if is_class_hour:
            if class_hour is not None or len(parts) != 1:
                raise CardNotSupported("Классные часы не помещаются в компактную плашку.")
            class_hour = entry
            continue
        number = lesson.lesson_number
        if type(number) is not int or not 1 <= number <= SLOT_COUNT or number in entries:
            raise CardNotSupported("Номер пары не помещается в сетку или повторяется.")
        if label and not re.fullmatch(rf"0?{number}(?:\s*пара)?", label, flags=re.IGNORECASE):
            raise CardNotSupported("Необычную метку пары лучше показать текстом.")
        entries[number] = entry
        numbered[number] = lesson
    occupied = sorted(entries)
    windows = sum(number not in entries for number in range(occupied[0], occupied[-1])) if occupied else 0
    for attribute in ("start_time", "end_time"):
        known = [getattr(numbered[number], attribute) for number in occupied if getattr(numbered[number], attribute)]
        if known != sorted(known):
            raise CardNotSupported("Время пар не соответствует их порядку.")
    # Only claim a complete interval when both endpoints of every real pair
    # are known. Class hour is deliberately excluded from pair statistics.
    time_range = None
    if occupied and all(numbered[number].start_time and numbered[number].end_time for number in occupied):
        time_range = f"{numbered[occupied[0]].start_time}–{numbered[occupied[-1]].end_time}"
    return CardModel(
        role, target, lesson_date,
        tuple(CardSlot(number, entries.get(number)) for number in range(1, SLOT_COUNT + 1)),
        len(entries), windows, time_range, class_hour,
    )


class _Typography:
    def __init__(self) -> None:
        self.fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self.fonts:
            filename = "NotoSans-SemiBold.ttf" if bold else "NotoSans-Regular.ttf"
            self.fonts[key] = ImageFont.truetype(str(FONT_DIRECTORY / filename), size)
        return self.fonts[key]

    def fit(
        self, text: str, width: int, *, sizes: range, lines: int = 1,
        bold: bool = False, max_height: int = 100,
    ):
        """Return complete measured text, or refuse the card. Never ellipsize."""
        for size in sizes:
            font = self.font(size, bold)
            wrapped = _wrap(text, font, width)
            bounds = [font.getbbox(line, anchor="lt") for line in wrapped]
            ink_fits = all(right - left <= width for left, _, right, _ in bounds)
            height = max(index * (size + 4) + bottom - top for index, (_, top, _, bottom) in enumerate(bounds))
            if len(wrapped) <= lines and ink_fits and height <= max_height:
                return wrapped, font, size
        raise CardNotSupported("В карточке недостаточно места для полного текста занятия.")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    result: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            result.append(line)
            line = ""
        for character in word:
            if font.getlength(line + character) > width:
                if not line:
                    raise CardNotSupported("Символ не помещается в карточку.")
                result.append(line)
                line = ""
            line += character
    if line:
        result.append(line)
    return result or [""]


def _text(draw, xy, text, *, font, fill):
    # Anchor the actual ink, including negative side bearings of combining marks.
    left, top, _, _ = font.getbbox(text, anchor="lt")
    draw.text((xy[0] - left, xy[1] - top), text, font=font, fill=fill, anchor="lt")


def _room_label(room: str | None) -> str:
    if not room:
        return "аудитория не указана"
    if "".join(room.casefold().split()) in {"спортзал", "сп/з", "сп/зал"}:
        return room
    return f"ауд. {room}"


def _draw_segment(draw, typeface, part, x, top, width, *, split):
    sizes = range(23, 19, -1) if split else range(30, 23, -1)
    lines, font, size = typeface.fit(
        part.subject, width, sizes=sizes, lines=2, bold=True,
        max_height=52 if split else 68,
    )
    for index, line in enumerate(lines):
        _text(draw, (x, top + index * (size + 4)), line, font=font, fill="#edf5ff")
    if split:
        teacher = part.teacher or "преподаватель не указан"
        teacher_lines, teacher_font, _ = typeface.fit(teacher, width, sizes=range(18, 16, -1), max_height=22)
        _text(draw, (x, top + 57), teacher_lines[0], font=teacher_font, fill="#a5b9d0")
        details = f"подгр. {part.subgroup} · " if part.subgroup else ""
        details += _room_label(part.room)
        details_lines, details_font, _ = typeface.fit(details, width, sizes=range(18, 16, -1), max_height=22)
        _text(draw, (x, top + 81), details_lines[0], font=details_font, fill="#83a4bd")
    else:
        details = [part.teacher or "преподаватель не указан", _room_label(part.room)]
        if part.subgroup:
            details.append(f"подгр. {part.subgroup}")
        metadata, metadata_font, _ = typeface.fit(" · ".join(details), width, sizes=range(21, 17, -1), max_height=24)
        _text(draw, (x, top + 77), metadata[0], font=metadata_font, fill="#9fb6cd")


def render_card(model: CardModel) -> bytes:
    """Render a normalized card as PNG. Caller must use a worker thread."""
    if len(model.slots) != SLOT_COUNT or tuple(slot.number for slot in model.slots) != tuple(range(1, SLOT_COUNT + 1)):
        raise CardNotSupported("Некорректная сетка карточки.")
    image = Image.new("RGB", IMAGE_SIZE, "#081323")
    draw = ImageDraw.Draw(image)
    # A restrained top-right glow, rasterized without a browser or external API.
    for row in range(IMAGE_SIZE[1]):
        strength = max(0.0, 1.0 - row / 880)
        color = (8 + round(3 * strength), 19 + round(9 * strength), 35 + round(15 * strength))
        draw.line((0, row, IMAGE_SIZE[0], row), fill=color)
    typeface = _Typography()
    accent = "#68d6ed"
    muted = "#829bb7"
    # Small code-native schedule mark.
    draw.rounded_rectangle((56, 56, 88, 88), radius=9, fill="#193c50")
    for column in (65, 74):
        draw.line((column, 65, column, 79), fill=accent, width=3)
    _text(draw, (102, 50), "schedule", font=typeface.font(36, True), fill="#f0f7ff")
    _text(draw, (57, 103), "расписание техникума", font=typeface.font(20), fill=muted)
    group_lines, group_font, _ = typeface.fit(model.target, 218, sizes=range(29, 18, -1), bold=True)
    draw.rounded_rectangle((762, 54, 1024, 110), radius=18, fill="#123044", outline="#295268")
    group_width = group_font.getlength(group_lines[0])
    _text(draw, (893 - group_width / 2, 69), group_lines[0], font=group_font, fill=accent)
    _text(draw, (56, 161), RUSSIAN_WEEKDAYS[model.lesson_date.weekday()].upper(), font=typeface.font(21, True), fill=accent)
    stamp = f"{model.lesson_date.day} {RUSSIAN_MONTHS[model.lesson_date.month]}"
    _text(draw, (53, 201), stamp, font=typeface.font(62, True), fill="#f2f7ff")
    _text(draw, (58, 277), model.time_range or "Интервал занятий не указан", font=typeface.font(22), fill=muted)
    metrics = (
        ("ПАР", str(model.lesson_count)),
        ("ИНТЕРВАЛ ЗАНЯТИЙ", model.time_range or "не указан"),
        ("ОКОН", str(model.window_count)),
    )
    for index, (label, value) in enumerate(metrics):
        left, right = (56, 303, 790)[index], (287, 774, 1024)[index]
        draw.rounded_rectangle((left, 327, right, 407), radius=17, fill="#112237", outline="#20364e")
        _text(draw, (left + 20, 341), value, font=typeface.font(28, True), fill="#e4f1ff")
        _text(draw, (left + 21, 379), label, font=typeface.font(14, True), fill=muted)
    _text(draw, (56, 432), "Пары", font=typeface.font(26, True), fill="#eaf3ff")
    _text(draw, (886, 439), "НА ВЕСЬ ДЕНЬ", font=typeface.font(14), fill="#5e7895")
    first_center = SLOT_TOP + SLOT_HEIGHT // 2
    last_center = first_center + SLOT_STEP * (SLOT_COUNT - 1)
    draw.line((77, first_center, 77, last_center), fill="#244d65", width=2)
    for slot in model.slots:
        left, top, right, bottom = slot.bounds
        center = top + SLOT_HEIGHT // 2
        if slot.entry is None:
            draw.rounded_rectangle(slot.bounds, radius=19, fill="#0b182a", outline="#182a40")
            draw.ellipse((72, center - 5, 82, center + 5), fill="#2d4a61")
            _text(draw, (left + 24, top + 38), f"{slot.number:02d}", font=typeface.font(29, True), fill="#35516c")
            _text(draw, (299, top + 44), "нет пары", font=typeface.font(23), fill="#48637d")
            continue
        draw.rounded_rectangle(slot.bounds, radius=19, fill="#14283e", outline="#28465e")
        draw.rounded_rectangle((left, top + 22, left + 3, bottom - 22), radius=2, fill="#5bc8e4")
        draw.ellipse((68, center - 9, 86, center + 9), fill="#15354a", outline="#62d3ed", width=3)
        _text(draw, (left + 24, top + 23), f"{slot.number:02d}", font=typeface.font(33, True), fill=accent)
        time_lines, time_font, _ = typeface.fit(slot.entry.time_label, 148, sizes=range(18, 15, -1), lines=2)
        for index, line in enumerate(time_lines):
            _text(draw, (left + 24, top + 70 + index * 19), line, font=time_font, fill="#87a9c3")
        draw.line((278, top + 19, 278, bottom - 19), fill="#29445b")
        if len(slot.entry.segments) == 1:
            _draw_segment(draw, typeface, slot.entry.segments[0], 299, top + 13, 701, split=False)
        elif len(slot.entry.segments) == 2:
            draw.line((652, top + 16, 652, bottom - 16), fill="#29445b")
            for part_index, part in enumerate(slot.entry.segments):
                _draw_segment(draw, typeface, part, 299 + part_index * 370, top + 10, 330, split=True)
        else:
            raise CardNotSupported("Некорректные подгруппы.")
    if model.class_hour is not None:
        part = model.class_hour.segments[0]
        details = ["Классный час", model.class_hour.time_label]
        if part.subject.casefold().replace(" ", "").replace(".", "") not in {"клчас", "классныйчас"}:
            details.append(part.subject)
        details += [part.teacher or "преподаватель не указан", _room_label(part.room)]
        if part.subgroup:
            details.append(f"подгр. {part.subgroup}")
        lines, font, size = typeface.fit(" · ".join(details), 918, sizes=range(19, 16, -1), lines=2, max_height=43)
        draw.rounded_rectangle((56, 1239, 1024, 1293), radius=13, fill="#10293a", outline="#244b60")
        for index, line in enumerate(lines):
            _text(draw, (76, 1246 + index * (size + 4)), line, font=font, fill="#a9cbdc")
    draw.line((56, 1310, 1024, 1310), fill="#20364a")
    _text(draw, (56, 1320), "schedule  /  расписание на день", font=typeface.font(15), fill="#57718c")
    _text(draw, (920, 1320), str(model.lesson_date.year), font=typeface.font(15), fill="#57718c")
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()
