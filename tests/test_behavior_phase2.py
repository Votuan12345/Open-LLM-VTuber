"""Phase 2 behavior tests. Run from repo root: uv run python -m unittest tests.test_behavior_phase2 -v"""

import unittest
from datetime import datetime, timedelta

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

from src.open_llm_vtuber.pet_brain import BrainEvent, Mood
from src.open_llm_vtuber.pet_brain.mood import MOOD_KEYS, MoodState
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


if __name__ == "__main__":
    unittest.main()
