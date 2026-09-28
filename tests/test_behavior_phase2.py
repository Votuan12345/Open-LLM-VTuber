"""Phase 2 behavior tests. Run from repo root: uv run python -m unittest tests.test_behavior_phase2 -v"""

import asyncio
import inspect
import json
import random
import sys
import unittest
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from src.open_llm_vtuber.config_manager import (
    BehaviorConfig,
    ContextConfig,
    IdleExpressionConfig,
    PermissionConfig,
    PetBrainConfig,
    ProactiveConfig,
    read_yaml,
    validate_config,
)
from loguru import logger

from src.open_llm_vtuber.pet_brain import BrainEvent, Mood, PetBrain
from src.open_llm_vtuber.pet_brain import context as context_module
from src.open_llm_vtuber.pet_brain.behavior import (
    BehaviorKind,
    BehaviorSelector,
    RETURNED_BONUS,
    SelectionInput,
    Trigger,
    effective_min_interval_s,
    willingness,
)
from src.open_llm_vtuber.pet_brain.eligibility import ClientEligibility
from src.open_llm_vtuber.pet_brain.emotion_manager import (
    EmotionManager,
    EmotionSource,
)
from src.open_llm_vtuber.pet_brain.proactive_prompt import build_context_block
from src.open_llm_vtuber.pet_brain.context import (
    ContextSnapshot,
    NullContextSensor,
    WindowsContextSensor,
    classify_process,
    idle_seconds_from_ticks,
)
from src.open_llm_vtuber.pet_brain.lifecycle import (
    ACTIVE_TO_IDLE_S,
    SLEEP_AT,
    SLEEP_USER_IDLE_S,
    SLEEPY_AT,
    Lifecycle,
    LifecycleInputs,
    LifecyclePhase,
)
from src.open_llm_vtuber.pet_brain.mood import MOOD_KEYS, MoodState
from src.open_llm_vtuber.pet_brain.pet_brain import ActivityState, BrainTickInputs
from src.open_llm_vtuber.pet_brain.presence import ClientPresence, prune_window
from src.open_llm_vtuber.pet_brain import scheduler as scheduler_module
from src.open_llm_vtuber.pet_brain.scheduler import BehaviorScheduler
from src.open_llm_vtuber.pet_brain.rhythm import (
    DayPart,
    day_part_at,
    split_by_day_part,
)
from src.open_llm_vtuber.service_context import ServiceContext


class _LoguruCapture:
    """Capture loguru records at/above `level`; mirrors tests/test_pet_brain.py."""

    def __init__(self, level="INFO"):
        self._level = level

    def __enter__(self):
        self.records = []
        self._id = logger.add(lambda m: self.records.append(str(m)), level=self._level)
        return self.records

    def __exit__(self, *exc):
        logger.remove(self._id)
        return False


