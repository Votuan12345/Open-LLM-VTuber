"""Desktop-global behavior scheduler (spec §4, §7, §9).

One `BehaviorScheduler` runs a single background asyncio task while at least
one client is connected. Each tick it samples desktop context once, ticks
every distinct `PetBrain` referenced by the handled clients once, detects
ignored proactive turns and then hands over to the decision/commit path.

Not exported from `pet_brain/__init__.py` to avoid an import cycle with the
websocket handler that constructs it.
"""

import asyncio
import functools
import json
import random
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from ..utils.stream_audio import prepare_audio_payload
from .behavior import (
    BehaviorKind,
    BehaviorSelector,
    Decision,
    SelectionInput,
    Trigger,
    effective_min_interval_s,
)
from .context import (
    ContextSensor,
    ContextSnapshot,
    classify_process,
    make_default_sensor,
)
from .desktop_pet import (
    PetDecision,
    PetHello,
    PetInteraction,
    PetKind,
    PetPresence,
    PetSelectionInput,
    PetSelector,
    PetStatus,
    choose_reaction,
    movement_command,
    parse_pet_message,
)
from .eligibility import ClientEligibility
from .events import BrainEvent
from .lifecycle import LifecyclePhase
from .pet_brain import ActivityState, BrainTickInputs, PetBrain
from .presence import ClientPresence, SchedulerState, prune_window
from .proactive_prompt import build_context_block
from .rhythm import day_part_at

# `user_returned` fires when the current known idle drops below this (spec §7.3).
RETURNED_IDLE_BELOW_S = 30.0

# Inbound `pet-interaction` rate limit per client (Phase 3 spec §5.3).
PET_INTERACTION_MAX_PER_S = 10

RunProactiveTurn = Callable[
    [Any, Callable[[str], Awaitable[None]], str, str], Awaitable[None]
]


