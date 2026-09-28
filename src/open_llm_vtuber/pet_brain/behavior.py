"""Deterministic behavior selector (spec §8.3-8.4, §8.9).

`BehaviorSelector.select` is the pure rule engine the BehaviorScheduler (a
later task) calls once per eligible client to choose between DO_NOTHING,
IDLE_EXPRESSION and PROACTIVE_SPEAK. It is pure with respect to
`SelectionInput.presence`: it reads presence fields but never mutates them,
and never touches the global `random` module (only the injected `rng`).
"""

import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Mapping, Optional

from ..config_manager.pet_brain import PetBrainConfig, ProactiveConfig
from .context import ContextSnapshot
from .lifecycle import LifecyclePhase
from .pet_brain import ActivityState
from .presence import ClientPresence
from .rhythm import DayPart, day_part_at

WANT_WEIGHTS = {"social_need": 0.5, "boredom": 0.3, "curiosity": 0.2}

CATEGORY_MULT = {
    "coding": 0.3,
    "unity": 0.3,
    "office": 0.5,
    "media": 0.5,
    "browser": 1.0,
    "unknown": 0.8,
    "gaming": 0.0,
}

DAYPART_MULT = {
    DayPart.MORNING: 1.0,
    DayPart.AFTERNOON: 1.0,
    DayPart.EVENING: 0.9,
    DayPart.LATE_NIGHT: 0.5,
}

CODING_PAUSE_S = 120
RETURNED_BONUS = 0.2

# First rule whose mood value is strictly greater than its threshold wins.
IDLE_EMOTION_RULES = (
    ("sleepiness", 0.6, "sleepy"),
    ("boredom", 0.6, "bored"),
    ("curiosity", 0.7, "curious"),
    ("happiness", 0.7, "joy"),
)

_FOCUS_CATEGORIES = frozenset({"coding", "unity"})
_HOURLY_WINDOW_S = 3600.0


class BehaviorKind(str, Enum):
    DO_NOTHING = "do_nothing"
    IDLE_EXPRESSION = "idle_expression"
    PROACTIVE_SPEAK = "proactive_speak"


class Trigger(str, Enum):
    TICK = "tick"
    REQUEST = "request"


@dataclass(frozen=True)
class Decision:
    kind: BehaviorKind
    reason: str
    willingness: Optional[float] = None
    emotion: Optional[str] = None


@dataclass(frozen=True)
class SelectionInput:
    trigger: Trigger
    now: float
    presence: ClientPresence
    activity: ActivityState
    lifecycle: LifecyclePhase
    mood: Mapping[str, float]
    context: ContextSnapshot
    category: Optional[str]
    config: PetBrainConfig
    emo_map: Mapping[str, Any]


def _count_within_window(
    ts: Deque[float], now: float, window_s: float = _HOURLY_WINDOW_S
) -> int:
    """Non-mutating count of timestamps within the trailing window."""

    return sum(1 for t in ts if (now - t) < window_s)


