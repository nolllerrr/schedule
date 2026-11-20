from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from schedule_bot.security import redact_secrets


type DateRange = tuple[date, date]


@dataclass(frozen=True, slots=True)
class PublicationCaption:
    ranges: tuple[DateRange, ...] = ()
    ambiguous: bool = False

    def conflicts_with(self, declared: DateRange) -> bool:
        return self.ambiguous or self.ranges not in ((), (declared,))


def format_date_range(dates: DateRange) -> str:
    start, end = dates
    return start.isoformat() if start == end else f"{start}–{end}"


@dataclass(frozen=True, slots=True)
class WorkbookDiagnostic:
    filename: str
    declared: DateRange | None
    caption: PublicationCaption
    sheets: tuple[tuple[date | None, str], ...] = ()

    def format(self, *, sheet_limit: int | None = None) -> str:
        basename = re.split(r"[/\\]", self.filename)[-1]
        basename = re.split(r"[?#]", basename, maxsplit=1)[0]
        filename = re.sub(r"[^\w.() -]", "_", redact_secrets(basename))[:100]
        lines = [f"Файл: {filename or '?'}"]
        if self.declared is not None:
            lines.append(f"Диапазон в имени: {format_date_range(self.declared)}")
        if self.caption.ranges:
            ranges = ", ".join(map(format_date_range, self.caption.ranges[:3]))
            if len(self.caption.ranges) > 3:
                ranges += ", …"
            lines.append(f"Даты в подписи: {ranges}")
        if self.caption.ambiguous:
            lines.append("Подпись ссылки неоднозначна")
        for index, (day, reason) in enumerate(self.sheets[:sheet_limit], start=1):
            lines.append(f"Лист {index} ({day or 'дата не распознана'}) — {reason}")
        if sheet_limit is not None and len(self.sheets) > sheet_limit:
            lines.append(
                f"Остальных листов в журнале: {len(self.sheets) - sheet_limit}"
            )
        return "\n".join(lines)


class ScheduleParseError(ValueError):
    """Raised when a workbook does not match the supported schedule layout."""

    def __init__(self, message: str, *, diagnostic: WorkbookDiagnostic | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic

    def __str__(self) -> str:
        message = super().__str__()
        return f"{message}\n{self.diagnostic.format()}" if self.diagnostic else message
