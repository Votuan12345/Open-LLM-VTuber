import time
from enum import Enum
from typing import Any, Callable, Dict

from loguru import logger

from ..config_manager.pet_brain import PetBrainConfig
from .emotion_manager import EmotionManager
from .events import BrainEvent
from .lifecycle import Lifecycle
from .mood import Mood
from .permission import ConfirmationProvider, PermissionGuard


class ActivityState(str, Enum):
    """What Mili is doing right now. How she feels lives in Mood, not here."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    TALKING = "talking"


EVENT_ACTIVITY: Dict[BrainEvent, ActivityState] = {
    BrainEvent.USER_INPUT: ActivityState.THINKING,
    BrainEvent.PROACTIVE_TRIGGER: ActivityState.THINKING,
    BrainEvent.RESPONSE_START: ActivityState.TALKING,
    BrainEvent.RESPONSE_END: ActivityState.IDLE,
    BrainEvent.INTERRUPTED: ActivityState.IDLE,
    BrainEvent.ERROR: ActivityState.IDLE,
}


class PetBrain:
    """Deterministic companion core: owns activity state, mood, lifecycle,
    the emotion gate and the permission guard.

    It keeps no reference to a websocket or session, so it can later be moved
    from ServiceContext ownership to a desktop-global instance unchanged.

    TODO(ownership): one instance is currently shared by every session cloned
    from the default context. Later split into a desktop-global CompanionBrain
    (mood, lifecycle, emotion, permission policy) and a per-session
    SessionPresence (activity), since concurrent sessions overwrite `activity`.
    Group conversation also needs per-character emotion state instead of this
    single EmotionManager.
    """

    def __init__(
        self,
        config: PetBrainConfig,
        confirmation: ConfirmationProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._clock = clock
        self.activity = ActivityState.IDLE
        self.activity_since = clock()
        self.mood = Mood(clock=clock)
        self.lifecycle = Lifecycle(clock=clock)
        self.emotion = EmotionManager(
            fallback_emotion=config.emotion.fallback_emotion, clock=clock
        )
        self.permission = PermissionGuard(config.permission, confirmation)
        logger.info("[PetBrain] Initialized")

    def update_config(self, config: PetBrainConfig) -> None:
        """Swap in a new config in place, keeping mood/lifecycle/activity state."""
        self.config = config

    def notify(self, event: BrainEvent, **details: Any) -> None:
        if event in (BrainEvent.USER_INPUT, BrainEvent.PROACTIVE_TRIGGER):
            self.lifecycle.ensure_active(event.value)

        target = EVENT_ACTIVITY.get(event)
        if target is not None and target != self.activity:
            logger.info(
                f"[PetBrain] {self.activity.value} -> {target.value} ({event.value})"
            )
            self.activity = target
            self.activity_since = self._clock()

        self.mood.apply_event(event)
        if details:
            logger.debug(f"[PetBrain] {event.value} details={details}")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "activity": self.activity.value,
            "lifecycle": self.lifecycle.phase.value,
            "mood": self.mood.snapshot(),
            **self.emotion.snapshot(),
        }
