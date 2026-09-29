"""Phase 3A desktop pet tests. Run from repo root: uv run python -m unittest tests.test_desktop_pet_phase3 -v"""

import inspect
import json
import os
import tempfile
import copy
import unittest
from collections import deque
from dataclasses import replace
from datetime import datetime

from loguru import logger
from pydantic import ValidationError

from src.open_llm_vtuber.config_manager import (
    DesktopPetConfig,
    PetBrainConfig,
    PetContextualConfig,
    PetIdleMotionConfig,
    PetInteractionConfig,
    PetMovementConfig,
    read_yaml,
    validate_config,
)
from src.open_llm_vtuber.live2d_model import Live2dModel
from src.open_llm_vtuber.pet_brain import BrainEvent, PetBrain
from src.open_llm_vtuber.pet_brain import context as context_module
from src.open_llm_vtuber.pet_brain.context import (
    OWN_PROCESS_NAMES,
    ContextSnapshot,
    WindowsContextSensor,
)
from src.open_llm_vtuber.pet_brain.desktop_pet import (
    PROTOCOL_VERSION,
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
from src.open_llm_vtuber.pet_brain.emotion_manager import EmotionManager
from src.open_llm_vtuber.pet_brain.lifecycle import LifecyclePhase
from src.open_llm_vtuber.service_context import ServiceContext

MAO_MODEL = Live2dModel("mao_pro")


class _LoguruCapture:
    def __init__(self, level="WARNING"):
        self._level = level

    def __enter__(self):
        self.records = []
        self._id = logger.add(lambda m: self.records.append(str(m)), level=self._level)
        return self.records

    def __exit__(self, *exc):
        logger.remove(self._id)
        return False


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        dp = PetBrainConfig().desktop_pet
        self.assertIsInstance(dp, DesktopPetConfig)
        self.assertTrue(dp.enabled)

        self.assertIsInstance(dp.movement, PetMovementConfig)
        self.assertTrue(dp.movement.enabled)
        self.assertEqual(dp.movement.min_interval_min, 4)
        self.assertEqual(dp.movement.max_per_hour, 8)
        self.assertEqual(dp.movement.chance, 0.4)
        self.assertEqual(dp.movement.wander_threshold, 0.35)
        self.assertEqual(dp.movement.post_conversation_quiet_min, 1)
        self.assertEqual(dp.movement.command_timeout_s, 60)

        self.assertIsInstance(dp.contextual, PetContextualConfig)
        self.assertTrue(dp.contextual.enabled)
        self.assertEqual(dp.contextual.categories, ["coding", "unity"])
        self.assertEqual(dp.contextual.min_curiosity, 0.5)
        self.assertEqual(dp.contextual.cooldown_min, 30)

        self.assertIsInstance(dp.interaction, PetInteractionConfig)
        self.assertTrue(dp.interaction.enabled)
        self.assertEqual(dp.interaction.reaction_cooldown_s, 3)
        self.assertEqual(dp.interaction.spam_clicks, 5)
        self.assertEqual(dp.interaction.spam_window_s, 10)
        self.assertEqual(dp.interaction.drag_pause_min, 5)
        self.assertEqual(dp.interaction.attention_pause_min, 3)
        self.assertEqual(
            dp.interaction.reactions,
            {
                "click_happy": "joy",
                "click_neutral": "surprise",
                "spam": "anger",
                "double_click": "surprise",
                "drag_playful": "smirk",
                "drag_annoyed": "anger",
            },
        )

        self.assertIsInstance(dp.idle_motion, PetIdleMotionConfig)
        self.assertTrue(dp.idle_motion.enabled)
        self.assertEqual(dp.idle_motion.min_interval_min, 5)
        self.assertEqual(dp.idle_motion.max_per_hour, 6)
        self.assertEqual(dp.idle_motion.chance, 0.3)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValidationError):
            PetMovementConfig(chance=1.5)
        with self.assertRaises(ValidationError):
            PetInteractionConfig(spam_clicks=1)
        with self.assertRaises(ValidationError):
            PetContextualConfig(categories=["games"])
        with self.assertRaises(ValidationError):
            PetInteractionConfig(reactions={"poke": "joy"})
        with self.assertRaises(ValidationError):
            PetMovementConfig(min_interval_min=-1)
        with self.assertRaises(ValidationError):
            PetIdleMotionConfig(chance=-0.1)

    def test_partial_reactions_keep_other_defaults(self):
        cfg = PetInteractionConfig(reactions={"spam": "sadness"})
        self.assertEqual(cfg.reactions["spam"], "sadness")
        self.assertEqual(cfg.reactions["click_happy"], "joy")

    def test_templates_validate(self):
        for path in (
            "config_templates/conf.default.yaml",
            "config_templates/conf.ZH.default.yaml",
        ):
            raw = read_yaml(path)
            self.assertIn(
                "desktop_pet", raw["character_config"]["pet_brain_config"], path
            )
            config = validate_config(raw)
            pb = config.character_config.pet_brain_config
            self.assertFalse(pb.enabled)
            self.assertTrue(pb.desktop_pet.enabled)
            self.assertEqual(pb.desktop_pet, DesktopPetConfig())

    def test_desktop_pet_change_updates_in_place(self):
        ctx = ServiceContext()
        cfg_a = PetBrainConfig(enabled=True)
        self.assertTrue(ctx.init_pet_brain(cfg_a))
        brain = ctx.pet_brain

        cfg_b = cfg_a.model_copy(deep=True)
        cfg_b.desktop_pet.movement.chance = 0.9

        self.assertFalse(ctx.init_pet_brain(cfg_b))
        self.assertIs(ctx.pet_brain, brain)
        self.assertEqual(brain.config.desktop_pet.movement.chance, 0.9)


