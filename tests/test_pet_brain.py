"""Phase 1 PetBrain tests. Run from repo root: uv run python -m unittest discover -s tests -v"""

import asyncio
import unittest
from types import SimpleNamespace

from loguru import logger

from src.open_llm_vtuber.agent.output_types import (
    Actions,
    DisplayText,
    EmotionTag,
    SentenceOutput,
)
from src.open_llm_vtuber.utils.sentence_divider import SentenceWithTags
from src.open_llm_vtuber.agent.transformers import (
    actions_extractor,
    display_processor,
    sentence_divider,
    tts_filter,
)
from src.open_llm_vtuber.config_manager import (
    PetBrainConfig,
    PermissionConfig,
    read_yaml,
    validate_config,
)
from src.open_llm_vtuber.live2d_model import Live2dModel
from src.open_llm_vtuber.utils.stream_audio import prepare_audio_payload
from src.open_llm_vtuber.conversations.conversation_utils import handle_sentence_output
from src.open_llm_vtuber.mcpp.tool_executor import ToolExecutor
from src.open_llm_vtuber.mcpp.tool_manager import ToolManager
from src.open_llm_vtuber.mcpp.types import FormattedTool
from src.open_llm_vtuber.pet_brain import (
    ActivityState,
    BrainEvent,
    ConfirmationProvider,
    EmotionManager,
    EmotionSource,
    Lifecycle,
    LifecyclePhase,
    Mood,
    PermissionGuard,
    PermissionLevel,
    PetBrain,
    ToolRequest,
)
from src.open_llm_vtuber.service_context import ServiceContext


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


MAO_EMO_MAP = {"neutral": 0, "anger": 2, "fear": 1, "joy": 3, "smirk": 3}
FAKE_MODEL = SimpleNamespace(emo_map=MAO_EMO_MAP)
# Real model from model_dict.json: joy and smirk both map to expression 3.
MAO_MODEL = Live2dModel("mao_pro")
TEMPLATE_TTS_PREPROCESSOR = validate_config(
    read_yaml("config_templates/conf.default.yaml")
).character_config.tts_preprocessor_config


def run(coro):
    return asyncio.run(coro)


def extract_actions(text):
    """Pass one sentence through the real actions_extractor decorator."""

    @actions_extractor(MAO_MODEL)
    async def one_sentence():
        yield SentenceWithTags(text=text, tags=[])

    async def first():
        async for _, actions in one_sentence():
            return actions

    return run(first())


class ConfigTests(unittest.TestCase):
    def test_default_is_disabled(self):
        self.assertFalse(PetBrainConfig().enabled)

    def test_templates_validate_and_are_disabled(self):
        for path in (
            "config_templates/conf.default.yaml",
            "config_templates/conf.ZH.default.yaml",
        ):
            config = validate_config(read_yaml(path))
            pb = config.character_config.pet_brain_config
            self.assertFalse(pb.enabled, path)
            self.assertEqual(pb.permission.tool_levels["get_current_time"], "read")

    def test_config_without_section_still_validates(self):
        data = read_yaml("config_templates/conf.default.yaml")
        del data["character_config"]["pet_brain_config"]
        config = validate_config(data)
        self.assertFalse(config.character_config.pet_brain_config.enabled)

    def test_invalid_level_rejected(self):
        with self.assertRaises(Exception):
            PermissionConfig(tool_levels={"x": "admin"})


