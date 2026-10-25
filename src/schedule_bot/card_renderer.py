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

TEMPLATE_VERSION = "schedule-cards-v3.2-single-subgroup"
IMAGE_SIZE = (1080, 1620)
RENDER_SCALE = 2
SLOT_COUNT = 6
SLOT_TOP = 510
SLOT_HEIGHT = 160
SLOT_STEP = 176
FONT_DIRECTORY = Path(__file__).parent / "assets" / "fonts"
WHITE = "#F4F8F8"
MUTED = "#9EAFB4"
ACCENT = "#83DFD5"


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
    class_hour_before: int | None = None

    @property
    def image_size(self) -> tuple[int, int]:
        return (IMAGE_SIZE[0], IMAGE_SIZE[1] + (SLOT_STEP if self.class_hour else 0))

    def slot_bounds(self, number: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = slot_bounds(number)
        offset = SLOT_STEP if self.class_hour_before is not None and number >= self.class_hour_before else 0
        return left, top + offset, right, bottom + offset

    @property
    def class_hour_bounds(self) -> tuple[int, int, int, int] | None:
        if self.class_hour_before is None:
            return None
        top = SLOT_TOP + (self.class_hour_before - 1) * SLOT_STEP
        return 136, top, 1024, top + SLOT_HEIGHT


def slot_bounds(number: int) -> tuple[int, int, int, int]:
    if not 1 <= number <= SLOT_COUNT:
        raise CardNotSupported("Номер пары вне сетки карточки.")
    top = SLOT_TOP + (number - 1) * SLOT_STEP
    return (136, top, 1024, top + SLOT_HEIGHT)


@lru_cache(maxsize=1)
def _supported_characters() -> frozenset[int]:
    """Read the bundled OpenType cmap; reject missing glyphs instead of tofu.

    The bundled Nimbus Sans uses a Unicode format-4 cmap. Format 12 is also
    supported so replacing the licensed font does not require another library.
    """
    data = (FONT_DIRECTORY / "font-regular.otf").read_bytes()
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
    class_hour_position: int | None = None
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
                raise CardNotSupported("Для нескольких классных часов доступно текстовое расписание.")
            class_hour = entry
            class_hour_position = lesson.position
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
    class_hour_before = None
    if class_hour is not None:
        positions = [numbered[number].position for number in occupied]
        if (any(type(position) is not int for position in [*positions, class_hour_position])
                or positions != sorted(set(positions)) or class_hour_position in positions):
            raise CardNotSupported("Не удалось определить место классного часа между парами.")
        # Use source positions, including when times are absent or the first
        # ordinary slots are empty. Input iteration order is irrelevant.
        class_hour_before = next(
            (number for number in occupied if numbered[number].position > class_hour_position),
            occupied[-1] + 1 if occupied else 1,
        )
    return CardModel(
        role, target, lesson_date,
        tuple(CardSlot(number, entries.get(number)) for number in range(1, SLOT_COUNT + 1)),
        len(entries), windows, time_range, class_hour, class_hour_before,
    )


class _Typography:
    def __init__(self) -> None:
        self.fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self.fonts:
            filename = "font-bold.otf" if bold else "font-regular.otf"
            self.fonts[key] = ImageFont.truetype(
                str(FONT_DIRECTORY / filename), size * RENDER_SCALE,
            )
        return self.fonts[key]

    def width(self, text: str, size: int, bold: bool = False) -> float:
        return self.font(size, bold).getlength(text) / RENDER_SCALE

    def fit(
        self, text: str, width: float, *, sizes: range, lines: int = 1,
        bold: bool = False, max_height: int = 100,
    ) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
        """Measure complete text in output pixels; never ellipsize or stretch."""
        for size in sizes:
            font = self.font(size, bold)
            wrapped = _wrap(text, font, width * RENDER_SCALE)
            bounds = [font.getbbox(line, anchor="lt") for line in wrapped]
            ink_fits = all(
                max(right, font.getlength(line)) - min(0, left) <= width * RENDER_SCALE
                for line, (left, _, right, _) in zip(wrapped, bounds, strict=True)
            )
            height = max(
                index * (size + 4) + (bottom - top) / RENDER_SCALE
                for index, (_, top, _, bottom) in enumerate(bounds)
            )
            if len(wrapped) <= lines and ink_fits and height <= max_height:
                return wrapped, font, size
        raise CardNotSupported("В карточке недостаточно места для полного текста занятия.")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
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


class _Canvas:
    """Draw the approved layout at 2x using its original text anchors."""

    def __init__(self, image_size: tuple[int, int] = IMAGE_SIZE) -> None:
        self.image = Image.new("RGB", tuple(value * RENDER_SCALE for value in image_size), "#0C191E")
        self.draw = ImageDraw.Draw(self.image)
        self.typeface = _Typography()

    def text(
        self, x: float, y: float, value: str, size: int,
        color: str = WHITE, bold: bool = False, anchor: str = "lt",
    ) -> None:
        self.draw.text(
            (round(x * RENDER_SCALE), round(y * RENDER_SCALE)), value,
            font=self.typeface.font(size, bold), fill=color, anchor=anchor,
        )

    def box(self, coords: tuple[float, float, float, float], radius: int, fill: str) -> None:
        self.draw.rounded_rectangle(
            tuple(round(value * RENDER_SCALE) for value in coords),
            radius=radius * RENDER_SCALE, fill=fill, width=RENDER_SCALE,
        )

    def line(self, x: int, y: int, right: int, color: str) -> None:
        self.draw.line(
            (x * RENDER_SCALE, y * RENDER_SCALE, right * RENDER_SCALE, y * RENDER_SCALE),
            fill=color, width=RENDER_SCALE,
        )

    def fitted_text(
        self, x: float, y: float, value: str, width: float, *, sizes: range,
        color: str = WHITE, bold: bool = False, max_height: int = 100,
        anchor: str = "lt",
    ) -> int:
        lines, _, size = self.typeface.fit(
            value, width, sizes=sizes, bold=bold, max_height=max_height,
        )
        self.text(x, y, lines[0], size, color, bold, anchor)
        return size

    def title(self, x: int, y: int, value: str) -> None:
        try:
            lines, _, size = self.typeface.fit(
                value, 836, sizes=range(43, 31, -1), bold=True, max_height=64,
            )
        except CardNotSupported:
            # The title starts at +28; metadata starts at +102. Use the
            # available height while leaving at least 2 px before its badge.
            lines, _, size = self.typeface.fit(
                value, 836, sizes=range(35, 29, -1), lines=2, bold=True, max_height=72,
            )
        for index, line in enumerate(lines):
            self.text(x, y + index * (size + 4), line, size, bold=True)


def lesson_count_label(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        noun = "пара"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        noun = "пары"
    else:
        noun = "пар"
    return f"{count} {noun}"


def _room_parts(room: str | None) -> tuple[str, str]:
    """Keep room identifiers and normalize existing classroom markers once."""
    value = " ".join((room or "").split())
    marker = r"(?:аудитория|ауд)\.?"
    explicit = False
    while value:
        cleaned = re.sub(rf"^{marker}(?=\s|$|\d)\s*", "", value, flags=re.IGNORECASE)
        cleaned = re.sub(rf"\s+{marker}$", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned == value:
            break
        explicit = True
        value = cleaned
    gym = value.casefold().replace(" ", "") in {"спортзал", "сп/з", "сп/зал"}
    suffix = " ауд." if value and not gym and (explicit or value[0].isdecimal()) else ""
    return value, suffix


def _draw_entry(c: _Canvas, entry: CardEntry, top: int, *, class_hour: bool = False) -> None:
    part = entry.segments[0]
    title = part.subject
    if class_hour:
        title = "Классный час"
        if "".join(part.subject.casefold().split()).replace(".", "") not in {"клчас", "классныйчас"}:
            title += f" · {part.subject}"
    if part.subgroup:
        # The Excel subject often already carries the subgroup marker. Keep it
        # once; otherwise make the domain label explicit in the measured title.
        markers = sorted(set(re.findall(r"\b([12])\s*п\s*/?\s*гр\.?", title, flags=re.IGNORECASE)))
        if part.subgroup != ",".join(markers):
            title += f" · подгр. {part.subgroup}"
    c.title(162, top + 28, title)
    # Reserve the time and a visible gap before fitting room and teacher.
    _, _, time_size = c.typeface.fit(
        entry.time_label, 260, sizes=range(26, 21, -1), max_height=30,
    )
    time_left = 998 - c.typeface.width(entry.time_label, time_size)
    available = time_left - 24 - 162
    room, suffix = _room_parts(part.room)
    teacher = part.teacher or ""
    room_width = 0.0
    room_size = 27
    teacher_size = 25
    if room:
        for room_size in range(27, 22, -1):
            # The regular-width badge is part of the approved reference.
            actual_width = (
                c.typeface.width(room, room_size, bool(suffix))
                + c.typeface.width(suffix, room_size)
            )
            room_width = max(c.typeface.width(room + suffix, room_size), actual_width) + 26
            room_font = c.typeface.font(room_size, bool(suffix))
            _, ink_top, _, ink_bottom = room_font.getbbox(room, anchor="lt")
            if (ink_bottom - ink_top) / RENDER_SCALE > (33 if suffix else 28):
                continue
            if room_width > available:
                continue
            if teacher:
                try:
                    _, _, teacher_size = c.typeface.fit(
                        teacher, available - room_width - 18,
                        sizes=range(25, 20, -1), max_height=28,
                    )
                except CardNotSupported:
                    continue
            break
        else:
            raise CardNotSupported("Аудитория и преподаватель не помещаются рядом со временем.")
        c.box((162, top + 102, 162 + room_width, top + 144), 12, "#26454B")
        c.text(175, top + (111 if suffix else 116), room, room_size,
               WHITE if suffix else MUTED, bold=bool(suffix))
        if suffix:
            offset = c.typeface.width(room, room_size, True)
            c.text(175 + offset, top + 116, suffix, room_size, MUTED)
    elif teacher:
        _, _, teacher_size = c.typeface.fit(
            teacher, available, sizes=range(25, 20, -1), max_height=28,
        )
    if teacher:
        c.text(180 + room_width if room else 162, top + 113, teacher, teacher_size, MUTED)
    c.text(998, top + 113, entry.time_label, time_size, MUTED, anchor="rt")


def render_card(model: CardModel) -> bytes:
    """Render real schedule fields in the approved layout as PNG bytes."""
    if len(model.slots) != SLOT_COUNT or tuple(slot.number for slot in model.slots) != tuple(range(1, SLOT_COUNT + 1)):
        raise CardNotSupported("Некорректная сетка карточки.")
    if ((model.class_hour is None) != (model.class_hour_before is None)
            or (model.class_hour_before is not None and (
                type(model.class_hour_before) is not int or not 1 <= model.class_hour_before <= SLOT_COUNT + 1
            ))):
        raise CardNotSupported("Не удалось определить место классного часа между парами.")
    if any(
        slot.entry is not None and len(slot.entry.segments) != 1 for slot in model.slots
    ) or (model.class_hour is not None and len(model.class_hour.segments) != 1):
        # Parallel assignments still require their complete linked text view.
        raise CardNotSupported("Несколько подгрупп в одном слоте доступны в текстовом расписании.")
    c = _Canvas(model.image_size)
    c.box((56, 54, 94, 92), 11, "#20383D")
    c.line(66, 69, 84, ACCENT)
    c.line(66, 78, 78, ACCENT)
    c.text(110, 52, "schedule", 43, bold=True)
    c.box((849, 50, 1024, 112), 24, "#20383D")
    c.fitted_text(936, 82, model.target, 149, sizes=range(32, 21, -1), bold=True, anchor="mm")
    stamp = f"{model.lesson_date.day} {RUSSIAN_MONTHS[model.lesson_date.month]}"
    c.text(56, 161, stamp, 76, bold=True)
    c.text(56, 260, RUSSIAN_WEEKDAYS[model.lesson_date.weekday()].upper(), 22, ACCENT)
    description = "Плотный учебный день" if model.lesson_count >= 5 else "Учебный день"
    c.text(1024, 260, description, 25, MUTED, anchor="rt")
    c.box((56, 324, 328, 404), 22, "#183036")
    c.text(80, 348, lesson_count_label(model.lesson_count), 34, bold=True)
    c.box((346, 324, 1024, 404), 22, "#183036")
    c.text(685, 364, model.time_range or "Интервал не указан", 35, bold=True, anchor="mm")
    c.text(56, 450, "Пары", 35, bold=True)
    c.text(1024, 460, "01–06", 23, MUTED, anchor="rt")

    def draw_class_hour() -> None:
        if model.class_hour is not None and model.class_hour_bounds is not None:
            c.box(model.class_hour_bounds, 24, "#20383D")
            _draw_entry(c, model.class_hour, model.class_hour_bounds[1], class_hour=True)

    for slot in model.slots:
        if slot.number == model.class_hour_before:
            draw_class_hour()
        bounds = model.slot_bounds(slot.number)
        _, top, _, _ = bounds
        c.text(56, top + 28, f"{slot.number:02}", 34,
               ACCENT if slot.entry else "#50676D", bold=True)
        c.box(bounds, 24, "#1A3037" if slot.entry else "#102126")
        if slot.entry:
            _draw_entry(c, slot.entry, top)
        else:
            c.text(580, top + 80, "—", 36, "#50676D", anchor="mm")
    if model.class_hour_before == SLOT_COUNT + 1:
        draw_class_hour()
    output = io.BytesIO()
    c.image.resize(model.image_size, Image.Resampling.LANCZOS).save(output, format="PNG", compress_level=6)
    return output.getvalue()