class FakeMono:
    """A mutable monotonic clock, mirroring FakeClock in tests/test_pet_brain.py."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeWall:
    """A mutable wall clock returning a `datetime`."""

    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


class Phase2ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = PetBrainConfig()

        self.assertIsInstance(cfg.behavior, BehaviorConfig)
        self.assertTrue(cfg.behavior.enabled)

        self.assertIsInstance(cfg.proactive, ProactiveConfig)
        self.assertTrue(cfg.proactive.enabled)
        self.assertEqual(cfg.proactive.min_interval_min, 10.0)
        self.assertEqual(cfg.proactive.post_conversation_quiet_min, 3.0)
        self.assertEqual(cfg.proactive.max_per_hour, 3)
        self.assertEqual(cfg.proactive.threshold, 0.30)
        self.assertEqual(cfg.proactive.chance, 0.35)
        self.assertEqual(cfg.proactive.ignored_after_min, 5.0)
        self.assertEqual(cfg.proactive.max_backoff_min, 60.0)

        self.assertIsInstance(cfg.idle_expression, IdleExpressionConfig)
        self.assertTrue(cfg.idle_expression.enabled)
        self.assertEqual(cfg.idle_expression.min_interval_min, 3.0)
        self.assertEqual(cfg.idle_expression.max_per_hour, 10)
        self.assertEqual(cfg.idle_expression.chance, 0.3)

        self.assertIsInstance(cfg.context, ContextConfig)
        self.assertTrue(cfg.context.enabled)
        self.assertEqual(cfg.context.away_after_min, 10.0)
        self.assertEqual(cfg.context.process_categories["Code.exe"], "coding")

    def test_templates_validate_with_phase2_sections(self):
        for path in (
            "config_templates/conf.default.yaml",
            "config_templates/conf.ZH.default.yaml",
        ):
            config = validate_config(read_yaml(path))
            pb = config.character_config.pet_brain_config
            self.assertTrue(pb.behavior.enabled)
            self.assertEqual(pb.proactive.min_interval_min, 10)

    def test_missing_sections_use_defaults(self):
        cfg = PetBrainConfig.model_validate({"enabled": True})
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.behavior.enabled)
        self.assertTrue(cfg.proactive.enabled)
        self.assertTrue(cfg.idle_expression.enabled)
        self.assertTrue(cfg.context.enabled)

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValidationError):
            IdleExpressionConfig(chance=1.5)
        with self.assertRaises(ValidationError):
            ProactiveConfig(threshold=-0.1)
        with self.assertRaises(ValidationError):
            ContextConfig(process_categories={"Foo.exe": "work"})
        with self.assertRaises(ValidationError):
            ProactiveConfig(min_interval_min=10, max_backoff_min=5)

    def test_phase2_change_keeps_brain_and_agent(self):
        ctx = ServiceContext()
        cfg_a = PetBrainConfig(enabled=True)
        self.assertTrue(ctx.init_pet_brain(cfg_a))
        b = ctx.pet_brain
        self.assertIsNotNone(b)

        cfg_b = cfg_a.model_copy(deep=True)
        cfg_b.proactive.chance = 0.9

        self.assertFalse(ctx.init_pet_brain(cfg_b))
        self.assertIs(ctx.pet_brain, b)
        self.assertEqual(b.config.proactive.chance, 0.9)

    def test_phase1_change_still_replaces_brain(self):
        ctx = ServiceContext()
        cfg_a = PetBrainConfig(enabled=True)
        self.assertTrue(ctx.init_pet_brain(cfg_a))
        first = ctx.pet_brain

        cfg_b = PetBrainConfig(
            enabled=True, permission=PermissionConfig(tool_levels={"a": "read"})
        )
        self.assertTrue(ctx.init_pet_brain(cfg_b))
        self.assertIsNot(ctx.pet_brain, first)


class RhythmTests(unittest.TestCase):
    def _dt(self, hour, minute=0, day=1):
        return datetime(2024, 1, day, hour, minute)

    def test_day_part_at_boundaries(self):
        self.assertEqual(day_part_at(self._dt(4, 59)), DayPart.LATE_NIGHT)
        self.assertEqual(day_part_at(self._dt(5, 0)), DayPart.MORNING)
        self.assertEqual(day_part_at(self._dt(10, 59)), DayPart.MORNING)
        self.assertEqual(day_part_at(self._dt(11, 0)), DayPart.AFTERNOON)
        self.assertEqual(day_part_at(self._dt(17, 0)), DayPart.EVENING)
        self.assertEqual(day_part_at(self._dt(22, 0)), DayPart.LATE_NIGHT)
        self.assertEqual(day_part_at(self._dt(0, 30)), DayPart.LATE_NIGHT)

    def test_split_crossing_five_am(self):
        start = self._dt(4, 40)
        end = self._dt(5, 10)
        self.assertEqual(
            split_by_day_part(start, end),
            [(DayPart.LATE_NIGHT, 1200.0), (DayPart.MORNING, 600.0)],
        )

    def test_split_crossing_midnight_not_split(self):
        start = self._dt(21, 30)
        end = self._dt(2, 0, day=2)
        self.assertEqual(
            split_by_day_part(start, end),
            [(DayPart.EVENING, 1800.0), (DayPart.LATE_NIGHT, 14400.0)],
        )

    def test_thirty_hour_span_sums_and_never_repeats_adjacent(self):
        start = self._dt(3, 0)
        end = start + timedelta(hours=30)
        segments = split_by_day_part(start, end)
        self.assertAlmostEqual(sum(seconds for _, seconds in segments), 30 * 3600.0)
        for (part_a, _), (part_b, _) in zip(segments, segments[1:]):
            self.assertNotEqual(part_a, part_b)

    def test_split_empty_when_end_not_after_start(self):
        t = self._dt(9, 0)
        self.assertEqual(split_by_day_part(t, t), [])
        self.assertEqual(split_by_day_part(t, t - timedelta(seconds=1)), [])


class MoodTimelineTests(unittest.TestCase):
    def test_crossing_five_am_splits_rates(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 4, 40))
        mood = Mood(clock=mono, wall_clock=wall)
        start_sleepiness = mood._state.sleepiness
        start_energy = mood._state.energy

        mono.t += 1800
        wall.dt += timedelta(seconds=1800)
        mood.advance()

        expected_sleepiness = start_sleepiness + 0.02 * 0.5 + 0.15 * (20 / 60)
        expected_energy = (
            start_energy - 0.03 * 0.5 - 0.05 * (20 / 60) + 0.05 * (10 / 60)
        )
        self.assertAlmostEqual(mood._state.sleepiness, expected_sleepiness, places=6)
        self.assertAlmostEqual(mood._state.energy, expected_energy, places=6)

    def test_long_equals_many_short(self):
        start_wall = datetime(2024, 1, 1, 20, 0)

        mono_a = FakeMono()
        wall_a = FakeWall(start_wall)
        mood_a = Mood(clock=mono_a, wall_clock=wall_a)
        mono_a.t += 6 * 3600
        wall_a.dt += timedelta(hours=6)
        mood_a.advance()

        mono_b = FakeMono()
        wall_b = FakeWall(start_wall)
        mood_b = Mood(clock=mono_b, wall_clock=wall_b)
        for _ in range(72):
            mono_b.t += 300
            wall_b.dt += timedelta(seconds=300)
            mood_b.advance()

        for key in MOOD_KEYS:
            self.assertAlmostEqual(
                getattr(mood_a._state, key),
                getattr(mood_b._state, key),
                delta=0.01,
                msg=f"mismatch for {key}",
            )

    def test_forward_jump_skips_rhythm(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 23, 0))
        mood = Mood(clock=mono, wall_clock=wall)
        start_sleepiness = mood._state.sleepiness

        mono.t += 1800  # 30 min
        wall.dt += timedelta(hours=2, minutes=30)

        with _LoguruCapture("INFO") as records:
            mood.advance()
        self.assertTrue(any("wall-clock jump" in r for r in records))

        expected = start_sleepiness + 0.02 * 0.5
        self.assertAlmostEqual(mood._state.sleepiness, expected, places=6)

        before_second = mood._state.sleepiness
        mono.t += 600
        wall.dt += timedelta(minutes=10)
        mood.advance()
        expected_second = before_second + 0.02 * (10 / 60) + 0.15 * (10 / 60)
        self.assertAlmostEqual(mood._state.sleepiness, expected_second, places=6)

    def test_backward_jump_skips_rhythm(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 23, 0))
        mood = Mood(clock=mono, wall_clock=wall)
        start_sleepiness = mood._state.sleepiness

        mono.t += 600  # 10 min
        wall.dt -= timedelta(hours=1)

        with _LoguruCapture("INFO") as records:
            mood.advance()
        self.assertTrue(any("wall-clock jump" in r for r in records))

        expected = start_sleepiness + 0.02 * (10 / 60)
        self.assertAlmostEqual(mood._state.sleepiness, expected, places=6)

    def test_small_drift_is_continuous(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 23, 0))
        mood = Mood(clock=mono, wall_clock=wall)
        start_sleepiness = mood._state.sleepiness

        mono.t += 600  # 10 min
        wall.dt += timedelta(minutes=10, seconds=90)
        mood.advance()

        expected = start_sleepiness + 0.02 * (10 / 60) + 0.15 * (10 / 60)
        self.assertAlmostEqual(mood._state.sleepiness, expected, places=6)

    def test_focus_trend_only_when_flag(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 13, 0))
        mood = Mood(initial=MoodState(focus=0.8), clock=mono, wall_clock=wall)
        mono.t += 3600
        wall.dt += timedelta(hours=1)
        mood.advance()
        self.assertLess(mood._state.focus, 0.8)

        mono2 = FakeMono()
        wall2 = FakeWall(datetime(2024, 1, 1, 13, 0))
        mood2 = Mood(initial=MoodState(focus=0.3), clock=mono2, wall_clock=wall2)
        mood2.focus_context_active = True
        mono2.t += 3600
        wall2.dt += timedelta(hours=1)
        mood2.advance()
        self.assertGreater(mood2._state.focus, 0.3)

    def test_new_event_effects(self):
        mood = Mood(clock=FakeMono(), wall_clock=FakeWall(datetime(2024, 1, 1, 13, 0)))
        before_happiness = mood._state.happiness
        before_curiosity = mood._state.curiosity
        mood.apply_event(BrainEvent.USER_RETURNED)
        self.assertAlmostEqual(mood._state.happiness, before_happiness + 0.05, places=6)
        self.assertAlmostEqual(mood._state.curiosity, before_curiosity + 0.05, places=6)

        mood2 = Mood(clock=FakeMono(), wall_clock=FakeWall(datetime(2024, 1, 1, 13, 0)))
        before_social_need = mood2._state.social_need
        mood2.apply_event(BrainEvent.PROACTIVE_SPOKEN)
        self.assertAlmostEqual(
            mood2._state.social_need, before_social_need - 0.10, places=6
        )

        mood3 = Mood(clock=FakeMono(), wall_clock=FakeWall(datetime(2024, 1, 1, 13, 0)))
        before_happiness3 = mood3._state.happiness
        mood3.apply_event(BrainEvent.PROACTIVE_IGNORED)
        self.assertAlmostEqual(
            mood3._state.happiness, before_happiness3 - 0.03, places=6
        )

    def test_phase1_behavior_unchanged_in_afternoon(self):
        mono = FakeMono()
        wall = FakeWall(datetime(2024, 1, 1, 13, 0))
        mood = Mood(clock=mono, wall_clock=wall)
        start_boredom = mood._state.boredom
        start_social_need = mood._state.social_need

        mono.t += 3600
        wall.dt += timedelta(hours=1)
        mood.advance()

        self.assertAlmostEqual(mood._state.boredom, start_boredom + 0.15, places=6)
        self.assertAlmostEqual(
            mood._state.social_need, start_social_need + 0.10, places=6
        )


class ContextTests(unittest.TestCase):
    def test_idle_seconds_from_ticks_basic(self):
        self.assertEqual(idle_seconds_from_ticks(5000, 2000), 3.0)

    def test_idle_seconds_from_ticks_wraps(self):
        self.assertEqual(idle_seconds_from_ticks(1000, 0xFFFFFFFF - 999), 2.0)

    def test_classify_process(self):
        table = {"Code.exe": "coding", "Unity.exe": "unity"}
        self.assertEqual(classify_process("CODE.EXE", table), "coding")
        self.assertEqual(
            classify_process(r"C:\Program Files\Unity\Unity.exe", table), "unity"
        )
        self.assertEqual(classify_process("notepad.exe", table), "unknown")
        self.assertIsNone(classify_process(None, table))

    def test_per_field_failure_is_isolated(self):
        class _PartialFailSensor(WindowsContextSensor):
            def _read_idle_seconds(self) -> float:
                return 12.5

            def _read_process_name(self) -> str:
                raise RuntimeError("boom")

            def _read_fullscreen(self) -> bool:
                return True

        sensor = _PartialFailSensor(wall_clock=lambda: datetime(2024, 1, 1, 9, 0))
        with _LoguruCapture("DEBUG"):
            snap = sensor.sample()
        self.assertIsNone(snap.process_name)
        self.assertEqual(snap.user_idle_seconds, 12.5)
        self.assertTrue(snap.fullscreen)

    def test_idle_failure_is_none_not_zero(self):
        class _IdleFailSensor(WindowsContextSensor):
            def _read_idle_seconds(self) -> float:
                raise RuntimeError("boom")

            def _read_process_name(self) -> str:
                return "Code.exe"

            def _read_fullscreen(self) -> bool:
                return False

        sensor = _IdleFailSensor(wall_clock=lambda: datetime(2024, 1, 1, 9, 0))
        with _LoguruCapture("DEBUG"):
            snap = sensor.sample()
        self.assertIsNone(snap.user_idle_seconds)
        self.assertEqual(snap.process_name, "Code.exe")
        self.assertFalse(snap.fullscreen)

    def test_null_sensor_all_none(self):
        snap = NullContextSensor(wall_clock=lambda: datetime(2024, 1, 1)).sample()
        self.assertIsNone(snap.user_idle_seconds)
        self.assertIsNone(snap.process_name)
        self.assertIsNone(snap.fullscreen)

    def test_source_guard_no_window_text(self):
        self.assertNotIn("GetWindowText", inspect.getsource(context_module))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only smoke test")
    def test_windows_sensor_smoke(self):
        snap = WindowsContextSensor().sample()
        self.assertTrue(snap.user_idle_seconds is None or snap.user_idle_seconds >= 0)


class LifecycleEvaluateTests(unittest.TestCase):
    def _inputs(self, **overrides):
        base = dict(
            sleepiness=0.1,
            user_idle_seconds=5.0,
            quiet_seconds=0.0,
            conversation_running=False,
            away_after_seconds=600.0,
            user_returned=False,
        )
        base.update(overrides)
        return LifecycleInputs(**base)

    def test_rule1_user_returned_wakes_from_away(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.AWAY
        lc.evaluate(self._inputs(user_returned=True))
        self.assertEqual(lc.phase, LifecyclePhase.ACTIVE)

    def test_rule2_idle_over_threshold_goes_away(self):
        for phase in (
            LifecyclePhase.ACTIVE,
            LifecyclePhase.IDLE,
            LifecyclePhase.SLEEPY,
        ):
            lc = Lifecycle(clock=FakeMono())
            lc.phase = phase
            lc.evaluate(self._inputs(user_idle_seconds=600.0, away_after_seconds=600.0))
            self.assertEqual(lc.phase, LifecyclePhase.AWAY, phase)

    def test_rule2_does_not_fire_when_idle_unknown(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.ACTIVE
        lc.evaluate(self._inputs(user_idle_seconds=None, away_after_seconds=600.0))
        self.assertEqual(lc.phase, LifecyclePhase.ACTIVE)

    def test_rule3_quiet_goes_idle(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.ACTIVE
        lc.evaluate(self._inputs(quiet_seconds=ACTIVE_TO_IDLE_S))
        self.assertEqual(lc.phase, LifecyclePhase.IDLE)

    def test_rule3_blocked_by_running_conversation(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.ACTIVE
        lc.evaluate(
            self._inputs(quiet_seconds=ACTIVE_TO_IDLE_S, conversation_running=True)
        )
        self.assertEqual(lc.phase, LifecyclePhase.ACTIVE)

    def test_rule4_idle_to_sleepy(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.IDLE
        lc.evaluate(self._inputs(sleepiness=SLEEPY_AT))
        self.assertEqual(lc.phase, LifecyclePhase.SLEEPY)

    def test_rule5_sleepy_to_sleep(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.SLEEPY
        lc.evaluate(
            self._inputs(
                sleepiness=SLEEP_AT,
                user_idle_seconds=SLEEP_USER_IDLE_S,
                away_after_seconds=SLEEP_USER_IDLE_S + 1,
            )
        )
        self.assertEqual(lc.phase, LifecyclePhase.SLEEP)

    def test_rule5_requires_known_idle(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.SLEEPY
        lc.evaluate(self._inputs(sleepiness=SLEEP_AT, user_idle_seconds=None))
        self.assertEqual(lc.phase, LifecyclePhase.SLEEPY)

    def test_sleep_stays_sleep_with_user_returned(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.SLEEP
        lc.evaluate(self._inputs(sleepiness=0.9, user_returned=True))
        self.assertEqual(lc.phase, LifecyclePhase.SLEEP)

    def test_sleep_leaves_only_via_ensure_active(self):
        lc = Lifecycle(clock=FakeMono())
        lc.phase = LifecyclePhase.SLEEP
        lc.ensure_active("user_input")
        self.assertEqual(lc.phase, LifecyclePhase.ACTIVE)


class BrainTickTests(unittest.TestCase):
    def _brain(self, wall_dt, config=None):
        from src.open_llm_vtuber.config_manager import PetBrainConfig

        mono = FakeMono()
        wall = FakeWall(wall_dt)
        brain = PetBrain(
            config or PetBrainConfig(enabled=True), clock=mono, wall_clock=wall
        )
        return brain, mono, wall

    def _ctx(self, wall, idle):
        return ContextSnapshot(
            taken_at_wall=wall.dt,
            user_idle_seconds=idle,
            process_name=None,
            fullscreen=None,
        )

    def test_user_returned_applies_event_and_wakes_from_away(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0))
        brain.lifecycle.phase = LifecyclePhase.AWAY
        before_happiness = brain.mood._state.happiness
        before_curiosity = brain.mood._state.curiosity

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 5.0),
                category=None,
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=True,
            )
        )

        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.ACTIVE)
        self.assertAlmostEqual(
            brain.mood._state.happiness, before_happiness + 0.05, places=6
        )
        self.assertAlmostEqual(
            brain.mood._state.curiosity, before_curiosity + 0.05, places=6
        )

    def test_user_returned_stays_away_when_not_currently_away(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0))
        brain.lifecycle.phase = LifecyclePhase.ACTIVE

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 5.0),
                category=None,
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=True,
            )
        )

        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.ACTIVE)

    def test_running_conversation_blocks_active_to_idle(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0))
        brain.lifecycle.phase = LifecyclePhase.ACTIVE

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 5.0),
                category=None,
                quiet_seconds=ACTIVE_TO_IDLE_S,
                conversation_running=True,
                user_returned=False,
            )
        )

        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.ACTIVE)

    def test_tick_uses_mood_updated_this_tick(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 23, 0))
        brain.lifecycle.phase = LifecyclePhase.IDLE
        brain.mood._state.sleepiness = 0.69

        mono.t += 3600
        wall.dt += timedelta(hours=1)

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 5.0),
                category=None,
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=False,
            )
        )

        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.SLEEPY)

    def test_tick_sets_focus_flag_after_advance(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0))
        focus_before = brain.mood._state.focus

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 10.0),
                category="coding",
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=False,
            )
        )

        self.assertTrue(brain.mood.focus_context_active)
        focus_after_first_tick = brain.mood._state.focus
        self.assertAlmostEqual(focus_after_first_tick, focus_before, places=6)

        mono.t += 3600
        wall.dt += timedelta(hours=1)

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 10.0),
                category="coding",
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=False,
            )
        )

        self.assertGreater(brain.mood._state.focus, focus_after_first_tick)

    def test_focus_flag_false_when_idle_too_high(self):
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0))

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 200.0),
                category="coding",
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=False,
            )
        )

        self.assertFalse(brain.mood.focus_context_active)

    def test_away_after_seconds_uses_config(self):
        from src.open_llm_vtuber.config_manager import ContextConfig, PetBrainConfig

        config = PetBrainConfig(enabled=True, context=ContextConfig(away_after_min=1.0))
        brain, mono, wall = self._brain(datetime(2024, 1, 1, 13, 0), config=config)
        brain.lifecycle.phase = LifecyclePhase.ACTIVE

        brain.tick(
            BrainTickInputs(
                context=self._ctx(wall, 60.0),
                category=None,
                quiet_seconds=0.0,
                conversation_running=False,
                user_returned=False,
            )
        )

        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.AWAY)


class _FixedRandom:
    """Duck-typed stand-in for random.Random that always returns `value`."""

    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


BASELINE_WALL = datetime(2024, 1, 1, 14, 0)
BASELINE_MOOD = {
    "social_need": 0.9,
    "boredom": 0.9,
    "curiosity": 0.5,
    "sleepiness": 0.0,
    "energy": 1.0,
    "happiness": 0.5,
}


def make_input(**overrides):
    presence = overrides.pop(
        "presence",
        ClientPresence(uid="u1", connected_at=0.0),
    )
    context = overrides.pop(
        "context",
        ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=600.0,
            process_name=None,
            fullscreen=False,
        ),
    )
    mood = overrides.pop("mood", dict(BASELINE_MOOD))
    if "config" in overrides:
        config = overrides.pop("config")
    else:
        config = PetBrainConfig(enabled=True)
        # Isolate the proactive-stage gates by default; idle-stage tests opt
        # back in explicitly via `config=`.
        config.idle_expression.enabled = False

    base = dict(
        trigger=Trigger.TICK,
        now=1_000_000.0,
        presence=presence,
        activity=ActivityState.IDLE,
        lifecycle=LifecyclePhase.ACTIVE,
        mood=mood,
        context=context,
        category="browser",
        config=config,
        emo_map={},
    )
    base.update(overrides)
    return SelectionInput(**base)


class SelectorTests(unittest.TestCase):
    def _selector(self, rng_value=0.0):
        return BehaviorSelector(rng=_FixedRandom(rng_value))

    def test_baseline_proactive_speak(self):
        decision = self._selector().select(make_input())
        self.assertEqual(decision.kind, BehaviorKind.PROACTIVE_SPEAK)

    # --- Vetoes -----------------------------------------------------

    def test_veto_conversation_lock(self):
        decision = self._selector().select(make_input(activity=ActivityState.TALKING))
        self.assertEqual(decision.kind, BehaviorKind.DO_NOTHING)
        self.assertEqual(decision.reason, "conversation_lock")

    def test_veto_lifecycle_sleep(self):
        decision = self._selector().select(make_input(lifecycle=LifecyclePhase.SLEEP))
        self.assertEqual(decision.reason, "lifecycle_sleep")

    def test_veto_lifecycle_away(self):
        decision = self._selector().select(make_input(lifecycle=LifecyclePhase.AWAY))
        self.assertEqual(decision.reason, "lifecycle_away")

    def test_veto_fullscreen_true(self):
        ctx = ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=600.0,
            process_name=None,
            fullscreen=True,
        )
        decision = self._selector().select(make_input(context=ctx))
        self.assertEqual(decision.reason, "fullscreen")

    def test_veto_fullscreen_unknown(self):
        ctx = ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=600.0,
            process_name=None,
            fullscreen=None,
        )
        decision = self._selector().select(make_input(context=ctx))
        self.assertEqual(decision.reason, "context_unknown")

    # --- Proactive gates ---------------------------------------------

    def test_proactive_disabled(self):
        cfg = PetBrainConfig(enabled=True)
        cfg.proactive.enabled = False
        cfg.idle_expression.enabled = False
        decision = self._selector().select(make_input(config=cfg))
        self.assertEqual(decision.reason, "proactive_disabled")

    def test_context_unknown_idle_none(self):
        ctx = ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=None,
            process_name=None,
            fullscreen=False,
        )
        decision = self._selector().select(make_input(context=ctx))
        self.assertEqual(decision.reason, "context_unknown")

    def test_context_unknown_category_none(self):
        decision = self._selector().select(make_input(category=None))
        self.assertEqual(decision.reason, "context_unknown")

    def test_cooldown_at_9_minutes(self):
        presence = ClientPresence(
            uid="u1", connected_at=0.0, last_proactive=1_000_000.0 - 9 * 60
        )
        decision = self._selector().select(make_input(presence=presence))
        self.assertEqual(decision.reason, "cooldown")

    def test_cooldown_allowed_at_10_minutes(self):
        presence = ClientPresence(
            uid="u1", connected_at=0.0, last_proactive=1_000_000.0 - 10 * 60
        )
        decision = self._selector().select(make_input(presence=presence))
        self.assertEqual(decision.kind, BehaviorKind.PROACTIVE_SPEAK)

    def test_post_conversation_quiet(self):
        presence = ClientPresence(
            uid="u1",
            connected_at=0.0,
            last_conversation_end=1_000_000.0 - 60,
        )
        decision = self._selector().select(make_input(presence=presence))
        self.assertEqual(decision.reason, "post_conversation_quiet")

    def test_post_conversation_quiet_uses_latest_of_both_fields(self):
        presence = ClientPresence(
            uid="u1",
            connected_at=0.0,
            last_conversation_end=1_000_000.0 - 1000,
            last_user_interaction=1_000_000.0 - 60,
        )
        decision = self._selector().select(make_input(presence=presence))
        self.assertEqual(decision.reason, "post_conversation_quiet")

    def test_hourly_cap_with_3_inside_60_min(self):
        now = 1_000_000.0
        ts = deque([now - 10 * 60, now - 20 * 60, now - 30 * 60])
        presence = ClientPresence(uid="u1", connected_at=0.0, proactive_timestamps=ts)
        decision = self._selector().select(make_input(presence=presence, now=now))
        self.assertEqual(decision.reason, "hourly_cap")

    def test_hourly_cap_allowed_when_one_is_61_minutes_old(self):
        now = 1_000_000.0
        ts = deque([now - 61 * 60, now - 20 * 60, now - 30 * 60])
        presence = ClientPresence(uid="u1", connected_at=0.0, proactive_timestamps=ts)
        decision = self._selector().select(make_input(presence=presence, now=now))
        self.assertEqual(decision.kind, BehaviorKind.PROACTIVE_SPEAK)

    def test_hourly_cap_check_does_not_mutate_presence(self):
        now = 1_000_000.0
        ts = deque([now - 61 * 60, now - 20 * 60, now - 30 * 60])
        presence = ClientPresence(uid="u1", connected_at=0.0, proactive_timestamps=ts)
        self._selector().select(make_input(presence=presence, now=now))
        self.assertEqual(len(presence.proactive_timestamps), 3)

    def test_user_busy_coding_idle_60_refused(self):
        ctx = ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=60.0,
            process_name=None,
            fullscreen=False,
        )
        decision = self._selector().select(make_input(context=ctx, category="coding"))
        self.assertEqual(decision.reason, "user_busy")

    def test_user_busy_coding_idle_130_allowed(self):
        ctx = ContextSnapshot(
            taken_at_wall=BASELINE_WALL,
            user_idle_seconds=130.0,
            process_name=None,
            fullscreen=False,
        )
        decision = self._selector().select(
            make_input(
                context=ctx,
                category="coding",
                mood={
                    "social_need": 1.0,
                    "boredom": 1.0,
                    "curiosity": 1.0,
                    "sleepiness": 0.0,
                    "energy": 1.0,
                    "happiness": 0.5,
                },
            )
        )
        self.assertEqual(decision.kind, BehaviorKind.PROACTIVE_SPEAK)

    def test_low_willingness(self):
        decision = self._selector().select(make_input(category="gaming"))
        self.assertEqual(decision.reason, "low_willingness")
        self.assertEqual(decision.willingness, 0.0)

    def test_chance_rejected(self):
        decision = self._selector(rng_value=0.99).select(make_input())
        self.assertEqual(decision.reason, "chance")
        self.assertIsNotNone(decision.willingness)

    # --- effective_min_interval_s ------------------------------------

    def test_effective_min_interval_s_backoff(self):
        cfg = PetBrainConfig(enabled=True).proactive
        self.assertEqual(effective_min_interval_s(cfg, 0), 600.0)
        self.assertEqual(effective_min_interval_s(cfg, 1), 1200.0)
        self.assertEqual(effective_min_interval_s(cfg, 2), 2400.0)
        self.assertEqual(effective_min_interval_s(cfg, 3), 3600.0)
        self.assertEqual(effective_min_interval_s(cfg, 5), 3600.0)

    # --- willingness ---------------------------------------------------

    def test_willingness_default_mood_browser_afternoon(self):
        default_mood = {
            "happiness": 0.6,
            "energy": 0.7,
            "curiosity": 0.5,
            "boredom": 0.2,
            "social_need": 0.4,
            "focus": 0.3,
            "sleepiness": 0.2,
        }
        w = willingness(default_mood, "browser", DayPart.AFTERNOON, False)
        self.assertAlmostEqual(w, 0.275, places=3)

    def test_willingness_after_30_minutes(self):
        mood = {
            "social_need": 0.45,
            "boredom": 0.275,
            "curiosity": 0.5,
            "sleepiness": 0.21,
            "energy": 0.685,
        }
        w = willingness(mood, "browser", DayPart.AFTERNOON, False)
        self.assertAlmostEqual(w, 0.307, places=2)

    def test_willingness_gaming_is_zero(self):
        w = willingness(BASELINE_MOOD, "gaming", DayPart.AFTERNOON, False)
        self.assertEqual(w, 0.0)

    def test_willingness_late_night_halves_value(self):
        w_afternoon = willingness(BASELINE_MOOD, "browser", DayPart.AFTERNOON, False)
        w_late_night = willingness(BASELINE_MOOD, "browser", DayPart.LATE_NIGHT, False)
        self.assertAlmostEqual(w_late_night, w_afternoon * 0.5, places=6)

    def test_willingness_returned_bonus_adds(self):
        w = willingness(BASELINE_MOOD, "browser", DayPart.AFTERNOON, False)
        w_bonus = willingness(BASELINE_MOOD, "browser", DayPart.AFTERNOON, True)
        self.assertAlmostEqual(w_bonus, w + RETURNED_BONUS, places=6)

    # --- REQUEST never produces IDLE_EXPRESSION -----------------------

    def test_request_refused_proactive_never_idle_expression(self):
        cfg = PetBrainConfig(enabled=True)
        cfg.idle_expression.enabled = True
        decision = self._selector(rng_value=0.0).select(
            make_input(
                trigger=Trigger.REQUEST,
                category="gaming",
                config=cfg,
                emo_map={"neutral": 0},
            )
        )
        self.assertEqual(decision.kind, BehaviorKind.DO_NOTHING)
        self.assertNotEqual(decision.kind, BehaviorKind.IDLE_EXPRESSION)

    # --- Idle expression ------------------------------------------------

    def _idle_ready_input(self, **overrides):
        """A TICK input where the proactive stage refuses (low willingness)
        but the idle-expression stage is otherwise eligible."""

        overrides.setdefault("category", "gaming")  # forces low_willingness
        overrides.setdefault("trigger", Trigger.TICK)
        if "config" not in overrides:
            cfg = PetBrainConfig(enabled=True)
            cfg.idle_expression.enabled = True
            overrides["config"] = cfg
        return make_input(**overrides)

    def test_idle_expression_sleepy(self):
        mood = dict(BASELINE_MOOD)
        mood["sleepiness"] = 0.7
        decision = self._selector(rng_value=0.0).select(
            self._idle_ready_input(mood=mood, emo_map={"sleepy": 5, "neutral": 0})
        )
        self.assertEqual(decision.kind, BehaviorKind.IDLE_EXPRESSION)
        self.assertEqual(decision.emotion, "sleepy")

    def test_idle_expression_falls_back_to_neutral(self):
        mood = dict(BASELINE_MOOD)
        mood["sleepiness"] = 0.7
        decision = self._selector(rng_value=0.0).select(
            self._idle_ready_input(mood=mood, emo_map={"neutral": 0})
        )
        self.assertEqual(decision.kind, BehaviorKind.IDLE_EXPRESSION)
        self.assertEqual(decision.emotion, "neutral")

    def test_idle_expression_no_expression_available(self):
        mood = dict(BASELINE_MOOD)
        mood["sleepiness"] = 0.7
        decision = self._selector(rng_value=0.0).select(
            self._idle_ready_input(mood=mood, emo_map={"joy": 3})
        )
        self.assertEqual(decision.kind, BehaviorKind.DO_NOTHING)
        self.assertEqual(decision.reason, "no_expression_available")

    def test_idle_expression_empty_emo_map(self):
        mood = dict(BASELINE_MOOD)
        mood["sleepiness"] = 0.7
        decision = self._selector(rng_value=0.0).select(
            self._idle_ready_input(mood=mood, emo_map={})
        )
        self.assertEqual(decision.kind, BehaviorKind.DO_NOTHING)
        self.assertEqual(decision.reason, "no_expression_available")

    # --- Deterministic rng ----------------------------------------------

    def test_deterministic_rng_same_sequence(self):
        idle_cfg = PetBrainConfig(enabled=True)
        idle_cfg.idle_expression.enabled = True
        inputs = [
            make_input(),
            make_input(category="gaming"),
            make_input(
                mood={**BASELINE_MOOD, "sleepiness": 0.7},
                category="gaming",
                emo_map={"sleepy": 5, "neutral": 0},
                config=idle_cfg,
            ),
        ]

        selector_a = BehaviorSelector(rng=random.Random(1))
        selector_b = BehaviorSelector(rng=random.Random(1))

        results_a = [selector_a.select(inp) for inp in inputs]
        results_b = [selector_b.select(inp) for inp in inputs]

        self.assertEqual(
            [(d.kind, d.reason, d.emotion) for d in results_a],
            [(d.kind, d.reason, d.emotion) for d in results_b],
        )


def make_brain(behavior_enabled: bool = True):
    cfg = PetBrainConfig(enabled=True)
    cfg.behavior.enabled = behavior_enabled
    return SimpleNamespace(config=cfg)


def make_presence(
    last_user_interaction=None,
    reserved=False,
    user_turn_pending=False,
):
    return ClientPresence(
        uid="u",
        connected_at=0.0,
        last_user_interaction=last_user_interaction,
        reserved=reserved,
        user_turn_pending=user_turn_pending,
    )


class _FakeTask:
    def __init__(self, is_done: bool):
        self._done = is_done

    def done(self) -> bool:
        return self._done


class ClientEligibilityTests(unittest.TestCase):
    def _make(
        self,
        connections=None,
        contexts=None,
        tasks=None,
        groups=None,
    ):
        group_map = groups or {}

        class _GroupManager:
            def get_client_group(self, uid):
                return group_map.get(uid)

        return ClientEligibility(
            client_connections=connections
            if connections is not None
            else {"a": object()},
            client_contexts=contexts
            if contexts is not None
            else {"a": SimpleNamespace(pet_brain=make_brain())},
            current_conversation_tasks=tasks if tasks is not None else {},
            chat_group_manager=_GroupManager(),
        )

    def test_brain_for_none_when_no_context(self):
        elig = self._make(contexts={})
        self.assertIsNone(elig.brain_for("a"))

    def test_brain_for_none_when_brain_missing(self):
        elig = self._make(contexts={"a": SimpleNamespace(pet_brain=None)})
        self.assertIsNone(elig.brain_for("a"))

    def test_brain_for_none_when_behavior_disabled(self):
        elig = self._make(
            contexts={
                "a": SimpleNamespace(pet_brain=make_brain(behavior_enabled=False))
            }
        )
        self.assertIsNone(elig.brain_for("a"))

    def test_brain_for_returns_brain_when_enabled(self):
        brain = make_brain()
        elig = self._make(contexts={"a": SimpleNamespace(pet_brain=brain)})
        self.assertIs(elig.brain_for("a"), brain)

    def test_in_group_false_when_no_group(self):
        elig = self._make(groups={})
        self.assertFalse(elig.in_group("a"))

    def test_in_group_false_when_solo_group(self):
        elig = self._make(groups={"a": SimpleNamespace(members={"a"})})
        self.assertFalse(elig.in_group("a"))

    def test_in_group_true_when_multi_member(self):
        elig = self._make(groups={"a": SimpleNamespace(members={"a", "b"})})
        self.assertTrue(elig.in_group("a"))

    def test_handles_true_when_all_conditions_met(self):
        elig = self._make()
        self.assertTrue(elig.handles("a"))

    def test_handles_false_when_not_connected(self):
        elig = self._make(connections={})
        self.assertFalse(elig.handles("a"))

    def test_handles_false_when_in_group(self):
        elig = self._make(groups={"a": SimpleNamespace(members={"a", "b"})})
        self.assertFalse(elig.handles("a"))

    # --- check() reasons, in order ---------------------------------------

    def test_check_not_connected(self):
        elig = self._make(connections={})
        self.assertEqual(elig.check("a", None), "not_connected")

    def test_check_not_handled_missing_context(self):
        elig = self._make(contexts={})
        self.assertEqual(elig.check("a", None), "not_handled")

    def test_check_not_handled_brain_none(self):
        elig = self._make(contexts={"a": SimpleNamespace(pet_brain=None)})
        self.assertEqual(elig.check("a", None), "not_handled")

    def test_check_not_handled_behavior_disabled(self):
        elig = self._make(
            contexts={
                "a": SimpleNamespace(pet_brain=make_brain(behavior_enabled=False))
            }
        )
        self.assertEqual(elig.check("a", None), "not_handled")

    def test_check_in_group(self):
        elig = self._make(groups={"a": SimpleNamespace(members={"a", "b"})})
        self.assertEqual(elig.check("a", None), "in_group")

    def test_check_reserved(self):
        elig = self._make()
        self.assertEqual(elig.check("a", make_presence(reserved=True)), "reserved")

    def test_check_user_turn_pending(self):
        elig = self._make()
        self.assertEqual(
            elig.check("a", make_presence(user_turn_pending=True)), "user_turn_pending"
        )

    def test_check_conversation_running(self):
        elig = self._make(tasks={"a": _FakeTask(is_done=False)})
        self.assertEqual(elig.check("a", make_presence()), "conversation_running")

    def test_check_done_task_is_eligible(self):
        elig = self._make(tasks={"a": _FakeTask(is_done=True)})
        self.assertIsNone(elig.check("a", make_presence()))

    def test_check_none_task_entry_is_eligible(self):
        elig = self._make(tasks={"a": None})
        self.assertIsNone(elig.check("a", make_presence()))

    def test_check_none_presence_is_eligible(self):
        elig = self._make()
        self.assertIsNone(elig.check("a", None))

    def test_check_none_when_eligible(self):
        elig = self._make()
        self.assertIsNone(elig.check("a", make_presence(last_user_interaction=5.0)))

    # --- order() ----------------------------------------------------------

    def test_order_most_recent_interaction_first(self):
        elig = self._make()
        presences = {
            "a": make_presence(last_user_interaction=10.0),
            "b": make_presence(last_user_interaction=50.0),
            "c": make_presence(last_user_interaction=None),
        }
        self.assertEqual(elig.order(["a", "b", "c"], presences), ["b", "a", "c"])

    def test_order_ties_broken_by_uid(self):
        elig = self._make()
        presences = {
            "b": make_presence(last_user_interaction=10.0),
            "a": make_presence(last_user_interaction=10.0),
        }
        self.assertEqual(elig.order(["b", "a"], presences), ["a", "b"])

    def test_order_none_always_last(self):
        elig = self._make()
        presences = {
            "a": make_presence(last_user_interaction=None),
            "b": make_presence(last_user_interaction=None),
        }
        self.assertEqual(elig.order(["a", "b"], presences), ["a", "b"])


class ProactiveContextBlockTests(unittest.TestCase):
    def test_full_block_contains_expected_lines(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={"happiness": 0.7, "energy": 0.65, "boredom": 0.1},
            category="coding",
            process_name="Code.exe",
            user_idle_seconds=180.0,
            returned_after_s=600.0,
            trigger=Trigger.TICK,
        )
        self.assertIn("14:05 (afternoon)", block)
        self.assertIn("coding (Code.exe), idle 3 min", block)
        self.assertIn("came back after 10 min away", block)
        self.assertIn("you chose to", block)
        self.assertIn("cheerful", block)

    def test_request_trigger_wording(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={},
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.REQUEST,
        )
        self.assertIn("the app asked you to", block)

    def test_no_user_activity_line_when_unknown(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={},
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.TICK,
        )
        self.assertNotIn("User activity", block)

    def test_no_returned_line_when_none(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={},
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.TICK,
        )
        self.assertNotIn("came back", block)

    def test_traits_calm_when_none_qualify(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={"happiness": 0.1},
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.TICK,
        )
        self.assertIn("feeling calm", block)

    def test_traits_ordered_by_value_descending(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={"boredom": 0.65, "curiosity": 0.9, "social_need": 0.75},
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.TICK,
        )
        idx_curious = block.index("curious")
        idx_chatting = block.index("like chatting")
        idx_bored = block.index("bored")
        self.assertLess(idx_curious, idx_chatting)
        self.assertLess(idx_chatting, idx_bored)

    def test_traits_capped_at_three(self):
        block = build_context_block(
            now_wall=datetime(2024, 1, 1, 14, 5),
            day_part=DayPart.AFTERNOON,
            lifecycle=LifecyclePhase.ACTIVE,
            mood={
                "happiness": 0.95,
                "energy": 0.9,
                "curiosity": 0.85,
                "social_need": 0.8,
            },
            category=None,
            process_name=None,
            user_idle_seconds=None,
            returned_after_s=None,
            trigger=Trigger.TICK,
        )
        self.assertNotIn("like chatting", block)

    def test_no_window_or_title_substring_anywhere(self):
        cases = [
            dict(
                now_wall=datetime(2024, 1, 1, 14, 5),
                day_part=DayPart.LATE_NIGHT,
                lifecycle=LifecyclePhase.SLEEPY,
                mood={"sleepiness": 0.9},
                category="gaming",
                process_name="game.exe",
                user_idle_seconds=5.0,
                returned_after_s=30.0,
                trigger=Trigger.REQUEST,
            ),
            dict(
                now_wall=datetime(2024, 6, 15, 9, 30),
                day_part=DayPart.MORNING,
                lifecycle=LifecyclePhase.ACTIVE,
                mood={},
                category=None,
                process_name=None,
                user_idle_seconds=None,
                returned_after_s=None,
                trigger=Trigger.TICK,
            ),
        ]
        for kwargs in cases:
            block = build_context_block(**kwargs)
            self.assertNotIn("window", block.lower())
            self.assertNotIn("title", block.lower())


class FakeSensor:
    """Returns a fixed snapshot and counts `sample()` calls."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.snapshot


