from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class LessonSegment:
    """One linked assignment within a shared lesson slot."""

    subject: str
    teacher: str | None = None
    room: str | None = None
    subgroup: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


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
    segments: tuple[LessonSegment, ...] = ()

    def effective_segments(self) -> tuple[LessonSegment, ...]:
        return tuple(sorted(self.segments, key=lambda part: (
            part.subgroup or "", part.subject, part.teacher or "", part.room or "",
        ))) if self.segments else (
            LessonSegment(self.subject, self.teacher, self.room, self.subgroup),
        )

    def with_segments(self, segments: tuple[LessonSegment, ...]) -> Lesson:
        """Project a slot without breaking the legacy display fields."""
        segments = tuple(sorted(segments, key=lambda part: (
            part.subgroup or "", part.subject, part.teacher or "", part.room or "",
        )))
        def joined(field_name: str) -> str | None:
            values = tuple(dict.fromkeys(
                value for part in segments
                if (value := getattr(part, field_name))
            ))
            return " / ".join(values) or None

        subgroups = tuple(dict.fromkeys(
            part.subgroup for part in segments if part.subgroup
        ))
        return replace(
            self,
            subject=joined("subject") or "",
            teacher=joined("teacher"),
            room=joined("room"),
            subgroup=",".join(subgroups) or None,
            segments=segments,
        )

    def for_teacher(self, target: str) -> Lesson | None:
        needle = target.casefold()
        matching = tuple(
            segment for segment in self.effective_segments()
            if segment.teacher and needle in segment.teacher.casefold()
        )
        if not matching:
            return None
        return self.with_segments(matching) if self.segments else self

    def comparable_data(self) -> tuple[Any, ...]:
        return (
            self.lesson_label,
            self.lesson_number,
            self.start_time,
            self.end_time,
            self.effective_segments(),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["lesson_date"] = self.lesson_date.isoformat()
        if self.segments:
            result["segments"] = [segment.to_dict() for segment in self.segments]
        else:
            result.pop("segments")
        return result


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    source_filename: str
    start_date: date
    end_date: date
    dates: tuple[date, ...]
    lessons: tuple[Lesson, ...]
    warnings: tuple[str, ...] = ()
    date_groups: dict[date, tuple[str, ...]] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()
    confirmed_empty_dates: tuple[date, ...] = ()

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(sorted({lesson.group_name for lesson in self.lessons}))


@dataclass(frozen=True, slots=True)
class ScheduleChange:
    kind: Literal["added", "removed", "modified", "moved"]
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
    parsed: ParsedSchedule | None
    new_dates: tuple[date, ...] = ()
    source_filename: str = ""
    stored_lesson_count: int = 0

    @property
    def filename(self) -> str:
        return self.parsed.source_filename if self.parsed else self.source_filename

    @property
    def lesson_count(self) -> int:
        return len(self.parsed.lessons) if self.parsed else self.stored_lesson_count

