"""Phase 2 behavior tests. Run from repo root: uv run python -m unittest tests.test_behavior_phase2 -v"""

import inspect
import random
import sys
import unittest
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

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
from src.open_llm_vtuber.pet_brain.presence import ClientPresence
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


if __name__ == "__main__":
    unittest.main()
