from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Lesson:
    lesson_date: date
    group_name: str
    position: int
    lesson_label: str
    lesson_number: int | None
    start_time: str | None
    end_time: str | None
    subject: str
    teacher: str | None = None
    room: str | None = None
    subgroup: str | None = None
    raw_data: dict[str, str] = field(default_factory=dict)

    def comparable_data(self) -> tuple[Any, ...]:
        return (
            self.lesson_label,
            self.lesson_number,
            self.start_time,
            self.end_time,
            self.subject,
            self.teacher,
            self.room,
            self.subgroup,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["lesson_date"] = self.lesson_date.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    source_filename: str
    start_date: date
    end_date: date
    dates: tuple[date, ...]
    lessons: tuple[Lesson, ...]
    warnings: tuple[str, ...] = ()

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(sorted({lesson.group_name for lesson in self.lessons}))


@dataclass(frozen=True, slots=True)
class ScheduleChange:
    kind: Literal["added", "removed", "modified"]
    before: Lesson | None
    after: Lesson | None

    @property
    def lesson_date(self) -> date:
        lesson = self.after or self.before
        if lesson is None:  # pragma: no cover - protected by construction
            raise RuntimeError("A schedule change must contain a lesson")
        return lesson.lesson_date

    @property
    def group_name(self) -> str:
        lesson = self.after or self.before
        if lesson is None:  # pragma: no cover - protected by construction
            raise RuntimeError("A schedule change must contain a lesson")
        return lesson.group_name


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_id: int | None
    skipped: bool
    changes: tuple[ScheduleChange, ...]
    parsed: ParsedSchedule

