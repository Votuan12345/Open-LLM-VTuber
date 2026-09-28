import time
from dataclasses import dataclass, asdict
from typing import Callable, Dict, Mapping

from loguru import logger

from .events import BrainEvent


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
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class Mood:
    """Mood held in code. Time drift is applied lazily on access, so no background loop is needed."""

    def __init__(
        self,
        initial: MoodState | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._baseline = MoodState()
        self._state = initial or MoodState()
        self._last_update = clock()

    def advance(self) -> None:
        now = self._clock()
        hours = (now - self._last_update) / 3600.0
        self._last_update = now
        if hours <= 0:
            return
        for key, rate in TREND_PER_HOUR.items():
            setattr(self._state, key, _clamp(getattr(self._state, key) + rate * hours))
        for key, rate in RELAX_PER_HOUR.items():
            current = getattr(self._state, key)
            target = getattr(self._baseline, key)
            step = min(1.0, rate * hours)
            setattr(self._state, key, _clamp(current + (target - current) * step))

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