class InteractionPrimitiveTests(unittest.TestCase):
    def _brain(self):
        return PetBrain(PetBrainConfig(enabled=True))

    def _delta(self, event):
        brain = self._brain()
        before = brain.mood.snapshot()
        brain.notify(event)
        after = brain.mood.snapshot()
        return {
            k: round(after[k] - before[k], 6) for k in before if after[k] != before[k]
        }

    def test_event_effects(self):
        self.assertEqual(
            self._delta(BrainEvent.PET_CLICKED),
            {"social_need": -0.03, "boredom": -0.05, "happiness": 0.02},
        )
        self.assertEqual(self._delta(BrainEvent.PET_SPAMMED), {"happiness": -0.05})
        self.assertEqual(
            self._delta(BrainEvent.PET_POKED),
            {"social_need": -0.05, "curiosity": 0.05},
        )
        self.assertEqual(self._delta(BrainEvent.PET_DRAGGED), {"boredom": -0.05})

    def test_event_values(self):
        self.assertEqual(BrainEvent.PET_CLICKED.value, "pet_clicked")
        self.assertEqual(BrainEvent.PET_SPAMMED.value, "pet_spammed")
        self.assertEqual(BrainEvent.PET_POKED.value, "pet_poked")
        self.assertEqual(BrainEvent.PET_DRAGGED.value, "pet_dragged")

    def test_poked_wakes_from_sleep(self):
        brain = self._brain()
        brain.lifecycle.phase = LifecyclePhase.SLEEP
        brain.notify(BrainEvent.PET_CLICKED)
        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.SLEEP)
        brain.notify(BrainEvent.PET_POKED)
        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.ACTIVE)

    def test_pet_events_do_not_change_activity(self):
        brain = self._brain()
        activity = brain.activity
        for event in (
            BrainEvent.PET_CLICKED,
            BrainEvent.PET_SPAMMED,
            BrainEvent.PET_POKED,
            BrainEvent.PET_DRAGGED,
        ):
            brain.notify(event)
            self.assertEqual(brain.activity, activity)

    def test_apply_reaction(self):
        em = EmotionManager()
        actions = em.apply_reaction("joy", MAO_MODEL)
        self.assertEqual(actions.expressions, [3])
        self.assertEqual(em.snapshot()["current_emotion"], "joy")
        self.assertEqual(em.snapshot()["current_emotion_source"], "interaction")
        self.assertIsNone(em.apply_reaction("giggle", MAO_MODEL))
        self.assertIsNone(em.apply_reaction("joy", object()))

    def test_motion_map_loading(self):
        entry = {
            "name": "tmp_model",
            "url": "/x.model3.json",
            "emotionMap": {"neutral": 0},
            "motionMap": {
                "yawn": {"group": "", "index": 3},
                "bad": {"group": 1, "index": -1},
                "worse": "nope",
            },
        }
        plain = dict(entry, name="plain_model")
        del plain["motionMap"]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "model_dict.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([entry, plain], f)
            with _LoguruCapture() as logs:
                model = Live2dModel("tmp_model", model_dict_path=path)
            self.assertEqual(model.motion_map, {"yawn": {"group": "", "index": 3}})
            self.assertTrue(any("motionMap" in r for r in logs))
            self.assertEqual(
                Live2dModel("plain_model", model_dict_path=path).motion_map, {}
            )


