"""Phase 3A desktop pet tests. Run from repo root: uv run python -m unittest tests.test_desktop_pet_phase3 -v"""

import unittest

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
from src.open_llm_vtuber.service_context import ServiceContext


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


if __name__ == "__main__":
    unittest.main()
