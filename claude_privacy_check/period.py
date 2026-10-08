"""The stretch of history a time-based view reports on.

Working time, the admin dashboard and the observer view all measure *when*.
Over the whole history those figures blur into one number that answers no
question anybody asks; over a calendar month they read like the timesheet or the
monthly report they are standing in for. So every one of those views carries a
period, and the last full month is where they open.

A span is a pair of local dates, both inclusive, or ``None`` for everything that
is on disk. Months are calendar months, cut at local midnight like the days in
the working-time reconstruction.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .i18n import t

# In the order the selection box lists them. Also the values of ``--period``.
CHOICES = ("current-month", "last-month", "last-3-months", "all")
DEFAULT = "last-month"


def _month_start(day, back=0):
    """First day of the month `back` months before the one `day` is in."""
    index = day.year * 12 + day.month - 1 - back
    return date(index // 12, index % 12 + 1, 1)


def bounds(choice, today=None):
    """(first, last) local dates for a choice, inclusive; None for all of it.

    The three months are the running one and the two before it, so the window
    reaches up to today like the current month does.
    """
    today = today or date.today()
    if choice == "current-month":
        return (_month_start(today), today)
    if choice == "last-month":
        return (_month_start(today, 1), _month_start(today) - timedelta(days=1))
    if choice == "last-3-months":
        return (_month_start(today, 2), today)
    return None


def contains(span, day):
    """Whether a local date falls inside the span."""
    return span is None or span[0] <= day <= span[1]


def predates(span, mtime):
    """Whether a file last written at `mtime` can hold nothing in the span.

    Transcripts are only ever appended to, so a file whose last write lies
    before the first day cannot carry a single line from inside the span --
    and need not be opened. That is what keeps the default month quick on a
    long history.
    """
    return span is not None and datetime.fromtimestamp(mtime).date() < span[0]


def serialise(span):
    """The span as it goes into a report: ISO dates, or None."""
    return None if span is None else [span[0].isoformat(), span[1].isoformat()]


def describe(choice, span):
    """``Period: Last month · 2026-09-01 to 2026-09-30`` for the terminal."""
    return t("period.line", name=t(f"period.{choice}"),
             range=range_text(serialise(span)))


def range_text(dates):
    """A report's ``period`` -- ISO dates or None -- in the reading language."""
    if dates is None:
        return t("period.range.all")
    first, last = dates
    return t("period.range", first=first, last=last)
