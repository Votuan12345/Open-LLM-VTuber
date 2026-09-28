"""Daily rhythm segmentation: splits a time span into day-part segments.

Day parts follow BOUNDARY_HOURS = (5, 11, 17, 22):
  [05:00, 11:00) MORNING
  [11:00, 17:00) AFTERNOON
  [17:00, 22:00) EVENING
  [22:00, 05:00) LATE_NIGHT (crosses midnight)
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import List, Tuple

BOUNDARY_HOURS = (5, 11, 17, 22)


class DayPart(str, Enum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    LATE_NIGHT = "late_night"


def day_part_at(t: datetime) -> DayPart:
    hour = t.hour
    if hour < BOUNDARY_HOURS[0] or hour >= BOUNDARY_HOURS[3]:
        return DayPart.LATE_NIGHT
    if hour < BOUNDARY_HOURS[1]:
        return DayPart.MORNING
    if hour < BOUNDARY_HOURS[2]:
        return DayPart.AFTERNOON
    return DayPart.EVENING


def _next_boundary(t: datetime) -> datetime:
    """The smallest boundary hour today strictly after `t`, else 05:00 tomorrow."""
    for hour in BOUNDARY_HOURS:
        candidate = t.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > t:
            return candidate
    tomorrow = t + timedelta(days=1)
    return tomorrow.replace(hour=BOUNDARY_HOURS[0], minute=0, second=0, microsecond=0)


def split_by_day_part(start: datetime, end: datetime) -> List[Tuple[DayPart, float]]:
    """Split [start, end) into (DayPart, seconds) segments in chronological order.

    Adjacent segments with the same DayPart are merged (e.g. a span crossing
    22:00 -> 05:00 stays a single LATE_NIGHT segment). `end <= start` -> [].
    """
    if end <= start:
        return []
    segments: List[Tuple[DayPart, float]] = []
    current = start
    while current < end:
        boundary = min(_next_boundary(current), end)
        seconds = (boundary - current).total_seconds()
        part = day_part_at(current)
        if segments and segments[-1][0] == part:
            segments[-1] = (part, segments[-1][1] + seconds)
        else:
            segments.append((part, seconds))
        current = boundary
    return segments
