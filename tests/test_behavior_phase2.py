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
from src.open_llm_vtuber.pet_brain.rhythm import (
    DayPart,
    day_part_at,
    split_by_day_part,
)
from src.open_llm_vtuber.service_context import ServiceContext


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


if __name__ == "__main__":
    unittest.main()