class EmotionManagerTests(unittest.TestCase):
    def setUp(self):
        self.em = EmotionManager()

    def gate_llm(self, text):
        """Run the real actions_extractor on one sentence, then the gate."""
        actions = extract_actions(text)
        return actions, self.em.gate(actions, MAO_MODEL)

    # 1
    def test_smirk_tag_keeps_name_and_expressions(self):
        actions, gated = self.gate_llm("[smirk] Hehe.")
        self.assertIs(gated, actions)
        self.assertEqual(gated.expressions, [3])
        self.assertEqual(self.em.current_emotion, "smirk")
        self.assertEqual(self.em.current_emotion_source, EmotionSource.LLM_TAG)

    # 2
    def test_joy_tag_keeps_name_and_expressions(self):
        actions, gated = self.gate_llm("[joy] Yay!")
        self.assertIs(gated, actions)
        self.assertEqual(gated.expressions, [3])
        self.assertEqual(self.em.current_emotion, "joy")

    # 3
    def test_same_index_tags_stay_distinct(self):
        _, first = self.gate_llm("[smirk] Hehe.")
        self.assertEqual(self.em.current_emotion, "smirk")
        _, second = self.gate_llm("[joy] Yay!")
        self.assertEqual(self.em.current_emotion, "joy")
        self.assertEqual(first.expressions, second.expressions)
        self.assertEqual(self.em.current_expression, 3)

    def test_last_valid_tag_wins(self):
        _, gated = self.gate_llm("[joy] and then [anger]")
        self.assertEqual(gated.expressions, [3, 2])
        self.assertEqual(self.em.current_emotion, "anger")

    # 4
    def test_invalid_expression_index_falls_back_to_neutral(self):
        actions = Actions(expressions=[99], sounds=["a.wav"])
        gated = self.em.gate(actions, MAO_MODEL)
        self.assertEqual(gated.expressions, [0])
        self.assertEqual(gated.sounds, ["a.wav"])
        self.assertEqual(actions.expressions, [99], "input must not be mutated")
        self.assertEqual(self.em.current_emotion, "neutral")
        self.assertEqual(
            self.em.current_emotion_source, EmotionSource.FALLBACK_INVALID_EXPRESSION
        )

    def test_mixed_index_keeps_only_valid(self):
        gated = self.em.gate(Actions(expressions=[2, 99, "x"]), MAO_MODEL)
        self.assertEqual(gated.expressions, [2])

    # 5
    def test_unknown_tag_is_ignored_without_fallback(self):
        self.gate_llm("[smirk] Hehe.")
        actions, gated = self.gate_llm("[happy] Hi!")
        self.assertIs(gated, actions)
        self.assertIsNone(gated.expressions, "no expression change")
        self.assertEqual(actions.emotion.unknown, ("happy",))
        self.assertEqual(self.em.current_emotion, "smirk", "state untouched")

    def test_unknown_tag_logs_warning(self):
        with self.assertLogs_loguru() as records:
            self.gate_llm("[happy] Hi!")
        self.assertTrue(any("Ignoring unknown emotion tags" in r for r in records))

    def test_non_word_brackets_are_not_tags(self):
        actions, gated = self.gate_llm("See [1] and [some note here].")
        self.assertIsNone(actions.emotion)
        self.assertIsNone(gated.expressions)

    def test_unknown_tag_next_to_valid_tag_is_ignored(self):
        actions, gated = self.gate_llm("[happy][smirk] Hi!")
        self.assertIs(gated, actions)
        self.assertEqual(gated.expressions, [3])
        self.assertEqual(self.em.current_emotion, "smirk")

    def test_no_tags_no_change(self):
        self.gate_llm("[joy] Yay!")
        actions, gated = self.gate_llm("Plain text.")
        self.assertIs(gated, actions)
        self.assertIsNone(gated.expressions)
        self.assertEqual(self.em.current_emotion, "joy")

    def test_expression_without_text_does_not_guess_name(self):
        actions = Actions(expressions=[3])
        self.assertIs(self.em.gate(actions, MAO_MODEL), actions)
        self.assertIsNone(self.em.current_emotion)
        self.assertEqual(self.em.current_expression, 3)
        self.assertEqual(self.em.current_emotion_source, EmotionSource.EXPRESSION_ONLY)

    def test_none_actions_pass_through(self):
        self.assertIsNone(self.em.gate(None, MAO_MODEL))

    def test_no_fallback_in_map_drops_expressions(self):
        model = SimpleNamespace(emo_map={"joy": 3})
        gated = self.em.gate(Actions(expressions=[99]), model)
        self.assertIsNone(gated.expressions)

    def test_no_model_passes_through(self):
        actions = Actions(expressions=[99])
        self.assertIs(self.em.gate(actions, None), actions)

    def assertLogs_loguru(self):
        return _LoguruCapture()


class _LoguruCapture:
    def __enter__(self):
        self.records = []
        self._id = logger.add(lambda m: self.records.append(str(m)), level="WARNING")
        return self.records

    def __exit__(self, *exc):
        logger.remove(self._id)
        return False


