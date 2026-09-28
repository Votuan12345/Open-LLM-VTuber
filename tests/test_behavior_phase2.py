"""Phase 2 behavior tests. Run from repo root: uv run python -m unittest tests.test_behavior_phase2 -v"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