class _FakeUser32:
    """Minimal user32 stand-in for `_read_foreground_rect`."""

    def __init__(self, rect=(10, 20, 810, 620), hwnd=100, iconic=0, rect_ok=True):
        self.rect = rect
        self.hwnd = hwnd
        self.iconic = iconic
        self.rect_ok = rect_ok
        self.dpi_calls = []

    def GetForegroundWindow(self):
        return self.hwnd

    def GetDesktopWindow(self):
        return 1

    def GetShellWindow(self):
        return 2

    def IsIconic(self, hwnd):
        return self.iconic

    def SetThreadDpiAwarenessContext(self, ctx):
        self.dpi_calls.append(ctx)
        return 17 if len(self.dpi_calls) == 1 else -4

    def GetWindowRect(self, hwnd, rect_ref):
        if self.rect_ok is None:
            raise OSError("boom")
        if not self.rect_ok:
            return 0
        r = rect_ref._obj
        r.left, r.top, r.right, r.bottom = self.rect
        return 1


class _RectSensor(WindowsContextSensor):
    def __init__(self, user32, process="Unity.exe"):
        super().__init__(wall_clock=lambda: datetime(2024, 1, 1, 9, 0))
        self._user32 = user32
        self._process = process

    def _read_idle_seconds(self) -> float:
        return 3.0

    def _read_process_name(self) -> str:
        return self._process

    def _read_fullscreen(self) -> bool:
        return False