class FakeWS:
    def __init__(self):
        self.messages = []

    async def send_text(self, message):
        self.messages.append(message)


class _NoGroupManager:
    def get_client_group(self, uid):
        return None


async def _noop_proactive_turn(context, websocket_send, client_uid, context_block):
    return None


_SCHED_WALL = datetime(2024, 6, 15, 10, 0)


def _snapshot(idle=5.0, process="Code.exe", fullscreen=False):
    return ContextSnapshot(
        taken_at_wall=_SCHED_WALL,
        user_idle_seconds=idle,
        process_name=process,
        fullscreen=fullscreen,
    )


def make_real_brain(mono, context_enabled=True, away_after_min=10.0):
    cfg = PetBrainConfig(enabled=True)
    cfg.context.enabled = context_enabled
    cfg.context.away_after_min = away_after_min
    return PetBrain(cfg, clock=mono, wall_clock=FakeWall(_SCHED_WALL))


def wrap_tick(brain):
    """Record every BrainTickInputs passed to `brain.tick`, still calling it."""

    calls = []
    original = brain.tick

    def _tick(inputs):
        calls.append(inputs)
        original(inputs)

    brain.tick = _tick
    return calls


def make_scheduler(
    uids=("a",),
    brains=None,
    sensor=None,
    clock=None,
    tick_seconds=20.0,
    cls=None,
):
    clock = clock or FakeMono()
    if brains is None:
        shared = make_real_brain(clock)
        brains = {uid: shared for uid in uids}
    connections = {uid: FakeWS() for uid in uids}
    contexts = {
        uid: SimpleNamespace(
            pet_brain=brains[uid],
            live2d_model=SimpleNamespace(emo_map={"joy": 3, "neutral": 0}),
        )
        for uid in uids
    }
    tasks = {}
    cls = cls or BehaviorScheduler
    sched = cls(
        connections,
        contexts,
        tasks,
        _NoGroupManager(),
        run_proactive_turn=_noop_proactive_turn,
        sensor=sensor or FakeSensor(_snapshot()),
        tick_seconds=tick_seconds,
        clock=clock,
        rng=random.Random(0),
    )
    return sched, connections, contexts, tasks