def _latest(*values: Optional[float]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return max(known) if known else None


def willingness(
    mood: Mapping[str, float],
    category: str,
    day_part: DayPart,
    returned_bonus: bool,
) -> float:
    want = (
        WANT_WEIGHTS["social_need"] * mood.get("social_need", 0.0)
        + WANT_WEIGHTS["boredom"] * mood.get("boredom", 0.0)
        + WANT_WEIGHTS["curiosity"] * mood.get("curiosity", 0.0)
    )
    sleepiness = mood.get("sleepiness", 0.0)
    energy = mood.get("energy", 0.0)
    category_mult = CATEGORY_MULT.get(category, CATEGORY_MULT["unknown"])
    daypart_mult = DAYPART_MULT.get(day_part, 1.0)

    w = (
        want
        * (1 - 0.5 * sleepiness)
        * (0.5 + 0.5 * energy)
        * category_mult
        * daypart_mult
    )
    if returned_bonus:
        w += RETURNED_BONUS
    return w


def choose_idle_emotion(
    mood: Mapping[str, float], emo_map: Mapping[str, Any]
) -> Optional[str]:
    preferred: Optional[str] = None
    for key, threshold, label in IDLE_EMOTION_RULES:
        if mood.get(key, 0.0) > threshold:
            preferred = label
            break

    for candidate in (preferred, "neutral"):
        if candidate is not None and candidate in emo_map:
            return candidate
    return None


def effective_min_interval_s(cfg: ProactiveConfig, ignored_count: int) -> float:
    return min(cfg.min_interval_min * (2**ignored_count), cfg.max_backoff_min) * 60


class BehaviorSelector:
    """Pure rule engine; `rng` is the only source of randomness it uses."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def select(self, inp: SelectionInput) -> Decision:
        veto = self._veto(inp)
        if veto is not None:
            return veto

        proactive_reason, proactive_willingness, speak_decision = self._proactive_stage(
            inp
        )
        if speak_decision is not None:
            return speak_decision

        if inp.trigger is Trigger.TICK:
            idle_decision = self._idle_stage(inp)
            if idle_decision is not None:
                return idle_decision

        return Decision(
            BehaviorKind.DO_NOTHING, proactive_reason, willingness=proactive_willingness
        )

    def _veto(self, inp: SelectionInput) -> Optional[Decision]:
        if inp.activity != ActivityState.IDLE:
            return Decision(BehaviorKind.DO_NOTHING, "conversation_lock")
        if inp.lifecycle in (LifecyclePhase.SLEEP, LifecyclePhase.AWAY):
            return Decision(BehaviorKind.DO_NOTHING, f"lifecycle_{inp.lifecycle.value}")
        if inp.context.fullscreen is True:
            return Decision(BehaviorKind.DO_NOTHING, "fullscreen")
        if inp.context.fullscreen is None:
            return Decision(BehaviorKind.DO_NOTHING, "context_unknown")
        return None

    def _proactive_stage(self, inp: SelectionInput):
        """Returns (reason, willingness_or_None, Decision_or_None).

        `Decision` is non-None only for the PROACTIVE_SPEAK outcome. Every
        other outcome (including `low_willingness`/`chance`, which still
        carry a willingness value) returns `None` for the third element so
        the caller can still attempt the idle-expression stage on a TICK,
        falling back to `(reason, willingness)` only if that also fails.
        """

        cfg = inp.config
        presence = inp.presence
        now = inp.now

        if not cfg.proactive.enabled:
            return "proactive_disabled", None, None

        idle = inp.context.user_idle_seconds
        category = inp.category
        if idle is None or category is None:
            return "context_unknown", None, None

        eff_interval = effective_min_interval_s(cfg.proactive, presence.ignored_count)
        if (
            presence.last_proactive is not None
            and (now - presence.last_proactive) < eff_interval
        ):
            return "cooldown", None, None

        last_activity = _latest(
            presence.last_conversation_end, presence.last_user_interaction
        )
        quiet_s = cfg.proactive.post_conversation_quiet_min * 60
        if last_activity is not None and (now - last_activity) < quiet_s:
            return "post_conversation_quiet", None, None

        count = _count_within_window(presence.proactive_timestamps, now)
        if count >= cfg.proactive.max_per_hour:
            return "hourly_cap", None, None

        if category in _FOCUS_CATEGORIES and idle < CODING_PAUSE_S:
            return "user_busy", None, None

        day_part = day_part_at(inp.context.taken_at_wall)
        w = willingness(inp.mood, category, day_part, presence.returned_bonus_pending)

        if w < cfg.proactive.threshold:
            return "low_willingness", w, None

        if self.rng.random() >= cfg.proactive.chance:
            return "chance", w, None

        return (
            "proactive_speak",
            w,
            Decision(BehaviorKind.PROACTIVE_SPEAK, "proactive_speak", willingness=w),
        )

    def _idle_stage(self, inp: SelectionInput) -> Optional[Decision]:
        cfg = inp.config.idle_expression
        presence = inp.presence
        now = inp.now

        if not cfg.enabled:
            return None

        if presence.last_idle_expression is not None:
            interval_s = cfg.min_interval_min * 60
            if (now - presence.last_idle_expression) < interval_s:
                return None

        count = _count_within_window(presence.idle_expression_timestamps, now)
        if count >= cfg.max_per_hour:
            return None

        emotion = choose_idle_emotion(inp.mood, inp.emo_map)
        if emotion is None:
            return Decision(BehaviorKind.DO_NOTHING, "no_expression_available")

        if self.rng.random() < cfg.chance:
            return Decision(
                BehaviorKind.IDLE_EXPRESSION, "idle_expression", emotion=emotion
            )

        return None
