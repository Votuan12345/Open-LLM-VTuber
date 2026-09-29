"""Phase 3A desktop pet: message parsing, per-client pet presence, the pure
movement/idle-motion selector and interaction reactions (spec §5, §6).

The backend only decides *whether* and *what kind* of movement happens. The
Electron frontend picks the coordinates and enforces its own bounds. Nothing
here touches a websocket; the scheduler owns dispatch.
"""

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, Mapping, Optional, Tuple, Union

from ..config_manager.pet_brain import DesktopPetConfig
from .context import ContextSnapshot
from .lifecycle import LifecyclePhase

PROTOCOL_VERSION = 1

_HOURLY_WINDOW_S = 3600.0
_MAX_STR_LEN = 64

MODES = frozenset({"pet", "window"})
ANCHORS = frozenset({"home", "edge", "free"})
RESULTS = frozenset({"arrived", "cancelled", "rejected"})
INTERACTION_KINDS = frozenset({"click", "double_click", "drag_start", "drag_end"})

# The user counts as busy in these categories while recently active (spec §6.3 rule 3).
BUSY_CATEGORIES = frozenset({"coding", "unity", "office"})
BUSY_IDLE_BELOW_S = 60.0
BUSY_EDGE_MIN_GAP_S = 60.0

SLEEPY_AT = 0.7
TOO_LAZY_BELOW = 0.2
LONG_WANDER_ENERGY = 0.5
SLOW_WANDER_ENERGY_BELOW = 0.4

# (mood key, threshold, second key, second threshold, semantic motion name),
# checked in order; the first match wins (spec §6.3 rule 7).
IDLE_MOTION_RULES: Tuple[Tuple[str, float, Optional[str], float, str], ...] = (
    ("sleepiness", 0.6, None, 0.0, "yawn"),
    ("energy", 0.6, "boredom", 0.4, "stretch"),
    ("curiosity", 0.6, None, 0.0, "look_around"),
)


# ----------------------------------------------------------------------
# Inbound messages (spec §5.1)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PetHello:
    protocol: int
    mode: str
    movement_enabled: bool


@dataclass(frozen=True)
class PetStatus:
    mode: str
    movement_enabled: bool
    moving: bool
    anchor: Optional[str]
    command_id: Optional[str]
    result: Optional[str]
    reason: Optional[str]


@dataclass(frozen=True)
class PetInteraction:
    kind: str
    hit_area: Optional[str]


PetMessage = Union[PetHello, PetStatus, PetInteraction]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _opt_str(data: Mapping[str, Any], key: str, allowed=None) -> Tuple[bool, Any]:
    """(ok, value) for an optional short string field."""
    value = data.get(key)
    if value is None:
        return True, None
    if not isinstance(value, str) or len(value) > _MAX_STR_LEN:
        return False, None
    if allowed is not None and value not in allowed:
        return False, None
    return True, value


def parse_pet_message(data: Any) -> Optional[PetMessage]:
    """Validate one inbound pet message. Invalid input returns `None`; unknown fields are ignored."""

    if not isinstance(data, dict):
        return None
    msg_type = data.get("type")

    if msg_type == "pet-hello":
        protocol = data.get("protocol")
        mode = data.get("mode")
        enabled = data.get("movement_enabled")
        if not _is_int(protocol) or mode not in MODES or not isinstance(enabled, bool):
            return None
        return PetHello(protocol=protocol, mode=mode, movement_enabled=enabled)

    if msg_type == "pet-status":
        mode = data.get("mode")
        enabled = data.get("movement_enabled")
        moving = data.get("moving")
        if (
            mode not in MODES
            or not isinstance(enabled, bool)
            or not isinstance(moving, bool)
        ):
            return None
        fields = {}
        for key, allowed in (
            ("anchor", ANCHORS),
            ("command_id", None),
            ("result", RESULTS),
            ("reason", None),
        ):
            ok, value = _opt_str(data, key, allowed)
            if not ok:
                return None
            fields[key] = value
        return PetStatus(mode=mode, movement_enabled=enabled, moving=moving, **fields)

    if msg_type == "pet-interaction":
        kind = data.get("kind")
        if kind not in INTERACTION_KINDS:
            return None
        ok, hit_area = _opt_str(data, "hit_area")
        if not ok:
            return None
        return PetInteraction(kind=kind, hit_area=hit_area)

    return None


# ----------------------------------------------------------------------
# Per-client pet presence (spec §6.1)
# ----------------------------------------------------------------------


