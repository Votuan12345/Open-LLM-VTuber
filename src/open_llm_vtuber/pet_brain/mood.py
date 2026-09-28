import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Callable, Dict, Mapping, Optional

from loguru import logger

from .events import BrainEvent
from .rhythm import DayPart, split_by_day_part


@dataclass
class MoodState:
    """All values are in [0, 1]. Several can be high at once; conflicts are intended."""

    happiness: float = 0.6
    energy: float = 0.7
    curiosity: float = 0.5
    boredom: float = 0.2
    social_need: float = 0.4
    focus: float = 0.3
    sleepiness: float = 0.2


MOOD_KEYS = tuple(asdict(MoodState()).keys())

# Linear change per hour with no interaction.
TREND_PER_HOUR: Dict[str, float] = {
    "boredom": 0.15,
    "social_need": 0.10,
    "energy": -0.03,
    "sleepiness": 0.02,
}

# Fraction of the gap to baseline closed per hour.
RELAX_PER_HOUR: Dict[str, float] = {
    "happiness": 0.2,
    "curiosity": 0.2,
    "focus": 0.3,
}

EVENT_EFFECTS: Dict[BrainEvent, Dict[str, float]] = {
    BrainEvent.USER_INPUT: {
        "social_need": -0.15,
        "boredom": -0.20,
        "happiness": 0.03,
        "curiosity": 0.02,
    },
    BrainEvent.RESPONSE_END: {"energy": -0.01},
    BrainEvent.INTERRUPTED: {"happiness": -0.02},
    BrainEvent.USER_RETURNED: {"happiness": 0.05, "curiosity": 0.05},
    BrainEvent.PROACTIVE_SPOKEN: {"social_need": -0.10},
    BrainEvent.PROACTIVE_IGNORED: {"happiness": -0.03},
}

# Additional per-hour trend applied on top of TREND_PER_HOUR, only for the
# listed DayPart while a segment falls within it.
RHYTHM_TREND_PER_HOUR: Dict[DayPart, Dict[str, float]] = {
    DayPart.MORNING: {"energy": 0.05},
    DayPart.LATE_NIGHT: {"sleepiness": 0.15, "energy": -0.05},
}

# Per-hour trend applied to `focus` instead of relaxing it toward baseline,
# while `focus_context_active` is True.
FOCUS_TREND_PER_HOUR = 0.2

# Max allowed mismatch between elapsed monotonic time and elapsed wall-clock
# time before an interval is treated as a wall-clock jump (rhythm skipped).
CLOCK_JUMP_TOLERANCE_S = 120.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class Mood:
    """Mood held in code. Time drift is applied lazily on access, so no background loop is needed."""

    def __init__(
        self,
        initial: MoodState | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = datetime.now,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self._baseline = MoodState()
        self._state = initial or MoodState()
        self.focus_context_active: bool = False
        self._last_mono = clock()
        self._last_wall = wall_clock()

    def advance(self) -> None:
        now_mono = self._clock()
        now_wall = self._wall_clock()
        elapsed = now_mono - self._last_mono
        if elapsed <= 0:
            self._last_mono, self._last_wall = now_mono, now_wall
            return

        wall_elapsed = (now_wall - self._last_wall).total_seconds()
        if abs(wall_elapsed - elapsed) <= CLOCK_JUMP_TOLERANCE_S:
            segments = split_by_day_part(
                self._last_wall, self._last_wall + timedelta(seconds=elapsed)
            )
        else:
            logger.info(
                f"[Mood] wall-clock jump detected (Δ={wall_elapsed - elapsed:.1f}s); "
                "rhythm trend skipped for this interval"
            )
            segments = [(None, elapsed)]

        for day_part, seconds in segments:
            self._apply_segment(seconds, day_part)
        self._last_mono, self._last_wall = now_mono, now_wall

    def _apply_segment(self, seconds: float, day_part: Optional[DayPart]) -> None:
        hours = seconds / 3600.0
        if hours <= 0:
            return
        for key, rate in TREND_PER_HOUR.items():
            setattr(self._state, key, _clamp(getattr(self._state, key) + rate * hours))
        for key, rate in RELAX_PER_HOUR.items():
            if key == "focus" and self.focus_context_active:
                continue
            current = getattr(self._state, key)
            target = getattr(self._baseline, key)
            step = min(1.0, rate * hours)
            setattr(self._state, key, _clamp(current + (target - current) * step))
        if day_part is not None:
            for key, rate in RHYTHM_TREND_PER_HOUR.get(day_part, {}).items():
                setattr(
                    self._state, key, _clamp(getattr(self._state, key) + rate * hours)
                )
        if self.focus_context_active:
            self._state.focus = _clamp(self._state.focus + FOCUS_TREND_PER_HOUR * hours)

    def apply(self, effects: Mapping[str, float], reason: str) -> None:
        self.advance()
        for key, delta in effects.items():
            if key not in MOOD_KEYS:
                logger.warning(f"[Mood] Ignoring unknown mood key '{key}' ({reason})")
                continue
            setattr(self._state, key, _clamp(getattr(self._state, key) + delta))
        logger.debug(f"[Mood] {reason}: {self.format()}")

    def apply_event(self, event: BrainEvent) -> None:
        effects = EVENT_EFFECTS.get(event)
        if effects:
            self.apply(effects, reason=event.value)

    def snapshot(self) -> Dict[str, float]:
        self.advance()
        return {k: round(v, 3) for k, v in asdict(self._state).items()}

    def format(self) -> str:
        return " ".join(f"{k}={v:.2f}" for k, v in asdict(self._state).items())