class PruneWindowTests(unittest.TestCase):
    def test_drops_old_keeps_new(self):
        ts = deque([100.0, 200.0, 3000.0, 3900.0])
        prune_window(ts, now=4000.0, window_s=3600.0)
        self.assertEqual(list(ts), [3000.0, 3900.0])

    def test_empty_is_noop(self):
        ts = deque()
        prune_window(ts, now=10.0)
        self.assertEqual(list(ts), [])


class SchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_connect_creates_single_task_and_last_disconnect_stops(self):
        sched, *_ = make_scheduler(uids=("a", "b"))
        self.assertFalse(sched.is_running)
        await sched.client_connected("a")
        self.assertTrue(sched.is_running)
        first = sched._task
        await sched.client_connected("b")
        self.assertIs(sched._task, first)
        await sched.client_disconnected("a")
        self.assertTrue(sched.is_running)
        self.assertIs(sched._task, first)
        await sched.client_disconnected("b")
        self.assertFalse(sched.is_running)
        self.assertEqual(sched.presences, {})
        self.assertTrue(first.done())

    async def test_disconnect_resets_scheduler_state(self):
        sched, *_ = make_scheduler()
        await sched.client_connected("a")
        await sched.tick_once()
        self.assertIsNotNone(sched.state.last_context)
        await sched.client_disconnected("a")
        self.assertIsNone(sched.state.last_context)
        self.assertIsNone(sched.state.previous_user_idle_seconds)

    async def test_interleaved_connects_disconnects_never_two_loops(self):
        live = [0]
        peak = [0]

        class Counting(BehaviorScheduler):
            async def _run(self):
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                try:
                    await super()._run()
                finally:
                    live[0] -= 1

        uids = tuple(f"u{i}" for i in range(5))
        sched, *_ = make_scheduler(uids=uids, cls=Counting, tick_seconds=0.001)
        ops = []
        for uid in uids:
            ops.append(sched.client_connected(uid))
            ops.append(sched.client_disconnected(uid))
        await asyncio.gather(*ops)
        for _ in range(5):
            await asyncio.sleep(0.001)
        self.assertLessEqual(peak[0], 1)
        self.assertEqual(sched.presences, {})
        self.assertFalse(sched.is_running)
        self.assertEqual(live[0], 0)

    async def test_brain_identity_unchanged_across_stop_start(self):
        sched, _, contexts, _ = make_scheduler()
        brain = contexts["a"].pet_brain
        await sched.client_connected("a")
        await sched.client_disconnected("a")
        await sched.client_connected("a")
        self.assertIs(contexts["a"].pet_brain, brain)
        self.assertIs(sched.eligibility.brain_for("a"), brain)
        await sched.client_disconnected("a")

    async def test_tick_exception_does_not_kill_loop(self):
        sched, *_ = make_scheduler(tick_seconds=0.001)
        calls = [0]

        async def flaky():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("boom")

        sched.tick_once = flaky
        # Patch `logger.exception` so the expected traceback stays out of the
        # test output while still asserting the log call.
        with mock.patch.object(scheduler_module.logger, "exception") as log_exc:
            await sched.client_connected("a")
            for _ in range(200):
                if calls[0] >= 3:
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(sched.is_running)
            await sched.client_disconnected("a")
        self.assertGreaterEqual(calls[0], 3)
        log_exc.assert_called_once_with("[Behavior] tick failed")


class TickPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_handled_clients_skips_sampling(self):
        mono = FakeMono()
        brain = make_real_brain(mono)
        brain.config.behavior.enabled = False
        sensor = FakeSensor(_snapshot())
        sched, *_ = make_scheduler(brains={"a": brain}, sensor=sensor, clock=mono)
        sched.presences["a"] = ClientPresence(uid="a", connected_at=mono())
        await sched.tick_once()
        self.assertEqual(sensor.calls, 0)
        self.assertIsNone(sched.state.last_context)

    async def test_no_presences_skips_sampling(self):
        sensor = FakeSensor(_snapshot())
        sched, *_ = make_scheduler(sensor=sensor)
        await sched.tick_once()
        self.assertEqual(sensor.calls, 0)

    async def test_shared_brain_ticked_once(self):
        mono = FakeMono()
        brain = make_real_brain(mono)
        calls = wrap_tick(brain)
        sensor = FakeSensor(_snapshot())
        sched, *_ = make_scheduler(
            uids=("a", "b"),
            brains={"a": brain, "b": brain},
            sensor=sensor,
            clock=mono,
        )
        for uid in ("a", "b"):
            sched.presences[uid] = ClientPresence(uid=uid, connected_at=mono())
        await sched.tick_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(sensor.calls, 1)
        self.assertIs(sched.state.last_context, sensor.snapshot)
        self.assertEqual(calls[0].category, "coding")

    async def test_distinct_brains_each_ticked_once(self):
        mono = FakeMono()
        brain_a = make_real_brain(mono)
        brain_b = make_real_brain(mono)
        calls_a = wrap_tick(brain_a)
        calls_b = wrap_tick(brain_b)
        sched, *_ = make_scheduler(
            uids=("a", "b"), brains={"a": brain_a, "b": brain_b}, clock=mono
        )
        for uid in ("a", "b"):
            sched.presences[uid] = ClientPresence(uid=uid, connected_at=mono())
        await sched.tick_once()
        self.assertEqual((len(calls_a), len(calls_b)), (1, 1))

    async def test_user_returned_applied_and_bonus_pending(self):
        mono = FakeMono()
        brain = make_real_brain(mono, away_after_min=10.0)
        calls = wrap_tick(brain)
        sched, *_ = make_scheduler(
            brains={"a": brain}, sensor=FakeSensor(_snapshot(idle=5.0)), clock=mono
        )
        sched.presences["a"] = ClientPresence(uid="a", connected_at=mono())
        sched.state.previous_user_idle_seconds = 700.0
        happiness_before = brain.mood.snapshot()["happiness"]
        await sched.tick_once()
        self.assertTrue(calls[0].user_returned)
        self.assertGreater(brain.mood.snapshot()["happiness"], happiness_before)
        presence = sched.presences["a"]
        self.assertTrue(presence.returned_bonus_pending)
        self.assertEqual(presence.returned_after_s, 700.0)
        self.assertEqual(sched.state.previous_user_idle_seconds, 5.0)

    async def test_user_returned_not_when_prev_idle_unknown(self):
        mono = FakeMono()
        brain = make_real_brain(mono)
        calls = wrap_tick(brain)
        sched, *_ = make_scheduler(
            brains={"a": brain}, sensor=FakeSensor(_snapshot(idle=5.0)), clock=mono
        )
        sched.presences["a"] = ClientPresence(uid="a", connected_at=mono())
        sched.state.previous_user_idle_seconds = None
        await sched.tick_once()
        self.assertFalse(calls[0].user_returned)
        self.assertFalse(sched.presences["a"].returned_bonus_pending)

    async def test_context_disabled_brain_gets_unknown_snapshot(self):
        mono = FakeMono()
        brain = make_real_brain(mono, context_enabled=False)
        calls = wrap_tick(brain)
        sensor = FakeSensor(_snapshot(idle=5.0))
        sched, *_ = make_scheduler(brains={"a": brain}, sensor=sensor, clock=mono)
        sched.presences["a"] = ClientPresence(uid="a", connected_at=mono())
        sched.state.previous_user_idle_seconds = 700.0
        await sched.tick_once()
        received = calls[0].context
        self.assertIsNone(received.user_idle_seconds)
        self.assertIsNone(received.process_name)
        self.assertIsNone(received.fullscreen)
        self.assertEqual(received.taken_at_wall, _SCHED_WALL)
        self.assertIsNone(calls[0].category)
        self.assertFalse(calls[0].user_returned)

    async def test_quiet_seconds_uses_latest_across_clients(self):
        mono = FakeMono(1000.0)
        brain = make_real_brain(mono)
        calls = wrap_tick(brain)
        sched, *_ = make_scheduler(
            uids=("a", "b"), brains={"a": brain, "b": brain}, clock=mono
        )
        sched.presences["a"] = ClientPresence(
            uid="a", connected_at=0.0, last_conversation_end=900.0
        )
        sched.presences["b"] = ClientPresence(
            uid="b", connected_at=0.0, last_user_interaction=950.0
        )
        await sched.tick_once()
        self.assertEqual(calls[0].quiet_seconds, 50.0)

    async def test_quiet_seconds_falls_back_to_lifecycle_since(self):
        mono = FakeMono(1000.0)
        brain = make_real_brain(mono)
        calls = wrap_tick(brain)
        mono.t = 1300.0
        sched, *_ = make_scheduler(brains={"a": brain}, clock=mono)
        sched.presences["a"] = ClientPresence(uid="a", connected_at=0.0)
        await sched.tick_once()
        self.assertEqual(calls[0].quiet_seconds, 300.0)

    async def test_conversation_running_from_task_or_reserved(self):
        mono = FakeMono()
        brain = make_real_brain(mono)
        calls = wrap_tick(brain)
        sched, _, _, tasks = make_scheduler(brains={"a": brain}, clock=mono)
        sched.presences["a"] = ClientPresence(uid="a", connected_at=0.0)
        await sched.tick_once()
        self.assertFalse(calls[-1].conversation_running)
        tasks["a"] = _FakeTask(is_done=False)
        await sched.tick_once()
        self.assertTrue(calls[-1].conversation_running)
        tasks["a"] = _FakeTask(is_done=True)
        await sched.tick_once()
        self.assertFalse(calls[-1].conversation_running)
        sched.presences["a"].reserved = True
        await sched.tick_once()
        self.assertTrue(calls[-1].conversation_running)

    async def test_ignored_detection_fires_once(self):
        mono = FakeMono(1000.0)
        brain = make_real_brain(mono)
        events = []
        original_notify = brain.notify

        def _notify(event, **details):
            events.append(event)
            original_notify(event, **details)

        brain.notify = _notify
        sched, *_ = make_scheduler(brains={"a": brain}, clock=mono)
        presence = ClientPresence(uid="a", connected_at=0.0)
        presence.awaiting_reply_since = 1000.0
        sched.presences["a"] = presence

        mono.t = 1000.0 + 4 * 60
        await sched.tick_once()
        self.assertEqual(presence.ignored_count, 0)
        self.assertEqual(presence.awaiting_reply_since, 1000.0)

        mono.t = 1000.0 + 5 * 60
        with _LoguruCapture(level="INFO") as records:
            await sched.tick_once()
        self.assertEqual(presence.ignored_count, 1)
        self.assertIsNone(presence.awaiting_reply_since)
        self.assertEqual(events.count(BrainEvent.PROACTIVE_IGNORED), 1)
        self.assertTrue(
            any("[Proactive] ignored count=1 backoff=20min" in r for r in records)
        )

        mono.t += 60 * 60
        await sched.tick_once()
        self.assertEqual(presence.ignored_count, 1)
        self.assertEqual(events.count(BrainEvent.PROACTIVE_IGNORED), 1)

    async def test_select_and_dispatch_called_with_now_and_ctx(self):
        mono = FakeMono(1234.0)
        sensor = FakeSensor(_snapshot())
        sched, *_ = make_scheduler(sensor=sensor, clock=mono)
        sched.presences["a"] = ClientPresence(uid="a", connected_at=0.0)
        seen = []

        async def _spy(now, ctx):
            seen.append((now, ctx))

        sched._select_and_dispatch = _spy
        await sched.tick_once()
        self.assertEqual(seen, [(1234.0, sensor.snapshot)])

    async def test_connect_records_presence_with_clock(self):
        mono = FakeMono(42.0)
        sched, *_ = make_scheduler(clock=mono)
        await sched.client_connected("a")
        self.assertEqual(sched.presences["a"].connected_at, 42.0)
        await sched.client_disconnected("a")