class BehaviorScheduler:
    def __init__(
        self,
        client_connections: Dict[str, Any],
        client_contexts: Dict[str, Any],
        current_conversation_tasks: Dict[str, Optional[Any]],
        chat_group_manager: Any,
        run_proactive_turn: RunProactiveTurn,
        sensor: Optional[ContextSensor] = None,
        tick_seconds: float = 20.0,
        preempt_warn_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        wall_clock: Callable[[], datetime] = datetime.now,
    ):
        self.client_connections = client_connections
        self.client_contexts = client_contexts
        self.current_conversation_tasks = current_conversation_tasks
        self.chat_group_manager = chat_group_manager
        self.eligibility = ClientEligibility(
            client_connections,
            client_contexts,
            current_conversation_tasks,
            chat_group_manager,
        )
        self._run_proactive_turn = run_proactive_turn
        self.sensor: ContextSensor = (
            sensor if sensor is not None else make_default_sensor()
        )
        self.tick_seconds = tick_seconds
        self.preempt_warn_seconds = preempt_warn_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self.rng = rng if rng is not None else random.Random()
        self.selector = BehaviorSelector(self.rng)

        self.presences: Dict[str, ClientPresence] = {}
        self.state = SchedulerState()
        # Phase 3A desktop pet lane: only clients that sent `pet-hello`.
        self.pet_presences: Dict[str, PetPresence] = {}
        self.pet_selector = PetSelector(self.rng)
        self._pet_command_seq = 0
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle (spec §9)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def client_connected(self, uid: str) -> None:
        async with self._lock:
            if uid not in self.presences:
                self.presences[uid] = ClientPresence(
                    uid=uid, connected_at=self._clock()
                )
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
                logger.debug("[Behavior] scheduler started")

    async def client_disconnected(self, uid: str) -> None:
        async with self._lock:
            self.presences.pop(uid, None)
            self.pet_presences.pop(uid, None)
            if self.presences or self._task is None:
                return
            task = self._task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None
            self.state = SchedulerState()
            logger.debug("[Behavior] scheduler stopped")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self.tick_once()
            except Exception:
                logger.exception("[Behavior] tick failed")

    # ------------------------------------------------------------------
    # Tick pipeline (spec §4.4, §7)
    # ------------------------------------------------------------------

    async def tick_once(self) -> None:
        handled = [uid for uid in self.presences if self.eligibility.handles(uid)]
        if not handled:
            return

        now = self._clock()
        ctx = self.sensor.sample()
        self.state.last_context = ctx

        brains_by_id: Dict[int, PetBrain] = {}
        clients_by_brain: Dict[int, List[str]] = {}
        for uid in handled:
            brain = self.eligibility.brain_for(uid)
            if brain is None:
                continue
            brains_by_id[id(brain)] = brain
            clients_by_brain.setdefault(id(brain), []).append(uid)

        self._log_context_label(ctx, handled)

        prev_idle = self.state.previous_user_idle_seconds
        for key, brain in brains_by_id.items():
            uids = clients_by_brain[key]
            self._tick_brain(brain, uids, ctx, prev_idle, now)

        self.state.previous_user_idle_seconds = ctx.user_idle_seconds

        self._detect_ignored(handled, now)

        await self._select_and_dispatch(now, ctx)

        await self._pet_lane(now)

    def _log_context_label(self, ctx: ContextSnapshot, handled: List[str]) -> None:
        # Label uses the most relevant handled client's brain table.
        first_uid = self.eligibility.order(handled, self.presences)[0]
        brain = self.eligibility.brain_for(first_uid)
        if brain is None or not brain.config.context.enabled:
            # Never log a process name for a brain whose context is disabled.
            ctx = ContextSnapshot.unknown(ctx.taken_at_wall)
        category = None
        if brain is not None:
            category = classify_process(
                ctx.process_name, brain.config.context.process_categories
            )
        label = f"{category}({ctx.process_name})"
        if label != self.state.last_category_label:
            logger.debug(f"[Context] {label}")
            self.state.last_category_label = label

    def _tick_brain(
        self,
        brain: PetBrain,
        uids: List[str],
        ctx: ContextSnapshot,
        prev_idle: Optional[float],
        now: float,
    ) -> None:
        context_cfg = brain.config.context
        brain_ctx = (
            ctx if context_cfg.enabled else ContextSnapshot.unknown(ctx.taken_at_wall)
        )
        category = classify_process(
            brain_ctx.process_name, context_cfg.process_categories
        )
        away_s = context_cfg.away_after_min * 60
        returned = (
            prev_idle is not None
            and prev_idle >= away_s
            and brain_ctx.user_idle_seconds is not None
            and brain_ctx.user_idle_seconds < RETURNED_IDLE_BELOW_S
        )

        presences = [self.presences[uid] for uid in uids]
        timestamps = [
            ts
            for p in presences
            for ts in (p.last_conversation_end, p.last_user_interaction)
            if ts is not None
        ]
        if timestamps:
            quiet_seconds = now - max(timestamps)
        else:
            quiet_seconds = now - brain.lifecycle.since

        conversation_running = any(
            p.reserved or self._task_running(p.uid) for p in presences
        )

        brain.tick(
            BrainTickInputs(
                context=brain_ctx,
                category=category,
                quiet_seconds=quiet_seconds,
                conversation_running=conversation_running,
                user_returned=returned,
            )
        )

        if returned:
            for p in presences:
                p.returned_bonus_pending = True
                p.returned_after_s = prev_idle
            for uid in uids:
                pet = self.pet_presences.get(uid)
                if pet is not None:
                    pet.returned_pending = True

    def _task_running(self, uid: str) -> bool:
        task = self.current_conversation_tasks.get(uid)
        return task is not None and not task.done()

    def _detect_ignored(self, handled: List[str], now: float) -> None:
        for uid in handled:
            presence = self.presences[uid]
            awaiting = presence.awaiting_reply_since
            if awaiting is None:
                continue
            brain = self.eligibility.brain_for(uid)
            if brain is None:
                continue
            proactive_cfg = brain.config.proactive
            if now - awaiting < proactive_cfg.ignored_after_min * 60:
                continue
            presence.ignored_count += 1
            presence.awaiting_reply_since = None
            brain.notify(BrainEvent.PROACTIVE_IGNORED)
            backoff_min = (
                effective_min_interval_s(proactive_cfg, presence.ignored_count) / 60
            )
            logger.info(
                f"[Proactive] ignored count={presence.ignored_count} "
                f"backoff={backoff_min:g}min"
            )

    # ------------------------------------------------------------------
    # Decision, commit and dispatch (spec §8.1, §8.5, §8.9, §8.10)
    # ------------------------------------------------------------------

    async def _select_and_dispatch(self, now: float, ctx: ContextSnapshot) -> None:
        # `now`/`ctx` are this tick's values; `evaluate_client` reads the same
        # clock and `state.last_context` so TICK and REQUEST share one path.
        handled = [uid for uid in self.presences if self.eligibility.handles(uid)]
        allow_proactive = True
        for uid in self.eligibility.order(handled, self.presences):
            d = self.evaluate_client(uid, Trigger.TICK, allow_proactive=allow_proactive)
            if d.kind is BehaviorKind.PROACTIVE_SPEAK:
                # At most one proactive turn per tick in total (spec §8.2).
                allow_proactive = False
            elif d.kind is BehaviorKind.IDLE_EXPRESSION:
                await self._dispatch_idle(uid, d.emotion)

    def request_proactive(self, uid: str, images: Optional[list] = None) -> Decision:
        """Legacy `ai-speak-signal` entry point (spec §8.1, §8.8). Synchronous."""

        if images:
            logger.debug(
                f"[Behavior] discarded {len(images)} legacy proactive image(s)"
            )
        self.state.last_context = self.sensor.sample()
        return self.evaluate_client(uid, Trigger.REQUEST)

    def evaluate_client(
        self, uid: str, trigger: Trigger, *, allow_proactive: bool = True
    ) -> Decision:
        """The only decision path for both TICK and REQUEST.

        Synchronous on purpose: there is no `await` between the eligibility
        check and the proactive task creation (spec §8.5). With
        `allow_proactive=False` a PROACTIVE_SPEAK selection is turned into
        `DO_NOTHING (proactive_slot_taken)` without committing.
        """

        d = self._decide(uid, trigger, allow_proactive)
        message = (
            f"[Behavior] uid={uid} trigger={trigger.value} → {d.kind.name} ({d.reason})"
        )
        if trigger is Trigger.REQUEST or d.kind is not BehaviorKind.DO_NOTHING:
            logger.info(message)
        else:
            logger.debug(message)
        return d

    def _decide(self, uid: str, trigger: Trigger, allow_proactive: bool) -> Decision:
        now = self._clock()
        presence = self.presences.get(uid)
        reason = self.eligibility.check(uid, presence)
        if reason is not None:
            return Decision(BehaviorKind.DO_NOTHING, reason)
        if presence is None:
            return Decision(BehaviorKind.DO_NOTHING, "not_connected")

        brain = self.eligibility.brain_for(uid)
        ctx, category = self._context_for(brain)

        live2d_model = getattr(self.client_contexts.get(uid), "live2d_model", None)
        emo_map = getattr(live2d_model, "emo_map", None) or {}
        mood = brain.mood.snapshot()

        d = self.selector.select(
            SelectionInput(
                trigger=trigger,
                now=now,
                presence=presence,
                activity=brain.activity,
                lifecycle=brain.lifecycle.phase,
                mood=mood,
                context=ctx,
                category=category,
                config=brain.config,
                emo_map=emo_map,
            )
        )

        returned_after_s = None
        if d.willingness is not None:
            # The returned bonus is consumed either way (spec §8.4); keep its
            # value for the prompt before clearing it.
            if presence.returned_bonus_pending:
                returned_after_s = presence.returned_after_s
            presence.returned_bonus_pending = False
            presence.returned_after_s = None

        if d.kind is BehaviorKind.PROACTIVE_SPEAK:
            if not allow_proactive:
                return Decision(BehaviorKind.DO_NOTHING, "proactive_slot_taken")
            committed = self._commit_proactive(
                uid,
                brain,
                presence,
                ctx,
                category,
                trigger,
                d.willingness,
                mood,
                returned_after_s,
                now,
            )
            if not committed:
                return Decision(BehaviorKind.DO_NOTHING, "conversation_lock")
        return d

    def _context_for(self, brain: PetBrain) -> Tuple[ContextSnapshot, Optional[str]]:
        """Last sampled context as seen by `brain` (unknown if its context is disabled)."""

        last = self.state.last_context
        if last is None or not brain.config.context.enabled:
            wall = last.taken_at_wall if last is not None else self._wall_clock()
            ctx = ContextSnapshot.unknown(wall)
        else:
            ctx = last
        category = classify_process(
            ctx.process_name, brain.config.context.process_categories
        )
        return ctx, category

    def _commit_proactive(
        self,
        uid: str,
        brain: PetBrain,
        presence: ClientPresence,
        ctx: ContextSnapshot,
        category: Optional[str],
        trigger: Trigger,
        willingness: Optional[float],
        mood: Dict[str, float],
        returned_after_s: Optional[float],
        now: float,
    ) -> bool:
        """Atomic commit (spec §8.5). Synchronous: no `await` in here."""

        if (
            self.eligibility.check(uid, presence) is not None
            or brain.activity != ActivityState.IDLE
        ):
            return False

        presence.reserved = True
        presence.last_proactive = now
        prune_window(presence.proactive_timestamps, now)
        presence.proactive_timestamps.append(now)
        presence.proactive_preempted = False

        block = build_context_block(
            now_wall=ctx.taken_at_wall,
            day_part=day_part_at(ctx.taken_at_wall),
            lifecycle=brain.lifecycle.phase,
            mood=mood,
            category=category,
            process_name=ctx.process_name,
            user_idle_seconds=ctx.user_idle_seconds,
            returned_after_s=returned_after_s,
            trigger=trigger,
        )

        context = self.client_contexts[uid]
        websocket = self.client_connections[uid]
        task = asyncio.create_task(
            self._run_proactive_turn(context, websocket.send_text, uid, block)
        )
        self.current_conversation_tasks[uid] = task
        presence.proactive_task = task
        task.add_done_callback(functools.partial(self._on_proactive_done, uid, brain))

        w = f"{willingness:.2f}" if willingness is not None else "None"
        logger.info(
            f"[Proactive] committed uid={uid} willingness={w} "
            f"category={category}({ctx.process_name})"
        )
        return True

    def _on_proactive_done(self, uid: str, brain: PetBrain, task: asyncio.Task) -> None:
        error = None if task.cancelled() else task.exception()
        presence = self.presences.get(uid)
        if presence is None or presence.proactive_task is not task:
            # Client disconnected (or reconnected with a fresh presence).
            return

        presence.reserved = False
        presence.proactive_task = None
        presence.last_conversation_end = self._clock()

        if error is not None:
            logger.debug(f"[Proactive] turn ended with error uid={uid}: {error!r}")
        normal = (
            not task.cancelled() and error is None and not presence.proactive_preempted
        )
        presence.proactive_preempted = False
        if normal:
            presence.awaiting_reply_since = self._clock()
            brain.notify(BrainEvent.PROACTIVE_SPOKEN)

    # ------------------------------------------------------------------
    # User preemption (spec §8.6)
    # ------------------------------------------------------------------

    async def preempt_for_user(self, uid: str) -> None:
        """Stop a running proactive turn before a user turn is created.

        Waits without an upper bound until the proactive task has really
        finished, so the user task never overlaps it. `preempt_warn_seconds`
        is only a diagnostic threshold. Never touches `reserved` /
        `proactive_task` (the proactive done-callback releases them), never
        takes the lifecycle lock.

        Callers must call `user_turn_registered(uid, task_or_None)` in a
        `finally`, otherwise `user_turn_pending` stays set if they are
        cancelled while waiting here.
        """

        presence = self.presences.get(uid)
        if presence is None:
            return

        presence.last_user_interaction = self._clock()
        presence.ignored_count = 0
        presence.awaiting_reply_since = None
        presence.user_turn_pending = True

        task = presence.proactive_task
        if task is None:
            return
        if not task.done() and not presence.proactive_preempted:
            # A second concurrent preempt only waits; it never re-cancels.
            presence.proactive_preempted = True
            task.cancel()

        # `asyncio.wait` never cancels or abandons the task on timeout. Even an
        # already-done task is awaited so its pending done-callback runs first.
        done, _ = await asyncio.wait({task}, timeout=self.preempt_warn_seconds)
        if not done:
            logger.error(
                f"[Proactive] preempt still waiting after "
                f"{self.preempt_warn_seconds:g}s uid={uid}"
            )
            await asyncio.wait({task})

        if self.presences.get(uid) is presence:
            # A turn that finished normally just before the preemption may
            # have set it in its done-callback; the user is answering now.
            presence.awaiting_reply_since = None

    def user_turn_registered(self, uid: str, task: Optional[asyncio.Task]) -> None:
        """Called right after the user task was registered (spec §8.6)."""

        presence = self.presences.get(uid)
        if presence is None:
            return
        presence.user_turn_pending = False
        if task is not None:
            task.add_done_callback(
                functools.partial(self._on_user_turn_done, uid, presence)
            )

    def _on_user_turn_done(
        self, uid: str, presence: ClientPresence, task: asyncio.Task
    ) -> None:
        if self.presences.get(uid) is not presence:
            # Client disconnected (or reconnected with a fresh presence).
            return
        presence.last_conversation_end = self._clock()

    async def _dispatch_idle(self, uid: str, emotion: Optional[str]) -> None:
        """Send an expression-only payload to this client (spec §8.9).

        Never touches the lifecycle lock or connect/disconnect paths; a send
        failure (closed websocket) is logged and swallowed so it never breaks
        the tick.
        """

        brain = self.eligibility.brain_for(uid)
        context = self.client_contexts.get(uid)
        websocket = self.client_connections.get(uid)
        if brain is None or context is None or websocket is None or emotion is None:
            return
        actions = brain.emotion.apply_idle(
            emotion, getattr(context, "live2d_model", None)
        )
        if actions is None:
            return

        now = self._clock()
        presence = self.presences.get(uid)
        if presence is not None:
            presence.last_idle_expression = now
            prune_window(presence.idle_expression_timestamps, now)
            presence.idle_expression_timestamps.append(now)

        payload = prepare_audio_payload(
            audio_path=None, display_text=None, actions=actions
        )
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            logger.debug(f"[Behavior] idle expression send failed uid={uid}: {e}")

    # ------------------------------------------------------------------
    # Desktop pet lane (Phase 3 spec §5, §6)
    # ------------------------------------------------------------------

    def _pet_brain(self, uid: str) -> Optional[PetBrain]:
        """Brain for a handled client whose `desktop_pet` is enabled, else `None`."""

        if not self.eligibility.handles(uid):
            return None
        brain = self.eligibility.brain_for(uid)
        if brain is None or not brain.config.desktop_pet.enabled:
            return None
        return brain

    async def handle_pet_message(self, uid: str, data: dict) -> None:
        """Single entry point for `pet-hello`, `pet-status` and `pet-interaction`."""

        if uid not in self.presences:
            logger.debug(f"[Pet] message from unknown client uid={uid} ignored")
            return
        msg = parse_pet_message(data)
        if msg is None:
            logger.debug(f"[Pet] invalid message dropped uid={uid}: {data!r:.200}")
            return

        if isinstance(msg, PetHello):
            self.pet_presences[uid] = PetPresence(
                protocol=msg.protocol,
                mode=msg.mode,
                movement_enabled=msg.movement_enabled,
            )
            logger.info(
                f"[Pet] hello uid={uid} protocol={msg.protocol} mode={msg.mode} "
                f"movement={msg.movement_enabled}"
            )
            return

        pet = self.pet_presences.get(uid)
        if pet is None:
            logger.debug(f"[Pet] {data.get('type')} before pet-hello uid={uid} ignored")
            return

        if isinstance(msg, PetStatus):
            self._apply_pet_status(uid, pet, msg)
            return

        now = self._clock()
        prune_window(pet.message_times, now, window_s=1.0)
        if len(pet.message_times) >= PET_INTERACTION_MAX_PER_S:
            logger.debug(f"[Pet] interaction rate limit uid={uid}")
            return
        pet.message_times.append(now)
        await self._handle_interaction(uid, msg)

    def _apply_pet_status(self, uid: str, pet: PetPresence, msg: PetStatus) -> None:
        pet.mode = msg.mode
        pet.movement_enabled = msg.movement_enabled
        if msg.anchor is not None:
            pet.anchor = msg.anchor
        if msg.result is None:
            return
        if msg.command_id is None or msg.command_id != pet.pending_command_id:
            logger.debug(
                f"[Pet] result for stale command id={msg.command_id} uid={uid} ignored"
            )
            return
        pet.pending_command_id = None
        pet.pending_since = None
        pet.moving = False
        if msg.result == "arrived":
            pet.last_move = self._clock()
        reason = f" ({msg.reason})" if msg.reason else ""
        logger.info(
            f"[Pet] result id={msg.command_id} {msg.result}{reason} anchor={pet.anchor}"
        )

    async def _handle_interaction(self, uid: str, msg: PetInteraction) -> None:
        brain = self._pet_brain(uid)
        pet = self.pet_presences.get(uid)
        if brain is None or pet is None:
            return
        cfg = brain.config.desktop_pet.interaction
        if not cfg.enabled:
            return

        now = self._clock()
        presence = self.presences[uid]
        presence.last_user_interaction = now

        spam = False
        if msg.kind in ("click", "double_click"):
            # Rapid clicking reaches us as click/double_click pairs; both count.
            prune_window(pet.click_times, now, window_s=cfg.spam_window_s)
            pet.click_times.append(now)
            if len(pet.click_times) >= cfg.spam_clicks:
                spam = True
                pet.click_times.clear()
                brain.notify(BrainEvent.PET_SPAMMED)
        if msg.kind == "click":
            brain.notify(BrainEvent.PET_CLICKED)
        elif msg.kind == "double_click":
            brain.notify(BrainEvent.PET_POKED)
            pet.paused_until = now + cfg.attention_pause_min * 60
            if pet.pending_command_id is not None:
                await self._send_pet_command(uid, "stop", {})
        elif msg.kind == "drag_end":
            brain.notify(BrainEvent.PET_DRAGGED)
            pet.paused_until = now + cfg.drag_pause_min * 60
            pet.anchor = "home"

        key = "spam" if spam else choose_reaction(msg.kind, brain.mood.snapshot(), spam)
        if key is None:
            return
        name = cfg.reactions.get(key)
        blocked = (
            self.eligibility.check(uid, presence) is not None
            or brain.lifecycle.phase is LifecyclePhase.SLEEP
        )
        # A click burst is the "annoyed" reaction itself and always shows. A
        # double click always follows a click by < 350 ms, so it bypasses the
        # cooldown too, except that nothing overrides a fresh "spam" reaction.
        in_cooldown = (
            pet.last_reaction is not None
            and now - pet.last_reaction < cfg.reaction_cooldown_s
        )
        cooling = (
            in_cooldown
            and not spam
            and (msg.kind != "double_click" or pet.last_reaction_key == "spam")
        )
        suppressed = " suppressed" if blocked or cooling else ""
        logger.info(
            f"[Pet] interaction {msg.kind} uid={uid} -> reaction {key}({name}){suppressed}"
        )
        if blocked or cooling or name is None:
            return

        live2d_model = getattr(self.client_contexts.get(uid), "live2d_model", None)
        actions = brain.emotion.apply_reaction(name, live2d_model)
        if actions is None:
            return
        pet.last_reaction = now
        pet.last_reaction_key = key
        payload = prepare_audio_payload(
            audio_path=None, display_text=None, actions=actions
        )
        await self._send_json(uid, payload, "reaction")

    async def _pet_lane(self, now: float) -> None:
        for uid in list(self.pet_presences):
            brain = self._pet_brain(uid)
            pet = self.pet_presences.get(uid)
            if brain is None or pet is None:
                continue
            _, category = self._context_for(brain)
            pet.category_changed = (
                pet.last_category is not None and category != pet.last_category
            )
            pet.last_category = category

            timeout = brain.config.desktop_pet.movement.command_timeout_s
            if (
                pet.pending_command_id is not None
                and pet.pending_since is not None
                and now - pet.pending_since >= timeout
            ):
                logger.warning(
                    f"[Pet] timeout id={pet.pending_command_id} uid={uid}; clearing"
                )
                pet.pending_command_id = None
                pet.pending_since = None
                pet.moving = False

            d = self.evaluate_pet(uid)
            await self._dispatch_pet(uid, d)

    def evaluate_pet(self, uid: str) -> PetDecision:
        """Synchronous pet-lane decision for one client."""

        brain = self._pet_brain(uid)
        pet = self.pet_presences.get(uid)
        presence = self.presences.get(uid)
        if brain is None or pet is None or presence is None:
            return PetDecision(PetKind.STAY, "disabled")

        ctx, category = self._context_for(brain)
        live2d_model = getattr(self.client_contexts.get(uid), "live2d_model", None)
        d = self.pet_selector.select(
            PetSelectionInput(
                now=self._clock(),
                cfg=brain.config.desktop_pet,
                presence=pet,
                mood=brain.mood.snapshot(),
                lifecycle=brain.lifecycle.phase,
                context=ctx,
                category=category,
                conversation_active=self.eligibility.check(uid, presence) is not None,
                last_conversation_end=presence.last_conversation_end,
                motion_map=getattr(live2d_model, "motion_map", None) or {},
            )
        )
        message = f"[Pet] uid={uid} -> {d.kind.name} ({d.reason})"
        if d.kind is PetKind.STAY:
            logger.debug(message)
        else:
            logger.info(message)
        return d

    async def _dispatch_pet(self, uid: str, decision: PetDecision) -> None:
        pet = self.pet_presences.get(uid)
        if pet is None:
            return
        if pet.returned_pending and (
            decision.kind is PetKind.GO_HOME or pet.anchor == "home"
        ):
            pet.returned_pending = False

        command = movement_command(decision)
        if command is None:
            return
        name, params = command
        now = self._clock()

        if decision.kind is PetKind.IDLE_MOTION:
            if await self._send_pet_command(uid, name, params) is None:
                return
            pet.last_idle_motion = now
            prune_window(pet.idle_motion_timestamps, now)
            pet.idle_motion_timestamps.append(now)
            logger.info(f"[Motion] idle {decision.params.get('name')} uid={uid}")
            return

        # Mark the command pending *before* the send await: the frontend's
        # result (or a double click's stop) may be handled during that await.
        command_id = self._next_command_id()
        previous = (pet.last_move, pet.last_contextual)
        pet.pending_command_id = command_id
        pet.pending_since = now
        pet.moving = True
        pet.last_move = now
        prune_window(pet.move_timestamps, now)
        pet.move_timestamps.append(now)
        if decision.kind is PetKind.APPROACH_WINDOW:
            pet.last_contextual = now

        if await self._send_pet_command(uid, name, params, command_id) is None:
            # Nothing reached the frontend: undo the reservation.
            if pet.pending_command_id == command_id:
                pet.pending_command_id = None
                pet.pending_since = None
                pet.moving = False
            pet.last_move, pet.last_contextual = previous
            if pet.move_timestamps and pet.move_timestamps[-1] == now:
                pet.move_timestamps.pop()

    def _next_command_id(self) -> str:
        self._pet_command_seq += 1
        return f"c-{self._pet_command_seq}"

    async def _send_pet_command(
        self,
        uid: str,
        command: str,
        params: dict,
        command_id: Optional[str] = None,
    ) -> Optional[str]:
        command_id = command_id or self._next_command_id()
        payload = {
            "type": "pet-command",
            "id": command_id,
            "command": command,
            "params": params,
        }
        if not await self._send_json(uid, payload, "command"):
            return None
        logger.info(f"[Pet] command {command} id={command_id} uid={uid}")
        return command_id

    async def _send_json(self, uid: str, payload: dict, what: str) -> bool:
        websocket = self.client_connections.get(uid)
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps(payload))
            return True
        except Exception as e:
            logger.debug(f"[Pet] {what} send failed uid={uid}: {e}")
            return False