@dataclass
class PetPresence:
    """What the frontend reported plus pet-lane cooldowns. Owned by the scheduler."""

    protocol: Optional[int] = None
    mode: str = "window"
    movement_enabled: bool = False
    moving: bool = False
    anchor: str = "free"
    pending_command_id: Optional[str] = None
    pending_since: Optional[float] = None
    last_move: Optional[float] = None
    move_timestamps: Deque[float] = field(default_factory=deque)
    last_contextual: Optional[float] = None
    paused_until: Optional[float] = None
    last_reaction: Optional[float] = None
    click_times: Deque[float] = field(default_factory=deque)
    last_idle_motion: Optional[float] = None
    idle_motion_timestamps: Deque[float] = field(default_factory=deque)
    last_category: Optional[str] = None
    category_changed: bool = False
    returned_pending: bool = False
    message_times: Deque[float] = field(default_factory=deque)


# ----------------------------------------------------------------------
# Selector (spec §6.3)
# ----------------------------------------------------------------------


class PetKind(str, Enum):
    STAY = "stay"
    WANDER = "wander"
    GO_EDGE = "go_edge"
    GO_HOME = "go_home"
    APPROACH_WINDOW = "approach_window"
    IDLE_MOTION = "idle_motion"


@dataclass(frozen=True)
class PetDecision:
    kind: PetKind
    reason: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PetSelectionInput:
    now: float
    cfg: DesktopPetConfig
    presence: PetPresence
    mood: Mapping[str, float]
    lifecycle: LifecyclePhase
    context: ContextSnapshot
    category: Optional[str]
    conversation_active: bool
    last_conversation_end: Optional[float]
    motion_map: Mapping[str, Mapping[str, Any]]


def _count_within_window(ts: Deque[float], now: float) -> int:
    return sum(1 for t in ts if (now - t) < _HOURLY_WINDOW_S)


def _since(now: float, then: Optional[float]) -> float:
    return float("inf") if then is None else now - then


def _stay(reason: str) -> PetDecision:
    return PetDecision(PetKind.STAY, reason)