HIGH_MOOD = MoodState(
    happiness=0.5,
    energy=1.0,
    curiosity=0.5,
    boredom=0.9,
    social_need=0.9,
    focus=0.3,
    sleepiness=0.0,
)


class ProactiveStub:
    """Injected `run_proactive_turn`; records calls, optionally blocks or raises."""

    def __init__(self, block=False, error=None):
        self.calls = []
        self.release = asyncio.Event()
        self.block = block
        self.error = error

    async def __call__(self, context, websocket_send, client_uid, context_block):
        self.calls.append((context, websocket_send, client_uid, context_block))
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error


def make_behavior_scheduler(
    uids=("a",),
    brains=None,
    stub=None,
    idle_expression=False,
    emo_map=None,
    mono=None,
):
    """Scheduler with a high-willingness brain, rng stub 0.0 and a browser sensor."""

    mono = mono or FakeMono(100_000.0)
    if brains is None:
        shared = make_real_brain(mono, away_after_min=30.0)
        brains = {uid: shared for uid in uids}
    for brain in {id(b): b for b in brains.values() if b is not None}.values():
        brain.mood = Mood(
            initial=MoodState(**vars(HIGH_MOOD)),
            clock=mono,
            wall_clock=FakeWall(_SCHED_WALL),
        )
        brain.config.idle_expression.enabled = idle_expression
    sensor = FakeSensor(_snapshot(idle=600.0, process="chrome.exe"))
    sched, connections, contexts, tasks = make_scheduler(
        uids=uids, brains=brains, sensor=sensor, clock=mono
    )
    if emo_map is not None:
        for ctx in contexts.values():
            ctx.live2d_model.emo_map = dict(emo_map)
    stub = stub or ProactiveStub()
    sched._run_proactive_turn = stub
    sched.selector = BehaviorSelector(rng=_FixedRandom(0.0))
    for uid in uids:
        sched.presences[uid] = ClientPresence(uid=uid, connected_at=0.0)
    return sched, stub, connections, contexts, tasks, mono


async def _drain(task):
    try:
        await task
    except BaseException:
        pass
    await asyncio.sleep(0)


class _CreateTaskCounter:
    """Counts `asyncio.create_task` calls made from inside the scheduler module."""

    def __init__(self):
        self.count = 0
        self._real = asyncio.create_task

    def __call__(self, coro, *args, **kwargs):
        self.count += 1
        return self._real(coro, *args, **kwargs)