class SingleParserTests(unittest.TestCase):
    """Live2dModel.parse_emotions is the only emotion-tag parser."""

    def test_extract_emotion_is_derived_from_parse_emotions(self):
        for text in [
            "[smirk] Hehe.",
            "[joy] then [anger]",
            "[JOY] caps",
            "[happy] Hi!",
            "[joy][smirk][neutral]",
            "no tags",
            "[1] [some note] [joy",
        ]:
            parsed = MAO_MODEL.parse_emotions(text)
            self.assertEqual(
                MAO_MODEL.extract_emotion(text),
                [t.expression for t in parsed.tags],
                text,
            )

    def test_parse_keeps_name_and_index(self):
        parsed = MAO_MODEL.parse_emotions("[Smirk] a [joy] b [happy] c [1]")
        self.assertEqual(parsed.tags, (EmotionTag("smirk", 3), EmotionTag("joy", 3)))
        self.assertEqual(parsed.unknown, ("happy",))

    def test_extract_emotion_matches_original_algorithm(self):
        def original(model, s):
            out, s, i = [], s.lower(), 0
            while i < len(s):
                if s[i] != "[":
                    i += 1
                    continue
                for key in model.emo_map.keys():
                    tag = f"[{key}]"
                    if s[i : i + len(tag)] == tag:
                        out.append(model.emo_map[key])
                        i += len(tag) - 1
                        break
                i += 1
            return out

        for text in ["[smirk][joy]", "[happy][joy]", "x[anger]y[fear]", "[[joy]]", ""]:
            self.assertEqual(MAO_MODEL.extract_emotion(text), original(MAO_MODEL, text))

    def test_internal_emotion_field_never_serialized(self):
        actions = extract_actions("[smirk] hi [happy]")
        self.assertIsNotNone(actions.emotion)
        self.assertEqual(actions.to_dict(), {"expressions": [3]})
        payload = prepare_audio_payload(audio_path=None, actions=actions)
        self.assertEqual(payload["actions"], {"expressions": [3]})
        self.assertEqual(Actions().to_dict(), {})


class ApproveAll(ConfirmationProvider):
    async def confirm(self, request, level):
        return True


class Exploding(ConfirmationProvider):
    async def confirm(self, request, level):
        raise RuntimeError("boom")


class PermissionTests(unittest.TestCase):
    def guard(self, confirmation=None):
        return PermissionGuard(
            PermissionConfig(
                tool_levels={"get_current_time": "read", "open_app": "interact"}
            ),
            confirmation,
        )

    def test_read_and_interact_whitelisted(self):
        g = self.guard()
        self.assertTrue(run(g.check(ToolRequest("get_current_time"))).allowed)
        self.assertTrue(run(g.check(ToolRequest("open_app"))).allowed)

    def test_unlisted_is_destructive_and_denied(self):
        d = run(self.guard().check(ToolRequest("delete_file")))
        self.assertFalse(d.allowed)
        self.assertEqual(d.level, PermissionLevel.DESTRUCTIVE)
        self.assertEqual(d.reason, "Confirmation required")

    def test_confirmation_can_approve(self):
        self.assertTrue(run(self.guard(ApproveAll()).check(ToolRequest("x"))).allowed)

    def test_confirmation_failure_denies(self):
        self.assertFalse(run(self.guard(Exploding()).check(ToolRequest("x"))).allowed)


class FakeMCPClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server_name, tool_name, tool_args):
        self.calls.append(tool_name)
        return {"metadata": {}, "content_items": [{"type": "text", "text": "ok"}]}


