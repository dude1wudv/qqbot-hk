"""Deterministic Beijing-time duty roster rotation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


BEIJING_TIME = ZoneInfo("Asia/Shanghai")
START_DATE = date(2026, 9, 13)
MEMBERS = ("孙", "常", "黄", "张")
DUTIES = ("轮休", "洗手台", "拖地", "厕所")


@dataclass(frozen=True)
class DutyRosterWeek:
    week_start: date
    week_end: date
    assignments: tuple[tuple[str, str], ...]
    preview: bool = False


def duty_roster_for(now: datetime | None = None) -> DutyRosterWeek:
    """Return the roster active at ``now``, evaluated in Beijing time."""

    if now is None:
        local_date = datetime.now(BEIJING_TIME).date()
    else:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local_date = now.astimezone(BEIJING_TIME).date()

    preview = local_date < START_DATE
    week_index = 0 if preview else (local_date - START_DATE).days // 7
    week_start = START_DATE + timedelta(days=week_index * 7)
    assignments = tuple(
        (member, DUTIES[(index + week_index) % len(DUTIES)])
        for index, member in enumerate(MEMBERS)
    )
    return DutyRosterWeek(
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        assignments=assignments,
        preview=preview,
    )


def _date_range_text(week_start: date, week_end: date) -> str:
    if week_start.year == week_end.year:
        return (
            f"{week_start.year}年{week_start.month}月{week_start.day}日"
            f"—{week_end.month}月{week_end.day}日"
        )
    return (
        f"{week_start.year}年{week_start.month}月{week_start.day}日"
        f"—{week_end.year}年{week_end.month}月{week_end.day}日"
    )


def duty_roster_text(now: datetime | None = None) -> str:
    roster = duty_roster_for(now)
    title = "值日表预告" if roster.preview else "本周值日表"
    lines = [f"【{title}｜{_date_range_text(roster.week_start, roster.week_end)}】"]
    if roster.preview:
        lines.append("将于北京时间 2026年9月13日（周日）开始")
    lines.extend(f"{member}：{duty}" for member, duty in roster.assignments)
    return "\n".join(lines)


__all__ = [
    "BEIJING_TIME",
    "DUTIES",
    "MEMBERS",
    "START_DATE",
    "DutyRosterWeek",
    "duty_roster_for",
    "duty_roster_text",
]
