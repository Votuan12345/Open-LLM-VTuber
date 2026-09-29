"""Phase 3A desktop pet tests. Run from repo root: uv run python -m unittest tests.test_desktop_pet_phase3 -v"""

import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