class ForegroundRectTests(unittest.TestCase):
    def _sample(self, user32, process="Unity.exe"):
        with _LoguruCapture("DEBUG"):
            return _RectSensor(user32, process).sample()

    def test_rect_read(self):
        snap = self._sample(_FakeUser32())
        self.assertEqual(snap.foreground_rect, (10, 20, 810, 620))
        self.assertEqual(snap.process_name, "Unity.exe")

    def test_rect_none_cases(self):
        cases = [
            (_FakeUser32(iconic=1), "Unity.exe"),
            (_FakeUser32(hwnd=1), "Unity.exe"),
            (_FakeUser32(hwnd=2), "Unity.exe"),
            (_FakeUser32(hwnd=0), "Unity.exe"),
            (_FakeUser32(rect=(10, 10, 10, 300)), "Unity.exe"),
            (_FakeUser32(), "open-llm-vtuber-electron.exe"),
            (_FakeUser32(), "Electron.exe"),
            (_FakeUser32(rect_ok=False), "Unity.exe"),
            (_FakeUser32(rect_ok=None), "Unity.exe"),
        ]
        for user32, process in cases:
            snap = self._sample(user32, process)
            self.assertIsNone(snap.foreground_rect, (user32.__dict__, process))
            self.assertEqual(snap.user_idle_seconds, 3.0)
            self.assertFalse(snap.fullscreen)

    def test_dpi_context_restored(self):
        ok = _FakeUser32()
        self._sample(ok)
        self.assertEqual(ok.dpi_calls, [-4, 17])

        failing = _FakeUser32(rect_ok=None)
        self._sample(failing)
        self.assertEqual(failing.dpi_calls, [-4, 17])

    def test_missing_dpi_api_still_reads(self):
        user32 = _FakeUser32()
        user32.SetThreadDpiAwarenessContext = None
        self.assertEqual(self._sample(user32).foreground_rect, (10, 20, 810, 620))

    def test_unknown_has_no_rect(self):
        self.assertIsNone(ContextSnapshot.unknown(datetime(2024, 1, 1)).foreground_rect)

    def test_own_process_names(self):
        self.assertIn("open-llm-vtuber-electron.exe", OWN_PROCESS_NAMES)
        self.assertIn("electron.exe", OWN_PROCESS_NAMES)

    def test_no_window_text_api(self):
        self.assertNotIn("GetWindowText", inspect.getsource(context_module))


class ParseTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(PROTOCOL_VERSION, 1)
        self.assertEqual(
            parse_pet_message(
                {
                    "type": "pet-hello",
                    "protocol": 1,
                    "mode": "pet",
                    "movement_enabled": True,
                }
            ),
            PetHello(protocol=1, mode="pet", movement_enabled=True),
        )
        self.assertEqual(
            parse_pet_message(
                {
                    "type": "pet-status",
                    "mode": "pet",
                    "movement_enabled": True,
                    "moving": False,
                    "anchor": "edge",
                    "command_id": "c-42",
                    "result": "arrived",
                    "reason": None,
                }
            ),
            PetStatus(
                mode="pet",
                movement_enabled=True,
                moving=False,
                anchor="edge",
                command_id="c-42",
                result="arrived",
                reason=None,
            ),
        )
        self.assertEqual(
            parse_pet_message(
                {
                    "type": "pet-status",
                    "mode": "window",
                    "movement_enabled": False,
                    "moving": False,
                }
            ),
            PetStatus("window", False, False, None, None, None, None),
        )
        self.assertEqual(
            parse_pet_message(
                {"type": "pet-interaction", "kind": "click", "hit_area": "HitAreaHead"}
            ),
            PetInteraction(kind="click", hit_area="HitAreaHead"),
        )
        self.assertEqual(
            parse_pet_message({"type": "pet-interaction", "kind": "drag_end"}),
            PetInteraction(kind="drag_end", hit_area=None),
        )

    def test_parse_rejects_bad_types(self):
        hello = {
            "type": "pet-hello",
            "protocol": 1,
            "mode": "pet",
            "movement_enabled": True,
        }
        status = {
            "type": "pet-status",
            "mode": "pet",
            "movement_enabled": True,
            "moving": False,
        }
        bad = [
            dict(hello, protocol="1"),
            dict(hello, protocol=True),
            dict(hello, mode="desk"),
            dict(hello, movement_enabled="yes"),
            {"type": "pet-interaction", "kind": "kick"},
            {"type": "pet-interaction", "kind": "click", "hit_area": 5},
            dict(status, result="done"),
            dict(status, anchor="moon"),
            dict(status, moving=1),
            dict(status, command_id="c" * 65),
            dict(status, reason=["x"]),
            {"protocol": 1, "mode": "pet", "movement_enabled": True},
            {"type": "pet-unknown"},
            "not a dict",
            None,
        ]
        for data in bad:
            self.assertIsNone(parse_pet_message(data), data)

    def test_unknown_fields_ignored(self):
        self.assertEqual(
            parse_pet_message(
                {"type": "pet-interaction", "kind": "double_click", "x": 1, "extra": {}}
            ),
            PetInteraction(kind="double_click", hit_area=None),
        )