class EvaluatePathTests(unittest.IsolatedAsyncioTestCase):
    def test_decision_path_is_synchronous(self):
        for name in ("evaluate_client", "request_proactive", "_commit_proactive"):
            self.assertFalse(
                inspect.iscoroutinefunction(getattr(BehaviorScheduler, name)), name
            )

    async def test_tick_commits_one_proactive(self):
        stub = ProactiveStub(block=True)
        sched, _, connections, contexts, tasks, _ = make_behavior_scheduler(stub=stub)
        await sched.tick_once()
        await asyncio.sleep(0)
        self.assertEqual(len(stub.calls), 1)
        context, send, uid, block = stub.calls[0]
        self.assertIs(context, contexts["a"])
        self.assertEqual(send, connections["a"].send_text)
        self.assertEqual(uid, "a")
        self.assertIn("browser (chrome.exe)", block)
        self.assertIn("you chose to", block)
        presence = sched.presences["a"]
        task = tasks["a"]
        self.assertIs(presence.proactive_task, task)
        self.assertTrue(presence.reserved)
        self.assertEqual(presence.last_proactive, 100_000.0)
        self.assertEqual(list(presence.proactive_timestamps), [100_000.0])
        stub.release.set()
        await _drain(task)
        self.assertFalse(presence.reserved)
        self.assertIsNone(presence.proactive_task)

    async def test_commit_logs_committed_line(self):
        sched, _, _, _, tasks, _ = make_behavior_scheduler()
        with _LoguruCapture(level="INFO") as records:
            d = sched.request_proactive("a")
        self.assertIs(d.kind, BehaviorKind.PROACTIVE_SPEAK)
        self.assertTrue(
            any(
                "[Proactive] committed uid=a willingness=" in r
                and "category=browser(chrome.exe)" in r
                for r in records
            ),
            records,
        )
        self.assertTrue(
            any("[Behavior] uid=a trigger=request" in r for r in records), records
        )
        await _drain(tasks["a"])

    async def test_request_refused_under_cooldown_sends_nothing(self):
        sched, stub, connections, _, tasks, mono = make_behavior_scheduler()
        sched.presences["a"].last_proactive = mono() - 60
        d = sched.request_proactive("a")
        self.assertEqual((d.kind, d.reason), (BehaviorKind.DO_NOTHING, "cooldown"))
        await asyncio.sleep(0)
        self.assertEqual(connections["a"].messages, [])
        self.assertEqual(stub.calls, [])
        self.assertNotIn("a", tasks)

    async def test_request_refusal_logged_at_info(self):
        sched, _, _, _, _, mono = make_behavior_scheduler()
        sched.presences["a"].last_proactive = mono() - 60
        with _LoguruCapture(level="INFO") as records:
            sched.request_proactive("a")
        self.assertTrue(
            any(
                "[Behavior] uid=a trigger=request" in r and "(cooldown)" in r
                for r in records
            ),
            records,
        )

    async def test_tick_do_nothing_logged_at_debug_only(self):
        sched, _, _, _, _, mono = make_behavior_scheduler()
        sched.presences["a"].last_proactive = mono() - 60
        with _LoguruCapture(level="INFO") as records:
            await sched.tick_once()
        self.assertFalse(any("[Behavior] uid=a" in r for r in records), records)
        with _LoguruCapture(level="DEBUG") as records:
            await sched.tick_once()
        self.assertTrue(
            any(
                "[Behavior] uid=a trigger=tick" in r and "(cooldown)" in r
                for r in records
            ),
            records,
        )

    def _gate_setups(self):
        def lock(sched, brain, presence, now):
            brain.activity = ActivityState.TALKING

        def hourly_cap(sched, brain, presence, now):
            presence.last_proactive = now - 15 * 60
            presence.proactive_timestamps.extend(
                [now - 50 * 60, now - 30 * 60, now - 15 * 60]
            )

        def low_willingness(sched, brain, presence, now):
            brain.mood = Mood(
                initial=MoodState(social_need=0.0, boredom=0.0, curiosity=0.0),
                clock=sched._clock,
                wall_clock=FakeWall(_SCHED_WALL),
            )

        def sleep(sched, brain, presence, now):
            brain.lifecycle.phase = LifecyclePhase.SLEEP

        return {
            "conversation_lock": lock,
            "hourly_cap": hourly_cap,
            "low_willingness": low_willingness,
            "lifecycle_sleep": sleep,
        }

    async def test_request_and_tick_share_gates(self):
        for expected, setup in self._gate_setups().items():
            reasons = {}
            for trigger in (Trigger.TICK, Trigger.REQUEST):
                sched, stub, *_ = make_behavior_scheduler()
                sched.state.last_context = sched.sensor.sample()
                brain = sched.eligibility.brain_for("a")
                setup(sched, brain, sched.presences["a"], sched._clock())
                d = sched.evaluate_client("a", trigger)
                self.assertIs(d.kind, BehaviorKind.DO_NOTHING, (expected, trigger))
                reasons[trigger] = d.reason
                self.assertEqual(stub.calls, [])
            self.assertEqual(reasons[Trigger.TICK], expected)
            self.assertEqual(reasons[Trigger.REQUEST], expected)

    async def test_legacy_timer_spam_refused(self):
        counter = _CreateTaskCounter()
        sched, stub, connections, _, tasks, _ = make_behavior_scheduler()
        with mock.patch.object(scheduler_module.asyncio, "create_task", counter):
            first = sched.request_proactive("a")
            self.assertIs(first.kind, BehaviorKind.PROACTIVE_SPEAK)
            decisions = [sched.request_proactive("a") for _ in range(10)]
        self.assertTrue(all(d.kind is BehaviorKind.DO_NOTHING for d in decisions))
        await _drain(tasks["a"])
        with mock.patch.object(scheduler_module.asyncio, "create_task", counter):
            decisions = [sched.request_proactive("a") for _ in range(10)]
        self.assertTrue(all(d.kind is BehaviorKind.DO_NOTHING for d in decisions))
        self.assertEqual({d.reason for d in decisions}, {"cooldown"})
        self.assertEqual(counter.count, 1)
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(connections["a"].messages, [])

    async def test_images_discarded(self):
        sched, stub, _, _, tasks, _ = make_behavior_scheduler()
        images = [{"source": "screen", "data": "xxx", "mime_type": "image/png"}]
        with _LoguruCapture(level="DEBUG") as records:
            sched.request_proactive("a", images=images)
        await _drain(tasks["a"])
        self.assertEqual(len(stub.calls), 1)
        for arg in stub.calls[0]:
            self.assertIsNot(arg, images)
        self.assertNotIn("xxx", stub.calls[0][3])
        self.assertTrue(
            any(
                "[Behavior] discarded 1 legacy proactive image(s)" in r for r in records
            ),
            records,
        )

    async def test_request_refreshes_context_synchronously(self):
        sched, _, _, _, tasks, _ = make_behavior_scheduler()
        self.assertIsNone(sched.state.last_context)
        sched.request_proactive("a")
        self.assertEqual(sched.sensor.calls, 1)
        self.assertIs(sched.state.last_context, sched.sensor.snapshot)
        await _drain(tasks["a"])
        self.assertIn("the app asked you to", sched._run_proactive_turn.calls[0][3])

    async def test_returned_bonus_consumed_and_in_prompt(self):
        sched, stub, _, _, tasks, _ = make_behavior_scheduler()
        presence = sched.presences["a"]
        presence.returned_bonus_pending = True
        presence.returned_after_s = 1800.0
        sched.request_proactive("a")
        await _drain(tasks["a"])
        self.assertIn("came back after 30 min", stub.calls[0][3])
        self.assertFalse(presence.returned_bonus_pending)
        self.assertIsNone(presence.returned_after_s)

    async def test_returned_bonus_consumed_when_refused_by_willingness(self):
        sched, stub, *_ = make_behavior_scheduler()
        sched.selector = BehaviorSelector(rng=_FixedRandom(0.99))
        presence = sched.presences["a"]
        presence.returned_bonus_pending = True
        presence.returned_after_s = 900.0
        d = sched.request_proactive("a")
        self.assertEqual(d.reason, "chance")
        self.assertFalse(presence.returned_bonus_pending)
        self.assertEqual(stub.calls, [])

    async def test_context_disabled_brain_evaluates_unknown(self):
        sched, stub, *_ = make_behavior_scheduler()
        sched.eligibility.brain_for("a").config.context.enabled = False
        d = sched.request_proactive("a")
        self.assertEqual(
            (d.kind, d.reason), (BehaviorKind.DO_NOTHING, "context_unknown")
        )

    async def test_missing_presence_does_nothing(self):
        sched, stub, *_ = make_behavior_scheduler()
        del sched.presences["a"]
        d = sched.request_proactive("a")
        self.assertIs(d.kind, BehaviorKind.DO_NOTHING)
        self.assertEqual(stub.calls, [])