class PetSelector:
    """Pure rule engine; `rng.random()` is its only randomness. Never mutates presence."""

    def __init__(self, rng: Any):
        self.rng = rng

    def select(self, inp: PetSelectionInput) -> PetDecision:
        veto = self._veto(inp)
        if veto is not None:
            return veto

        p = inp.presence
        move_cfg = inp.cfg.movement
        under_cap = (
            _count_within_window(p.move_timestamps, inp.now) < move_cfg.max_per_hour
        )
        since_move = _since(inp.now, p.last_move)

        # 1. User returned from away.
        if p.returned_pending and p.anchor != "home":
            return PetDecision(PetKind.GO_HOME, "user_returned")

        # 2. Contextual approach (event-driven).
        contextual = self._contextual(inp, under_cap)
        if contextual is not None:
            return contextual

        # 3. Busy user: peek then leave; never wander while the user works.
        idle = inp.context.user_idle_seconds
        if (
            inp.category in BUSY_CATEGORIES
            and idle is not None
            and idle < BUSY_IDLE_BELOW_S
        ):
            if p.anchor != "edge" and since_move >= BUSY_EDGE_MIN_GAP_S and under_cap:
                return PetDecision(PetKind.GO_EDGE, "user_busy")
            return _stay("user_busy")

        # 4-6. Frequency-gated movement.
        if since_move >= move_cfg.min_interval_min * 60 and under_cap:
            moved = self._gated_movement(inp)
            if moved is not None:
                return moved

        # 7-8. Idle motion or nothing.
        return self._idle_motion(inp)

    def _veto(self, inp: PetSelectionInput) -> Optional[PetDecision]:
        p = inp.presence
        cfg = inp.cfg
        if (
            p.protocol != PROTOCOL_VERSION
            or p.mode != "pet"
            or not p.movement_enabled
            or not cfg.enabled
            or not cfg.movement.enabled
        ):
            return _stay("disabled")
        if p.moving:
            return _stay("moving")
        if inp.conversation_active:
            return _stay("conversation")
        quiet_s = cfg.movement.post_conversation_quiet_min * 60
        if _since(inp.now, inp.last_conversation_end) < quiet_s:
            return _stay("post_conversation")
        if p.paused_until is not None and p.paused_until > inp.now:
            return _stay("paused")
        if inp.lifecycle in (LifecyclePhase.SLEEP, LifecyclePhase.AWAY):
            return _stay(f"lifecycle_{inp.lifecycle.value}")
        if inp.context.fullscreen is True:
            return _stay("fullscreen")
        if inp.context.user_idle_seconds is None or inp.context.fullscreen is None:
            return _stay("context_unknown")
        return None

    def _contextual(
        self, inp: PetSelectionInput, under_cap: bool
    ) -> Optional[PetDecision]:
        c = inp.cfg.contextual
        p = inp.presence
        if (
            c.enabled
            and p.category_changed
            and inp.category in c.categories
            and inp.mood.get("curiosity", 0.0) >= c.min_curiosity
            and inp.context.foreground_rect is not None
            and _since(inp.now, p.last_contextual) >= c.cooldown_min * 60
            and under_cap
        ):
            return PetDecision(
                PetKind.APPROACH_WINDOW,
                f"curious_about_{inp.category}",
                {"rect": inp.context.foreground_rect},
            )
        return None

    def _gated_movement(self, inp: PetSelectionInput) -> Optional[PetDecision]:
        mood = inp.mood
        move_cfg = inp.cfg.movement
        sleepiness = mood.get("sleepiness", 0.0)
        energy = mood.get("energy", 0.0)

        # 5. Sleepy: retreat to an edge.
        if inp.lifecycle is LifecyclePhase.SLEEPY or sleepiness > SLEEPY_AT:
            if inp.presence.anchor != "edge" and self.rng.random() < move_cfg.chance:
                return PetDecision(PetKind.GO_EDGE, "sleepy")
            return None

        # 6. Wander ("wants to but is too lazy" below the energy floor).
        if energy < TOO_LAZY_BELOW:
            return None
        score = (
            0.5 * mood.get("boredom", 0.0)
            + 0.3 * mood.get("curiosity", 0.0)
            + 0.2 * energy
            - 0.4 * sleepiness
        )
        if score >= move_cfg.wander_threshold and self.rng.random() < move_cfg.chance:
            return PetDecision(
                PetKind.WANDER,
                "wander",
                {
                    "distance": "long" if energy >= LONG_WANDER_ENERGY else "short",
                    "speed": "slow" if energy < SLOW_WANDER_ENERGY_BELOW else "normal",
                },
            )
        return None

    def _idle_motion(self, inp: PetSelectionInput) -> PetDecision:
        cfg = inp.cfg.idle_motion
        p = inp.presence
        if not cfg.enabled:
            return _stay("nothing_to_do")
        if _since(inp.now, p.last_idle_motion) < cfg.min_interval_min * 60:
            return _stay("nothing_to_do")
        if _count_within_window(p.idle_motion_timestamps, inp.now) >= cfg.max_per_hour:
            return _stay("nothing_to_do")

        name = None
        for key, threshold, key2, threshold2, label in IDLE_MOTION_RULES:
            if inp.mood.get(key, 0.0) > threshold and (
                key2 is None or inp.mood.get(key2, 0.0) > threshold2
            ):
                name = label
                break
        entry = inp.motion_map.get(name) if name is not None else None
        if entry is None or self.rng.random() >= cfg.chance:
            return _stay("nothing_to_do")
        return PetDecision(
            PetKind.IDLE_MOTION,
            f"idle_motion_{name}",
            {"group": entry["group"], "index": entry["index"], "name": name},
        )


# ----------------------------------------------------------------------
# Reactions and outbound commands (spec §5.2, §6.5)
# ----------------------------------------------------------------------


def choose_reaction(kind: str, mood: Mapping[str, float], spam: bool) -> Optional[str]:
    """Reaction key for an interaction, or `None` when it has no reaction."""

    happiness = mood.get("happiness", 0.0)
    if kind == "click":
        if spam:
            return "spam"
        return "click_happy" if happiness >= 0.5 else "click_neutral"
    if kind == "double_click":
        return "double_click"
    if kind == "drag_end":
        if happiness >= 0.5 and mood.get("energy", 0.0) >= 0.4:
            return "drag_playful"
        return "drag_annoyed"
    return None


def movement_command(decision: PetDecision) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Map a decision to a `pet-command` (command, params); `STAY` maps to `None`."""

    kind = decision.kind
    params = decision.params
    if kind is PetKind.WANDER:
        return "wander", {"distance": params["distance"], "speed": params["speed"]}
    if kind is PetKind.GO_EDGE:
        return "go_edge", {"side": "nearest"}
    if kind is PetKind.GO_HOME:
        return "go_home", {}
    if kind is PetKind.APPROACH_WINDOW:
        left, top, right, bottom = params["rect"]
        return "approach_rect", {
            "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top}
        }
    if kind is PetKind.IDLE_MOTION:
        # "name" is for logs only; the wire carries group/index (spec §5.2).
        return "play_motion", {"group": params["group"], "index": params["index"]}
    return None