class ToolExecutorPermissionTests(unittest.TestCase):
    def make(self, guard):
        client = FakeMCPClient()
        manager = ToolManager(
            initial_tools_dict={
                "get_current_time": FormattedTool({}, "time"),
                "delete_file": FormattedTool({}, "fs"),
            }
        )
        return ToolExecutor(client, manager, permission_guard=guard), client

    def test_no_guard_executes_everything(self):
        executor, client = self.make(None)
        is_error, text, _, _ = run(executor.run_single_tool("delete_file", "1", {}))
        self.assertFalse(is_error)
        self.assertEqual(client.calls, ["delete_file"])

    def test_guard_blocks_before_execution(self):
        guard = PermissionGuard(
            PermissionConfig(tool_levels={"get_current_time": "read"})
        )
        executor, client = self.make(guard)
        is_error, text, _, items = run(executor.run_single_tool("delete_file", "1", {}))
        self.assertTrue(is_error)
        self.assertIn("Permission denied", text)
        self.assertEqual(items[0]["type"], "error")
        self.assertEqual(client.calls, [])

        is_error, _, _, _ = run(executor.run_single_tool("get_current_time", "2", {}))
        self.assertFalse(is_error)
        self.assertEqual(client.calls, ["get_current_time"])

    def test_guard_exception_fails_closed(self):
        class Broken:
            async def check(self, request):
                raise RuntimeError("boom")

        executor, client = self.make(Broken())
        is_error, _, _, _ = run(executor.run_single_tool("get_current_time", "1", {}))
        self.assertTrue(is_error)
        self.assertEqual(client.calls, [])

    def test_execute_tools_status_contract_on_denial(self):
        guard = PermissionGuard(PermissionConfig())
        executor, _ = self.make(guard)

        async def collect():
            call = {"id": "t1", "name": "delete_file", "input": {}}
            return [u async for u in executor.execute_tools([call], "Prompt")]

        updates = run(collect())
        self.assertEqual(updates[0]["status"], "running")
        self.assertEqual(updates[1]["status"], "error")
        self.assertEqual(updates[-1]["type"], "final_tool_results")
        self.assertTrue(updates[-1]["results"][0]["is_error"])


class MoodTests(unittest.TestCase):
    def test_lazy_time_drift(self):
        clock = FakeClock()
        mood = Mood(clock=clock)
        before = mood.snapshot()
        clock.t += 3600
        after = mood.snapshot()
        self.assertGreater(after["boredom"], before["boredom"])
        self.assertGreater(after["social_need"], before["social_need"])
        self.assertLess(after["energy"], before["energy"])

    def test_values_clamped(self):
        clock = FakeClock()
        mood = Mood(clock=clock)
        clock.t += 3600 * 100
        snap = mood.snapshot()
        self.assertTrue(all(0.0 <= v <= 1.0 for v in snap.values()))
        self.assertEqual(snap["boredom"], 1.0)

    def test_user_input_effects(self):
        mood = Mood(clock=FakeClock())
        before = mood.snapshot()
        mood.apply_event(BrainEvent.USER_INPUT)
        after = mood.snapshot()
        self.assertLess(after["social_need"], before["social_need"])
        self.assertLess(after["boredom"], before["boredom"])
        self.assertGreater(after["happiness"], before["happiness"])

    def test_unknown_key_ignored(self):
        mood = Mood(clock=FakeClock())
        mood.apply({"not_a_mood": 1.0}, reason="test")
        self.assertNotIn("not_a_mood", mood.snapshot())


class LifecycleTests(unittest.TestCase):
    def test_starts_awake_and_rejects_invalid(self):
        lc = Lifecycle(clock=FakeClock())
        self.assertEqual(lc.phase, LifecyclePhase.WAKE_UP)
        self.assertFalse(lc.transition(LifecyclePhase.SLEEP, "test"))
        self.assertEqual(lc.phase, LifecyclePhase.WAKE_UP)

    def test_ensure_active_from_sleep_goes_through_wake_up(self):
        lc = Lifecycle(clock=FakeClock())
        lc.transition(LifecyclePhase.IDLE, "t")
        lc.transition(LifecyclePhase.SLEEP, "t")
        lc.ensure_active("user_input")
        self.assertEqual(lc.phase, LifecyclePhase.ACTIVE)


class PetBrainTests(unittest.TestCase):
    def test_conversation_sequence(self):
        brain = PetBrain(PetBrainConfig(enabled=True), clock=FakeClock())
        self.assertEqual(brain.activity, ActivityState.IDLE)
        brain.notify(BrainEvent.USER_INPUT)
        self.assertEqual(brain.activity, ActivityState.THINKING)
        self.assertEqual(brain.lifecycle.phase, LifecyclePhase.ACTIVE)
        brain.notify(BrainEvent.RESPONSE_START)
        brain.notify(BrainEvent.RESPONSE_START)
        self.assertEqual(brain.activity, ActivityState.TALKING)
        brain.notify(BrainEvent.INTERRUPTED)
        self.assertEqual(brain.activity, ActivityState.IDLE)
        snap = brain.snapshot()
        self.assertEqual(snap["activity"], "idle")
        self.assertIn("mood", snap)


