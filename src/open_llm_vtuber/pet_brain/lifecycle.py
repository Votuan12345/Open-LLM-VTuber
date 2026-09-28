import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, FrozenSet, Optional

from loguru import logger

# Time-driven lifecycle thresholds (spec §7.2).
ACTIVE_TO_IDLE_S = 120
SLEEPY_AT = 0.7
SLEEP_AT = 0.85
SLEEP_USER_IDLE_S = 900


class LifecyclePhase(str, Enum):
    WAKE_UP = "wake_up"
    ACTIVE = "active"
    IDLE = "idle"
    SLEEPY = "sleepy"
    SLEEP = "sleep"
    AWAY = "away"


_P = LifecyclePhase

ALLOWED_TRANSITIONS: Dict[LifecyclePhase, FrozenSet[LifecyclePhase]] = {
    _P.WAKE_UP: frozenset({_P.ACTIVE, _P.IDLE, _P.SLEEPY}),
    _P.ACTIVE: frozenset({_P.IDLE, _P.SLEEPY, _P.AWAY}),
    _P.IDLE: frozenset({_P.ACTIVE, _P.SLEEPY, _P.SLEEP, _P.AWAY}),
    _P.SLEEPY: frozenset({_P.ACTIVE, _P.IDLE, _P.SLEEP, _P.AWAY}),
    _P.SLEEP: frozenset({_P.WAKE_UP}),
    _P.AWAY: frozenset({_P.WAKE_UP, _P.ACTIVE, _P.IDLE}),
}


@dataclass(frozen=True)
class LifecycleInputs:
    """Inputs the scheduler assembles once per tick for `Lifecycle.evaluate`."""

    sleepiness: float
    user_idle_seconds: Optional[float]
    quiet_seconds: float
    conversation_running: bool
    away_after_seconds: float
    user_returned: bool


class Lifecycle:
    """High-level lifecycle state machine. Time-based drivers arrive in Phase 2."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.phase = LifecyclePhase.WAKE_UP
        self.since = clock()

    def can_transition(self, target: LifecyclePhase) -> bool:
        return target in ALLOWED_TRANSITIONS[self.phase]

    def transition(self, target: LifecyclePhase, reason: str) -> bool:
        if target == self.phase:
            return True
        if not self.can_transition(target):
            logger.debug(
                f"[LifeCycle] Rejected {self.phase.value} -> {target.value} ({reason})"
            )
            return False
        logger.info(f"[LifeCycle] {self.phase.value} -> {target.value} ({reason})")
        self.phase = target
        self.since = self._clock()
        return True

    def ensure_active(self, reason: str) -> None:
        if self.phase in (LifecyclePhase.SLEEP, LifecyclePhase.AWAY):
            self.transition(LifecyclePhase.WAKE_UP, reason)
        self.transition(LifecyclePhase.ACTIVE, reason)

    def evaluate(self, inputs: LifecycleInputs) -> None:
        """Time-driven lifecycle transitions (spec §7.2). First matching rule wins."""

        if inputs.user_returned and self.phase == LifecyclePhase.AWAY:
            self.transition(LifecyclePhase.WAKE_UP, "user_returned")
            self.transition(LifecyclePhase.ACTIVE, "user_returned")
            return

        if (
            self.phase
            in (LifecyclePhase.ACTIVE, LifecyclePhase.IDLE, LifecyclePhase.SLEEPY)
            and inputs.user_idle_seconds is not None
            and inputs.user_idle_seconds >= inputs.away_after_seconds
        ):
            self.transition(LifecyclePhase.AWAY, "user_idle")
            return

        if (
            self.phase in (LifecyclePhase.ACTIVE, LifecyclePhase.WAKE_UP)
            and inputs.quiet_seconds >= ACTIVE_TO_IDLE_S
            and not inputs.conversation_running
        ):
            self.transition(LifecyclePhase.IDLE, "quiet")
            return

        if self.phase == LifecyclePhase.IDLE and inputs.sleepiness >= SLEEPY_AT:
            self.transition(LifecyclePhase.SLEEPY, "sleepiness")
            return

        if (
            self.phase == LifecyclePhase.SLEEPY
            and inputs.sleepiness >= SLEEP_AT
            and inputs.user_idle_seconds is not None
            and inputs.user_idle_seconds >= SLEEP_USER_IDLE_S
        ):
            self.transition(LifecyclePhase.SLEEP, "sleepiness_and_idle")
            return
