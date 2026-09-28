"""Context block appended to the proactive LLM prompt (spec §8.7).

No window title is ever read, logged, or included here. Unknown fields
(``None``) are omitted rather than guessed.
"""

from datetime import datetime
from typing import Mapping, Optional

from .behavior import Trigger
from .lifecycle import LifecyclePhase
from .rhythm import DayPart

TRAIT_LABELS = {
    "happiness": "cheerful",
    "energy": "energetic",
    "curiosity": "curious",
    "boredom": "bored",
    "social_need": "like chatting",
    "focus": "focused",
    "sleepiness": "sleepy",
}

TRAIT_THRESHOLD = 0.6
MAX_TRAITS = 3


def _round_minutes(seconds: float) -> int:
    return int(round(seconds / 60.0))


def _traits(mood: Mapping[str, float]) -> str:
    qualifying = [
        (key, value)
        for key, value in mood.items()
        if key in TRAIT_LABELS and value >= TRAIT_THRESHOLD
    ]
    qualifying.sort(key=lambda item: item[1], reverse=True)
    labels = [TRAIT_LABELS[key] for key, _ in qualifying[:MAX_TRAITS]]
    if not labels:
        return "calm"
    return ", ".join(labels)


def build_context_block(
    *,
    now_wall: datetime,
    day_part: DayPart,
    lifecycle: LifecyclePhase,
    mood: Mapping[str, float],
    category: Optional[str],
    process_name: Optional[str],
    user_idle_seconds: Optional[float],
    returned_after_s: Optional[float],
    trigger: Trigger,
) -> str:
    lines = [
        "[Context for this moment. Use it naturally; do not read it out.]",
        f"- Local time: {now_wall.strftime('%H:%M')} ({day_part.value})",
        f"- Your state: {lifecycle.value}; feeling {_traits(mood)}",
    ]

    if category is not None:
        activity = category
        if process_name:
            activity += f" ({process_name})"
        if user_idle_seconds is not None:
            activity += f", idle {_round_minutes(user_idle_seconds)} min"
        lines.append(f"- User activity: {activity}")

    if returned_after_s is not None:
        lines.append(
            f"- The user just came back after {_round_minutes(returned_after_s)} min away."
        )

    why = "you chose to" if trigger is Trigger.TICK else "the app asked you to"
    lines.append(f"- Why you are speaking: {why}")

    return "\n".join(lines)