class ServiceContextPetBrainTests(unittest.TestCase):
    def test_init_pet_brain_lifecycle(self):
        ctx = ServiceContext()
        self.assertFalse(ctx.init_pet_brain(PetBrainConfig(enabled=False)))
        self.assertIsNone(ctx.pet_brain)

        self.assertTrue(ctx.init_pet_brain(PetBrainConfig(enabled=True)))
        first = ctx.pet_brain
        self.assertIsNotNone(first)

        self.assertFalse(ctx.init_pet_brain(PetBrainConfig(enabled=True)))
        self.assertIs(ctx.pet_brain, first, "same config keeps mood/state")

        changed = PetBrainConfig(
            enabled=True, permission=PermissionConfig(tool_levels={"a": "read"})
        )
        self.assertTrue(ctx.init_pet_brain(changed))
        self.assertIsNot(ctx.pet_brain, first)

        self.assertTrue(ctx.init_pet_brain(PetBrainConfig(enabled=False)))
        self.assertIsNone(ctx.pet_brain)


class FakeTTSManager:
    def __init__(self):
        self.actions = []

    async def speak(
        self, tts_text, display_text, actions, live2d_model, tts_engine, websocket_send
    ):
        self.actions.append(actions)


class SentenceOutputHookTests(unittest.TestCase):
    def speak_once(self, actions, pet_brain):
        tts = FakeTTSManager()
        output = SentenceOutput(DisplayText(text="hi"), "hi", actions)
        run(
            handle_sentence_output(output, FAKE_MODEL, None, None, tts, None, pet_brain)
        )
        return tts.actions[0]

    def test_disabled_passes_identical_object(self):
        actions = Actions(expressions=[99])
        self.assertIs(self.speak_once(actions, None), actions)

    def test_enabled_gates_expressions(self):
        brain = PetBrain(PetBrainConfig(enabled=True))
        self.assertEqual(
            self.speak_once(Actions(expressions=[99]), brain).expressions, [0]
        )


class RecordingTTSManager:
    """Captures what would be sent to the frontend, plus the brain's emotion at that moment."""

    def __init__(self, brain):
        self.brain = brain
        self.sent = []

    async def speak(
        self, tts_text, display_text, actions, live2d_model, tts_engine, websocket_send
    ):
        payload = prepare_audio_payload(
            audio_path=None, display_text=display_text, actions=actions
        )
        emotion = self.brain.emotion.current_emotion if self.brain else None
        self.sent.append((display_text.text, payload, emotion))


# 6
class LLMTextToPayloadTests(unittest.TestCase):
    """LLM tokens -> the real BasicMemoryAgent decorator chain -> handle_sentence_output -> payload."""

    TOKENS = [
        "[smirk] Hehe, ",
        "got you. ",
        "[joy] That was fun! ",
        "[happy] Anyway. ",
        "No tag here.",
    ]

    def run_pipeline(self, brain):
        @tts_filter(TEMPLATE_TTS_PREPROCESSOR)
        @display_processor()
        @actions_extractor(MAO_MODEL)
        @sentence_divider(
            faster_first_response=False, segment_method="regex", valid_tags=["think"]
        )
        async def fake_llm():
            for token in self.TOKENS:
                yield token

        tts = RecordingTTSManager(brain)

        async def drive():
            async for output in fake_llm():
                await handle_sentence_output(
                    output, MAO_MODEL, None, None, tts, None, brain
                )

        run(drive())
        return tts.sent

    def test_emotion_names_and_payload(self):
        sent = self.run_pipeline(PetBrain(PetBrainConfig(enabled=True)))
        by_text = {
            text: (payload["actions"], emotion) for text, payload, emotion in sent
        }

        self.assertEqual(
            by_text["[smirk] Hehe, got you."], ({"expressions": [3]}, "smirk")
        )
        self.assertEqual(by_text["[joy] That was fun!"], ({"expressions": [3]}, "joy"))
        # Unknown tag: ignored, no expression, previous emotion kept.
        self.assertEqual(by_text["[happy] Anyway."], ({}, "joy"))
        self.assertEqual(by_text["No tag here."], ({}, "joy"))

    def test_enabled_payloads_identical_to_disabled(self):
        disabled = self.run_pipeline(None)
        enabled = self.run_pipeline(PetBrain(PetBrainConfig(enabled=True)))
        self.assertEqual(
            [(text, payload) for text, payload, _ in disabled],
            [(text, payload) for text, payload, _ in enabled],
        )


if __name__ == "__main__":
    unittest.main()
