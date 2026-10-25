"""Render synthetic notification concepts using the existing card typography.

Run: python scripts/preview_change_cards.py [--output docs/examples/schedule-changes]
These are design examples; they do not change notification delivery.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schedule_bot.card_renderer import (  # noqa: E402
    ACCENT, FONT_DIRECTORY, MUTED, RENDER_SCALE, WHITE, _Canvas,
)


SIZE = (1080, 1200)


@dataclass(frozen=True)
class Example:
    name: str
    title: str
    status: str
    color: str
    tint: str
    subject: str
    teacher: str
    number: int
    before: tuple[str, str, str] | None
    after: tuple[str, str, str] | None
    note: str


EXAMPLES = (
    Example(
        "modified", "Пара изменилась", "Изменены время и аудитория", "#E7C586", "#353225",
        "Математика", "Иванов И. И.", 2,
        ("2 пара", "10:30–11:40", "24 ауд."), ("2 пара", "11:50–13:00", "45 ауд."),
        "Предмет и преподаватель остались прежними.",
    ),
    Example(
        "added", "Добавлена пара", "Новое занятие в расписании", "#83DFB5", "#18372F",
        "Базы данных", "Петров П. П.", 3,
        None, ("3 пара", "13:00–14:00", "31 ауд."),
        "Занятие добавлено в расписание на 26 октября.",
    ),
    Example(
        "removed", "Пара убрана", "Занятие удалено из расписания", "#EAA6A5", "#3A282D",
        "Иностранный язык", "Смирнова Е. А.", 2,
        ("2 пара", "10:30–11:40", "18 ауд."), None,
        "Этой пары больше нет в расписании на 26 октября.",
    ),
    Example(
        "moved", "Перенос пары", "Со 2-й пары на 3-ю", "#9BC9EF", "#203346",
        "Системное программирование", "Сидоров А. А.", 3,
        ("2 пара", "10:30–11:40", "21 ауд."), ("3 пара", "13:00–14:00", "21 ауд."),
        "Предмет, преподаватель и аудитория без изменений.",
    ),
)


def icon(c: _Canvas, name: str, color: str) -> None:
    def line(points):
        c.draw.line([(x * RENDER_SCALE, y * RENDER_SCALE) for x, y in points],
                    fill=color, width=3 * RENDER_SCALE, joint="curve")

    if name == "added":
        line([(86, 364), (114, 364)])
        line([(100, 350), (100, 378)])
    elif name == "removed":
        line([(86, 364), (114, 364)])
    elif name == "moved":
        line([(84, 364), (116, 364)])
        line([(104, 352), (116, 364), (104, 376)])
    else:
        line([(89, 374), (110, 353), (115, 358), (94, 379), (87, 381), (89, 374)])


def detail_panel(c: _Canvas, left: int, label: str, values, example: Example, *, active: bool) -> None:
    color = example.color if active else MUTED
    c.box((left, 672, left + 472, 1000), 24, example.tint if active else "#14272D")
    c.text(left + 32, 703, label.upper(), 23, color, bold=True)
    if values is None:
        c.text(left + 32, 800, "Нет занятия", 40, color, bold=True)
        c.text(left + 32, 866, "Пара убрана", 27, MUTED)
        return
    number, time, room = values
    c.text(left + 32, 760, number, 29, color)
    c.fitted_text(left + 32, 818, time, 408, sizes=range(44, 34, -1), bold=True)
    room_width = c.typeface.width(room, 30, True) + 32
    c.box((left + 32, 905, left + 32 + room_width, 951), 12,
          "#2B4247" if not active else "#30464A")
    c.text(left + 48, 916, room, 30, WHITE if active else MUTED, bold=True)


def render(example: Example) -> Image.Image:
    c = _Canvas(SIZE)
    c.box((56, 54, 94, 92), 11, "#20383D")
    c.line(66, 69, 84, ACCENT)
    c.line(66, 78, 78, ACCENT)
    c.text(110, 52, "schedule", 43, bold=True)
    c.box((849, 50, 1024, 112), 24, "#20383D")
    c.text(936, 82, "ИС-22", 32, bold=True, anchor="mm")
    c.fitted_text(56, 164, example.title, 968, sizes=range(72, 56, -1), bold=True)
    c.text(56, 260, "26 октября · Понедельник", 30, ACCENT)
    c.box((56, 324, 1024, 404), 22, example.tint)
    icon(c, example.name, example.color)
    c.fitted_text(140, 350, example.status, 850, sizes=range(34, 27, -1),
                  color=example.color, bold=True)

    c.box((56, 440, 1024, 636), 24, "#1A3037")
    c.text(88, 464, f"{example.number:02} / ПАРА", 25, example.color, bold=True)
    c.fitted_text(88, 514, example.subject, 904, sizes=range(48, 32, -1), bold=True)
    c.text(88, 588, example.teacher, 28, MUTED)

    if example.before is None:
        c.box((56, 672, 1024, 1000), 24, example.tint)
        c.text(88, 703, "НОВОЕ ЗАНЯТИЕ", 23, example.color, bold=True)
        c.text(88, 762, example.after[1], 62, bold=True)
        c.text(88, 858, example.after[0], 32, example.color)
        c.box((790, 846, 992, 909), 16, "#29483D")
        c.text(891, 877, example.after[2], 32, bold=True, anchor="mm")
        c.text(88, 930, "26 октября · ИС-22", 28, MUTED)
    else:
        detail_panel(c, 56, "Было", example.before, example, active=False)
        detail_panel(c, 552, "Стало", example.after, example, active=True)

    lines, _, size = c.typeface.fit(example.note, 968, sizes=range(29, 24, -1), lines=2, max_height=72)
    for index, line in enumerate(lines):
        c.text(56, 1040 + index * (size + 6), line, size, MUTED)
    c.line(56, 1122, 1024, "#20383D")
    c.text(56, 1150, "Пример уведомления · Данные вымышлены", 22, "#647D83")
    return c.image.resize(SIZE, Image.Resampling.LANCZOS)


def overview(cards: list[Image.Image]) -> Image.Image:
    result = Image.new("RGB", (1192, 1404), "#101F25")
    draw = ImageDraw.Draw(result)
    bold = ImageFont.truetype(str(FONT_DIRECTORY / "font-bold.otf"), 36)
    regular = ImageFont.truetype(str(FONT_DIRECTORY / "font-regular.otf"), 22)
    draw.text((40, 30), "Изменения в расписании", fill=WHITE, font=bold)
    draw.text((40, 78), "Четыре примера уведомлений · ИС-22", fill=MUTED, font=regular)
    for index, card in enumerate(cards):
        result.paste(card.resize((540, 600), Image.Resampling.LANCZOS),
                     (40 + (index % 2) * 572, 144 + (index // 2) * 628))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/examples/schedule-changes"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cards = []
    for example in EXAMPLES:
        card = render(example)
        destination = args.output / f"{example.name}.png"
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite existing example: {destination}")
        card.save(destination, format="PNG")
        cards.append(card)
        print(destination)
    destination = args.output / "overview.png"
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing example: {destination}")
    overview(cards).save(destination, format="PNG")
    print(destination)


if __name__ == "__main__":
    main()
