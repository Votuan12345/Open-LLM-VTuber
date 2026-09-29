import time
from dataclasses import replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ..agent.output_types import Actions


class EmotionSource(str, Enum):
    LLM_TAG = "llm_tag"
    FALLBACK_INVALID_EXPRESSION = "fallback_invalid_expression"
    EXPRESSION_ONLY = "expression_only"
    IDLE_BEHAVIOR = "idle_behavior"
    INTERACTION = "interaction"
    REACTION_RESET = "reaction_reset"


class EmotionManager:
    """Single gate in front of the existing Live2D expression pipeline.

    It does not parse text. Tags are parsed once by Live2dModel.parse_emotions
    (called from actions_extractor) and arrive as `Actions.emotion`, which
    carries both the original tag name and its expression index.

    Two separate jobs:
    - Filter: every outgoing `Actions.expressions` value must exist in the
      model's emotionMap. The payload shape never changes.
    - State: `current_emotion` is the original tag name ("smirk" vs "joy" even
      when both map to the same index), never guessed back from an index.

    Rules, applied per sentence:
    1. Valid expressions with parsed tags -> current_emotion = name of the last
       tag whose expression is the last one sent.
    2. Unknown word tags (e.g. "[happy]") -> logged and ignored; no expression
       change, no fallback.
    3. Expression index not in emotionMap -> dropped; if none remain, the
       fallback emotion's expression is sent instead.
    4. Valid expression without tag metadata -> expression is sent,
       current_emotion becomes None (name not inferred).
    5. No tags and no expressions -> nothing changes.

    TODO(priority): with several tags in one sentence the last valid one wins;
    primary/secondary emotion handling is not designed yet.
    """

    def __init__(
        self,
        fallback_emotion: str = "neutral",
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self.fallback_emotion = fallback_emotion.lower()
        self.current_expression: Any = None
        self.current_emotion: Optional[str] = None
        self.current_emotion_source: Optional[EmotionSource] = None
        self.updated_at: Optional[float] = None
        # Incremented on every recorded change; lets a caller tell whether
        # anything else changed the expression since it last looked.
        self.version = 0

    def gate(self, actions: Optional[Actions], live2d_model: Any) -> Optional[Actions]:
        emo_map: Optional[Dict[str, Any]] = getattr(live2d_model, "emo_map", None)
        if actions is None or not emo_map:
            return actions

        if actions.emotion and actions.emotion.unknown:
            logger.warning(
                f"[Emotion] Ignoring unknown emotion tags: {list(actions.emotion.unknown)}"
            )

        if not actions.expressions:
            return actions

        accepted, rejected = self._filter(actions.expressions, emo_map)

        if rejected:
            logger.warning(f"[Emotion] Rejected invalid expressions: {rejected}")
            if not accepted:
                return self._fallback(actions, emo_map)
            self._record_from(actions, accepted[-1])
            return replace(actions, expressions=accepted)

        self._record_from(actions, accepted[-1])
        return actions

    def apply_idle(self, name: str, live2d_model: Any) -> Optional[Actions]:
        """Idle-behavior expression (spec §8.9): no LLM, no speech.

        Missing keys never raise; they return `None` so nothing is sent.
        The returned `Actions` carries no `emotion` metadata.
        """
        return self._apply_named(name, live2d_model, EmotionSource.IDLE_BEHAVIOR)

    def apply_reaction(self, name: str, live2d_model: Any) -> Optional[Actions]:
        """Pet interaction reaction (Phase 3 spec §6.5): same rules as `apply_idle`."""
        return self._apply_named(name, live2d_model, EmotionSource.INTERACTION)

    def apply_reaction_reset(self, name: str, live2d_model: Any) -> Optional[Actions]:
        """Return from an interaction reaction to `name` (Phase 3 review/E2E fix D)."""
        return self._apply_named(name, live2d_model, EmotionSource.REACTION_RESET)

    def _apply_named(
        self, name: str, live2d_model: Any, source: EmotionSource
    ) -> Optional[Actions]:
        emo_map: Optional[Dict[str, Any]] = getattr(live2d_model, "emo_map", None)
        if not emo_map or name not in emo_map:
            logger.debug(
                f"[Emotion] {source.value} expression '{name}' not in emotionMap"
            )
            return None
        expression = emo_map[name]
        self._record(name, expression, source)
        return Actions(expressions=[expression])

    @staticmethod
    def _filter(expressions: List[Any], emo_map: Dict[str, Any]):
        accepted: List[Any] = []
        rejected: List[Any] = []
        valid_values = set(emo_map.values())
        for expression in expressions:
            if isinstance(expression, (int, str)) and expression in valid_values:
                accepted.append(expression)
            else:
                rejected.append(expression)
        return accepted, rejected

    def _record_from(self, actions: Actions, expression: Any) -> None:
        tags = actions.emotion.tags if actions.emotion else ()
        name = next(
            (t.name for t in reversed(tags) if t.expression == expression), None
        )
        if name is None:
            self._record(None, expression, EmotionSource.EXPRESSION_ONLY)
        else:
            self._record(name, expression, EmotionSource.LLM_TAG)

    def _fallback(self, actions: Actions, emo_map: Dict[str, Any]) -> Actions:
        fallback = emo_map.get(self.fallback_emotion)
        if fallback is None:
            logger.warning(
                f"[Emotion] Fallback '{self.fallback_emotion}' not in emotionMap; sending no expression"
            )
            return replace(actions, expressions=None)
        logger.info(f"[Emotion] Falling back to '{self.fallback_emotion}'")
        self._record(
            self.fallback_emotion, fallback, EmotionSource.FALLBACK_INVALID_EXPRESSION
        )
        return replace(actions, expressions=[fallback])

    def _record(
        self, name: Optional[str], expression: Any, source: EmotionSource
    ) -> None:
        if name != self.current_emotion or expression != self.current_expression:
            logger.info(
                f"[Emotion] {self.current_emotion} -> {name} (expression={expression}, source={source.value})"
            )
        self.current_emotion = name
        self.current_expression = expression
        self.current_emotion_source = source
        self.updated_at = self._clock()
        self.version += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "current_emotion": self.current_emotion,
            "current_expression": self.current_expression,
            "current_emotion_source": self.current_emotion_source.value
            if self.current_emotion_source
            else None,
        }