class _FixedRng:
    def __init__(self, value=0.1):
        self.value = value

    def random(self):
        return self.value


NOW = 100_000.0


def _presence(**overrides):
    p = PetPresence(protocol=1, mode="pet", movement_enabled=True, anchor="free")
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def make_input(presence=None, cfg=None, **overrides):
    base = dict(
        now=NOW,
        cfg=cfg or DesktopPetConfig(),
        presence=presence or _presence(),
        mood={
            "happiness": 0.6,
            "energy": 0.7,
            "curiosity": 0.5,
            "boredom": 0.8,
            "social_need": 0.4,
            "focus": 0.3,
            "sleepiness": 0.1,
        },
        lifecycle=LifecyclePhase.ACTIVE,
        context=ContextSnapshot(
            taken_at_wall=datetime(2024, 1, 1, 14, 0),
            user_idle_seconds=300.0,
            process_name="chrome.exe",
            fullscreen=False,
            foreground_rect=(100, 100, 900, 700),
        ),
        category="browser",
        conversation_active=False,
        last_conversation_end=None,
        motion_map={},
    )
    base.update(overrides)
    return PetSelectionInput(**base)


def _mood(**changes):
    return {**make_input().mood, **changes}


class SelectorTests(unittest.TestCase):
    def select(self, inp, rng=0.1):
        return PetSelector(_FixedRng(rng)).select(inp)

    def test_baseline_wanders(self):
        d = self.select(make_input())
        self.assertEqual((d.kind, d.reason), (PetKind.WANDER, "wander"))

    def test_vetoes(self):
        ctx = make_input().context
        no_move_cfg = DesktopPetConfig()
        no_move_cfg.movement.enabled = False
        cases = [
            (make_input(presence=_presence(mode="window")), "disabled"),
            (make_input(presence=_presence(movement_enabled=False)), "disabled"),
            (make_input(presence=_presence(protocol=None)), "disabled"),
            (make_input(cfg=DesktopPetConfig(enabled=False)), "disabled"),
            (make_input(cfg=no_move_cfg), "disabled"),
            (make_input(presence=_presence(moving=True)), "moving"),
            (make_input(conversation_active=True), "conversation"),
            (make_input(last_conversation_end=NOW - 30), "post_conversation"),
            (make_input(presence=_presence(paused_until=NOW + 10)), "paused"),
            (make_input(lifecycle=LifecyclePhase.SLEEP), "lifecycle_sleep"),
            (make_input(lifecycle=LifecyclePhase.AWAY), "lifecycle_away"),
            (make_input(context=replace(ctx, fullscreen=True)), "fullscreen"),
            (make_input(context=replace(ctx, fullscreen=None)), "context_unknown"),
            (
                make_input(context=replace(ctx, user_idle_seconds=None)),
                "context_unknown",
            ),
        ]
        for inp, reason in cases:
            d = self.select(inp)
            self.assertEqual((d.kind, d.reason), (PetKind.STAY, reason))

    def test_veto_order(self):
        d = self.select(
            make_input(presence=_presence(moving=True), conversation_active=True)
        )
        self.assertEqual(d.reason, "moving")

    def test_user_returned_goes_home(self):
        p = _presence(
            returned_pending=True,
            last_move=NOW - 10,
            move_timestamps=deque([NOW - 10] * 8),
        )
        d = self.select(make_input(presence=p), rng=0.99)
        self.assertEqual((d.kind, d.reason), (PetKind.GO_HOME, "user_returned"))
        p_home = _presence(returned_pending=True, anchor="home")
        self.assertNotEqual(
            self.select(make_input(presence=p_home)).kind, PetKind.GO_HOME
        )

    def test_contextual_approach(self):
        ctx = make_input().context

        def inp(presence_kw=None, category="unity", curiosity=0.6, **kw):
            p_kw = {"category_changed": True}
            p_kw.update(presence_kw or {})
            return make_input(
                presence=_presence(**p_kw),
                category=category,
                mood=_mood(curiosity=curiosity),
                **kw,
            )

        d = self.select(inp(), rng=0.99)
        self.assertEqual(
            (d.kind, d.reason), (PetKind.APPROACH_WINDOW, "curious_about_unity")
        )
        self.assertEqual(d.params["rect"], (100, 100, 900, 700))

        no_ctx_cfg = DesktopPetConfig()
        no_ctx_cfg.contextual.enabled = False
        blocked = [
            inp(curiosity=0.4),
            inp(context=replace(ctx, foreground_rect=None)),
            inp(presence_kw={"last_contextual": NOW - 29 * 60}),
            inp(category="browser"),
            inp(cfg=no_ctx_cfg),
            inp(presence_kw={"category_changed": False}),
            inp(presence_kw={"move_timestamps": deque([NOW - 60] * 8)}),
        ]
        for i in blocked:
            self.assertNotEqual(self.select(i, rng=0.99).kind, PetKind.APPROACH_WINDOW)

    def test_busy_user_goes_edge_then_stays(self):
        ctx = replace(make_input().context, user_idle_seconds=5.0)

        def busy(**p_kw):
            return make_input(
                presence=_presence(**p_kw), context=ctx, category="coding"
            )

        d = self.select(busy(last_move=NOW - 90))
        self.assertEqual((d.kind, d.reason), (PetKind.GO_EDGE, "user_busy"))
        d = self.select(busy(anchor="edge"))
        self.assertEqual((d.kind, d.reason), (PetKind.STAY, "user_busy"))
        d = self.select(busy(last_move=NOW - 30))
        self.assertEqual((d.kind, d.reason), (PetKind.STAY, "user_busy"))
        d = self.select(busy(move_timestamps=deque([NOW - 600] * 8)))
        self.assertEqual((d.kind, d.reason), (PetKind.STAY, "user_busy"))

    def test_frequency_gates(self):
        recent = _presence(last_move=NOW - 3 * 60)
        self.assertNotEqual(
            self.select(make_input(presence=recent)).kind, PetKind.WANDER
        )
        capped = _presence(
            last_move=NOW - 30 * 60, move_timestamps=deque([NOW - 30 * 60] * 8)
        )
        self.assertNotEqual(
            self.select(make_input(presence=capped)).kind, PetKind.WANDER
        )
        old = _presence(last_move=NOW - 5 * 60, move_timestamps=deque([NOW - 4000] * 8))
        self.assertEqual(self.select(make_input(presence=old)).kind, PetKind.WANDER)

    def test_sleepy_goes_edge(self):
        d = self.select(make_input(mood=_mood(sleepiness=0.8)))
        self.assertEqual((d.kind, d.reason), (PetKind.GO_EDGE, "sleepy"))
        d = self.select(make_input(lifecycle=LifecyclePhase.SLEEPY))
        self.assertEqual((d.kind, d.reason), (PetKind.GO_EDGE, "sleepy"))
        d = self.select(
            make_input(presence=_presence(anchor="edge"), mood=_mood(sleepiness=0.8))
        )
        self.assertNotEqual(d.kind, PetKind.GO_EDGE)

    def test_too_lazy(self):
        d = self.select(make_input(mood=_mood(energy=0.1)))
        self.assertNotEqual(d.kind, PetKind.WANDER)

    def test_wander_distance_and_speed(self):
        for energy, distance, speed in (
            (0.7, "long", "normal"),
            (0.45, "short", "normal"),
            (0.3, "short", "slow"),
        ):
            d = self.select(make_input(mood=_mood(energy=energy)))
            self.assertEqual(d.kind, PetKind.WANDER)
            self.assertEqual(d.params, {"distance": distance, "speed": speed})
        low = _mood(boredom=0.1, curiosity=0.1, energy=0.5, sleepiness=0.1)
        self.assertNotEqual(self.select(make_input(mood=low)).kind, PetKind.WANDER)
        self.assertNotEqual(self.select(make_input(), rng=0.5).kind, PetKind.WANDER)

    def test_idle_motion(self):
        mood = _mood(sleepiness=0.65, boredom=0.1)
        motion_map = {"yawn": {"group": "", "index": 3}}
        d = self.select(make_input(mood=mood, motion_map=motion_map))
        self.assertEqual((d.kind, d.reason), (PetKind.IDLE_MOTION, "idle_motion_yawn"))
        self.assertEqual(d.params, {"group": "", "index": 3, "name": "yawn"})

        d = self.select(make_input(mood=mood, motion_map={}))
        self.assertEqual((d.kind, d.reason), (PetKind.STAY, "nothing_to_do"))

        stretch = _mood(energy=0.7, boredom=0.5, sleepiness=0.1, curiosity=0.1)
        d = self.select(
            make_input(
                presence=_presence(last_move=NOW - 60),
                mood=stretch,
                motion_map={"stretch": {"group": "", "index": 1}},
            )
        )
        self.assertEqual(d.reason, "idle_motion_stretch")

        gated = _presence(last_idle_motion=NOW - 60)
        d = self.select(make_input(presence=gated, mood=mood, motion_map=motion_map))
        self.assertEqual(d.kind, PetKind.STAY)
        d = self.select(make_input(mood=mood, motion_map=motion_map), rng=0.35)
        self.assertEqual(d.kind, PetKind.STAY)

    def test_selector_does_not_mutate_presence(self):
        p = _presence(returned_pending=True, category_changed=True)
        before = copy.deepcopy(p)
        self.select(make_input(presence=p))
        self.assertEqual(p, before)