class RaceTests(unittest.IsolatedAsyncioTestCase):
    async def _run_race(self, request_first):
        counter = _CreateTaskCounter()
        sched, stub, _, _, tasks, _ = make_behavior_scheduler()

        async def req():
            return sched.request_proactive("a")

        with mock.patch.object(scheduler_module.asyncio, "create_task", counter):
            if request_first:
                await asyncio.gather(req(), sched.tick_once())
            else:
                await asyncio.gather(sched.tick_once(), req())
            await _drain(tasks["a"])
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(counter.count, 1)

    async def test_request_then_tick_same_iteration_one_task(self):
        await self._run_race(request_first=True)

    async def test_tick_then_request_one_task(self):
        await self._run_race(request_first=False)

    async def test_recheck_sees_reserved(self):
        sched, stub, _, _, tasks, _ = make_behavior_scheduler()
        original = sched.selector.select

        def _select(inp):
            d = original(inp)
            sched.presences["a"].reserved = True
            return d

        sched.selector.select = _select
        d = sched.request_proactive("a")
        self.assertEqual(
            (d.kind, d.reason), (BehaviorKind.DO_NOTHING, "conversation_lock")
        )
        await asyncio.sleep(0)
        self.assertEqual(stub.calls, [])
        self.assertNotIn("a", tasks)
        self.assertIsNone(sched.presences["a"].last_proactive)

    async def test_recheck_sees_activity_not_idle(self):
        sched, stub, _, _, tasks, _ = make_behavior_scheduler()
        original = sched.selector.select
        brain = sched.eligibility.brain_for("a")

        def _select(inp):
            d = original(inp)
            brain.activity = ActivityState.THINKING
            return d

        sched.selector.select = _select
        d = sched.request_proactive("a")
        self.assertEqual(d.reason, "conversation_lock")
        self.assertNotIn("a", tasks)

    async def test_switch_config_follows_new_brain(self):
        sched, stub, _, contexts, tasks, mono = make_behavior_scheduler()
        old = contexts["a"].pet_brain
        new = make_real_brain(mono, away_after_min=30.0)
        new.config.idle_expression.enabled = False
        old_calls = wrap_tick(old)
        new_calls = wrap_tick(new)
        contexts["a"].pet_brain = new
        sched.presences["a"].last_proactive = mono() - 60
        await sched.tick_once()
        self.assertEqual((len(old_calls), len(new_calls)), (0, 1))
        contexts["a"].pet_brain = None
        await sched.tick_once()
        self.assertEqual((len(old_calls), len(new_calls)), (0, 1))
        self.assertEqual(sched.request_proactive("a").reason, "not_handled")
        self.assertEqual(stub.calls, [])

    async def test_client_a_does_not_affect_client_b(self):
        sched, stub, _, _, tasks, mono = make_behavior_scheduler(uids=("a", "b"))
        a = sched.presences["a"]
        a.last_proactive = mono() - 60
        before = (
            a.last_proactive,
            list(a.proactive_timestamps),
            a.reserved,
            a.last_conversation_end,
            a.awaiting_reply_since,
        )
        seen = {}
        original = sched.evaluate_client

        def _spy(uid, trigger, **kwargs):
            d = original(uid, trigger, **kwargs)
            seen[uid] = d
            return d

        sched.evaluate_client = _spy
        await sched.tick_once()
        self.assertEqual(
            (seen["a"].kind, seen["a"].reason), (BehaviorKind.DO_NOTHING, "cooldown")
        )
        self.assertIs(seen["b"].kind, BehaviorKind.PROACTIVE_SPEAK)
        await _drain(tasks["b"])
        self.assertEqual([c[2] for c in stub.calls], ["b"])
        self.assertNotIn("a", tasks)
        self.assertEqual(
            before,
            (
                a.last_proactive,
                list(a.proactive_timestamps),
                a.reserved,
                a.last_conversation_end,
                a.awaiting_reply_since,
            ),
        )

    async def test_at_most_one_proactive_per_tick(self):
        mono = FakeMono(100_000.0)
        brains = {
            "a": make_real_brain(mono, away_after_min=30.0),
            "b": make_real_brain(mono, away_after_min=30.0),
        }
        sched, stub, _, _, tasks, _ = make_behavior_scheduler(
            uids=("a", "b"), brains=brains, mono=mono
        )
        sched.presences["b"].last_user_interaction = mono() - 3600
        seen = {}
        original = sched.evaluate_client

        def _spy(uid, trigger, **kwargs):
            d = original(uid, trigger, **kwargs)
            seen[uid] = d
            return d

        sched.evaluate_client = _spy
        await sched.tick_once()
        self.assertIs(seen["b"].kind, BehaviorKind.PROACTIVE_SPEAK)
        self.assertEqual(
            (seen["a"].kind, seen["a"].reason),
            (BehaviorKind.DO_NOTHING, "proactive_slot_taken"),
        )
        self.assertNotIn("a", tasks)
        self.assertIsNone(sched.presences["a"].last_proactive)
        await _drain(tasks["b"])
        self.assertEqual(len(stub.calls), 1)

    async def test_disconnect_connect_race_keeps_loop(self):
        sched, *_ = make_scheduler(uids=("a", "b"))
        await sched.client_connected("a")
        await sched.tick_once()
        await asyncio.gather(
            sched.client_disconnected("a"), sched.client_connected("b")
        )
        self.assertTrue(sched.is_running)
        self.assertEqual(set(sched.presences), {"b"})
        await sched.client_disconnected("b")

    async def test_context_label_hidden_when_context_disabled(self):
        mono = FakeMono()
        brain = make_real_brain(mono, context_enabled=False)
        sched, *_ = make_scheduler(
            brains={"a": brain},
            sensor=FakeSensor(_snapshot(process="Code.exe")),
            clock=mono,
        )
        sched.presences["a"] = ClientPresence(uid="a", connected_at=0.0)
        with _LoguruCapture(level="DEBUG") as records:
            await sched.tick_once()
        self.assertFalse(any("Code.exe" in r for r in records), records)
        self.assertFalse(
            any("[Context]" in r and "coding" in r for r in records), records
        )


class ProactiveDoneCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion_sets_awaiting_and_spoken(self):
        sched, stub, _, _, tasks, mono = make_behavior_scheduler()
        brain = sched.eligibility.brain_for("a")
        social_before = brain.mood.snapshot()["social_need"]
        sched.request_proactive("a")
        mono.t += 30
        await _drain(tasks["a"])
        presence = sched.presences["a"]
        self.assertEqual(presence.awaiting_reply_since, mono())
        self.assertEqual(presence.last_conversation_end, mono())
        self.assertFalse(presence.reserved)
        self.assertIsNone(presence.proactive_task)
        self.assertLess(brain.mood.snapshot()["social_need"], social_before)

    async def test_error_completion_no_awaiting(self):
        stub = ProactiveStub(error=RuntimeError("boom"))
        sched, _, _, _, tasks, mono = make_behavior_scheduler(stub=stub)
        brain = sched.eligibility.brain_for("a")
        social_before = brain.mood.snapshot()["social_need"]
        sched.request_proactive("a")
        with _LoguruCapture(level="DEBUG"):
            await _drain(tasks["a"])
        presence = sched.presences["a"]
        self.assertIsNone(presence.awaiting_reply_since)
        self.assertEqual(presence.last_conversation_end, mono())
        self.assertFalse(presence.reserved)
        self.assertEqual(brain.mood.snapshot()["social_need"], social_before)
        self.assertEqual(presence.last_proactive, mono())

    async def test_cancelled_completion_no_awaiting(self):
        stub = ProactiveStub(block=True)
        sched, _, _, _, tasks, _ = make_behavior_scheduler(stub=stub)
        sched.request_proactive("a")
        await asyncio.sleep(0)
        tasks["a"].cancel()
        await _drain(tasks["a"])
        presence = sched.presences["a"]
        self.assertIsNone(presence.awaiting_reply_since)
        self.assertFalse(presence.reserved)
        self.assertIsNone(presence.proactive_task)

    async def test_preempted_completion_no_awaiting_and_flag_reset(self):
        sched, _, _, _, tasks, _ = make_behavior_scheduler()
        sched.request_proactive("a")
        presence = sched.presences["a"]
        presence.proactive_preempted = True
        await _drain(tasks["a"])
        self.assertIsNone(presence.awaiting_reply_since)
        self.assertFalse(presence.proactive_preempted)

    async def test_presence_gone_is_tolerated(self):
        stub = ProactiveStub(block=True)
        sched, _, _, _, tasks, _ = make_behavior_scheduler(stub=stub)
        sched.request_proactive("a")
        await asyncio.sleep(0)
        del sched.presences["a"]
        stub.release.set()
        await tasks["a"]
        await asyncio.sleep(0)
        self.assertNotIn("a", sched.presences)

    async def test_stale_callback_does_not_touch_new_presence(self):
        stub = ProactiveStub(block=True)
        sched, _, _, _, tasks, _ = make_behavior_scheduler(stub=stub)
        sched.request_proactive("a")
        await asyncio.sleep(0)
        fresh = ClientPresence(uid="a", connected_at=0.0)
        sched.presences["a"] = fresh
        stub.release.set()
        await _drain(tasks["a"])
        self.assertIsNone(fresh.awaiting_reply_since)
        self.assertIsNone(fresh.last_conversation_end)


class _FailingWS:
    async def send_text(self, message):
        raise RuntimeError("websocket closed")


class IdleDispatchTests(unittest.IsolatedAsyncioTestCase):
    def test_apply_idle_records_and_returns_actions(self):
        mono = FakeMono(5.0)
        manager = EmotionManager(clock=mono)
        model = SimpleNamespace(emo_map={"sleepy": 5, "neutral": 0})
        actions = manager.apply_idle("sleepy", model)
        self.assertEqual(actions.expressions, [5])
        self.assertIsNone(actions.emotion)
        self.assertEqual(manager.current_emotion, "sleepy")
        self.assertEqual(manager.current_expression, 5)
        self.assertIs(manager.current_emotion_source, EmotionSource.IDLE_BEHAVIOR)
        self.assertEqual(EmotionSource.IDLE_BEHAVIOR.value, "idle_behavior")
        self.assertEqual(manager.updated_at, 5.0)

    def test_apply_idle_missing_key_returns_none(self):
        manager = EmotionManager()
        self.assertIsNone(manager.apply_idle("bored", SimpleNamespace(emo_map={})))
        self.assertIsNone(manager.apply_idle("bored", None))
        self.assertIsNone(manager.current_emotion_source)

    async def test_dispatch_sends_expression_only_payload(self):
        sched, _, connections, _, _, mono = make_behavior_scheduler(
            emo_map={"sleepy": 5, "neutral": 0}
        )
        await sched._dispatch_idle("a", "sleepy")
        self.assertEqual(len(connections["a"].messages), 1)
        payload = json.loads(connections["a"].messages[0])
        self.assertEqual(payload["type"], "audio")
        self.assertIsNone(payload["audio"])
        self.assertIsNone(payload["display_text"])
        self.assertEqual(payload["actions"], {"expressions": [5]})
        self.assertNotIn("emotion", payload["actions"])
        brain = sched.eligibility.brain_for("a")
        self.assertEqual(brain.emotion.current_emotion_source.value, "idle_behavior")
        presence = sched.presences["a"]
        self.assertEqual(presence.last_idle_expression, mono())
        self.assertEqual(list(presence.idle_expression_timestamps), [mono()])
        self.assertFalse(presence.reserved)

    async def test_dispatch_missing_key_sends_nothing(self):
        sched, _, connections, _, _, _ = make_behavior_scheduler(emo_map={"neutral": 0})
        await sched._dispatch_idle("a", "bored")
        self.assertEqual(connections["a"].messages, [])
        self.assertIsNone(sched.presences["a"].last_idle_expression)

    async def test_dispatch_send_failure_is_swallowed(self):
        sched, _, connections, _, _, _ = make_behavior_scheduler(emo_map={"sleepy": 5})
        connections["a"] = _FailingWS()
        with _LoguruCapture(level="DEBUG") as records:
            await sched._dispatch_idle("a", "sleepy")
        self.assertTrue(
            any("[Behavior]" in r and "websocket closed" in r for r in records),
            records,
        )

    async def test_tick_dispatches_idle_expression(self):
        mono = FakeMono(100_000.0)
        brain = make_real_brain(mono, away_after_min=30.0)
        brain.config.proactive.enabled = False
        sched, stub, connections, _, tasks, _ = make_behavior_scheduler(
            brains={"a": brain}, idle_expression=True, emo_map={"bored": 7}, mono=mono
        )
        await sched.tick_once()
        self.assertEqual(stub.calls, [])
        self.assertNotIn("a", tasks)
        self.assertEqual(len(connections["a"].messages), 1)
        payload = json.loads(connections["a"].messages[0])
        self.assertEqual(payload["actions"], {"expressions": [7]})


class PetBrainExportsTests(unittest.TestCase):
    def test_package_exports(self):
        import src.open_llm_vtuber.pet_brain as pkg

        for name in (
            "ContextSnapshot",
            "DayPart",
            "BehaviorKind",
            "Trigger",
            "Decision",
            "ClientPresence",
        ):
            self.assertIn(name, pkg.__all__)
            self.assertTrue(hasattr(pkg, name))
        self.assertNotIn("BehaviorScheduler", pkg.__all__)


if __name__ == "__main__":
    unittest.main()