class ReactionTests(unittest.TestCase):
    def test_choose_reaction(self):
        m = {"happiness": 0.6, "energy": 0.5}
        self.assertEqual(choose_reaction("click", m, spam=False), "click_happy")
        self.assertEqual(
            choose_reaction("click", {**m, "happiness": 0.4}, spam=False),
            "click_neutral",
        )
        self.assertEqual(choose_reaction("click", m, spam=True), "spam")
        self.assertEqual(choose_reaction("double_click", m, spam=False), "double_click")
        self.assertEqual(choose_reaction("drag_end", m, spam=False), "drag_playful")
        self.assertEqual(
            choose_reaction("drag_end", {**m, "energy": 0.3}, spam=False),
            "drag_annoyed",
        )
        self.assertIsNone(choose_reaction("drag_start", m, spam=False))

    def test_movement_command_mapping(self):
        self.assertEqual(
            movement_command(
                PetDecision(
                    PetKind.WANDER, "wander", {"distance": "long", "speed": "slow"}
                )
            ),
            ("wander", {"distance": "long", "speed": "slow"}),
        )
        self.assertEqual(
            movement_command(PetDecision(PetKind.GO_EDGE, "sleepy")),
            ("go_edge", {"side": "nearest"}),
        )
        self.assertEqual(
            movement_command(PetDecision(PetKind.GO_HOME, "user_returned")),
            ("go_home", {}),
        )
        self.assertEqual(
            movement_command(
                PetDecision(PetKind.APPROACH_WINDOW, "c", {"rect": (10, 20, 110, 220)})
            ),
            (
                "approach_rect",
                {"rect": {"x": 10, "y": 20, "width": 100, "height": 200}},
            ),
        )
        self.assertEqual(
            movement_command(
                PetDecision(
                    PetKind.IDLE_MOTION, "m", {"group": "", "index": 3, "name": "yawn"}
                )
            ),
            ("play_motion", {"group": "", "index": 3}),
        )
        self.assertIsNone(movement_command(PetDecision(PetKind.STAY, "nothing_to_do")))


if __name__ == "__main__":
    unittest.main()
