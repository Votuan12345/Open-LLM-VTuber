# PHASE_1_SUMMARY.md — Core Foundation

Status: **implemented and verified, not yet committed** (see [Git state](#git-state)).
Date: 2026-09-28. Branch: `mili-dev`.

This file records what Phase 1 did and why. The permanent project description stays in `PROJECT_CONTEXT.md`.

---

## 1. What was implemented

### New package `src/open_llm_vtuber/pet_brain/`

| Module | Responsibility |
|---|---|
| `pet_brain.py` | `PetBrain` core + `ActivityState` (`idle` / `listening` / `thinking` / `talking`). Receives `BrainEvent`s, updates activity, lifecycle and mood. `snapshot()` returns the full state for debugging. |
| `events.py` | `BrainEvent`: `user_input`, `proactive_trigger`, `response_start`, `response_end`, `interrupted`, `error`. |
| `mood.py` | Real mood state in code: `happiness, energy, curiosity, boredom, social_need, focus, sleepiness` in `[0, 1]`. Time drift is applied **lazily on access** (no background loop). Small data tables: `TREND_PER_HOUR`, `RELAX_PER_HOUR`, `EVENT_EFFECTS`. |
| `lifecycle.py` | `LifecyclePhase` state machine (`wake_up, active, idle, sleepy, sleep, away`) with an explicit allowed-transition table. Phase 1 only drives `wake_up → active` (and waking from `sleep`/`away` on user input). |
| `emotion_manager.py` | Gate in front of the existing Live2D expression pipeline. Validates expression indexes; keeps `current_emotion` by the **original LLM tag name**. |
| `permission.py` | Permission Core: `PermissionLevel` (READ / INTERACT / DESTRUCTIVE), `ToolRequest`, `PermissionDecision`, `PermissionGuard`, `ConfirmationProvider` (+ `DenyAllConfirmation`). |

### Config
- `config_manager/pet_brain.py`: `PetBrainConfig { enabled, emotion.fallback_emotion, permission.tool_levels }`.
- Attached as `character_config.pet_brain_config` with a default, so an existing `conf.yaml` without the section still validates.
- Added to **both** templates (`conf.default.yaml`, `conf.ZH.default.yaml`) with `enabled: False`. This is required because `upgrade.py` deletes user keys that are missing from the template.
- The template pre-classifies the tools of the two default MCP servers as `read`: `get_current_time`, `convert_time`, `search`, `fetch_content`.

### Integration hooks (small, all no-ops when disabled)
- **Permission boundary:** `ToolExecutor.run_single_tool()` calls `PermissionGuard.check()` right before `MCPClient.call_tool()`. That is the only place any MCP tool is executed.
- **Emotion gate:** `process_agent_output()` (in `handle_sentence_output` / `handle_audio_output`) calls `pet_brain.emotion.gate(actions, live2d_model)`. That is the last point before `actions` reach the websocket, for every agent type and for single + group conversations.
- **Conversation events:** `single_conversation.py` sends `user_input` / `proactive_trigger`, `response_start`, `response_end`, `interrupted`, `error`.
- **Ownership:** `ServiceContext.init_pet_brain()` creates, keeps or drops the brain in `load_from_config()`. `load_cache()` / `websocket_handler` pass it to cloned session contexts by reference, the same way `agent_engine` is shared.

### Single emotion parser
- `Live2dModel.parse_emotions(text) -> EmotionParse(tags, unknown)` is the **only** emotion-tag parser.
- Each `EmotionTag(name, expression)` keeps both the original tag name (`"smirk"`) and its expression index (`3`).
- `extract_emotion()` is now a thin wrapper over it. A test checks that its output is identical to the original algorithm.
- `actions_extractor` parses once and sets both:
  - `Actions.expressions = [3]`, the unchanged frontend contract;
  - `Actions.emotion = EmotionParse(...)`, a backend-only field that `Actions.to_dict()` excludes, so it never reaches the payload.

### Behavior when enabled

| Situation | Expressions sent | `current_emotion` | Source |
|---|---|---|---|
| Valid tag, e.g. `[smirk]` | unchanged (same object) | `smirk` | `llm_tag` |
| `[joy]` and `[smirk]` (same index 3) | `[3]` in both cases | `joy` / `smirk`, kept distinct | `llm_tag` |
| Unknown word tag, e.g. `[happy]` | unchanged (none) | unchanged | only a logged warning |
| Non-word brackets, e.g. `[1]`, `[some note]` | unchanged | unchanged | not treated as tags at all |
| Invalid expression index | dropped; `[neutral]` if none remain | `neutral` | `fallback_invalid_expression` |
| Valid index with no tag metadata | unchanged | `None` (not inferred) | `expression_only` |

- **Permission rules:**
  - READ / INTERACT are auto-allowed only when the tool is explicitly listed in `tool_levels`.
  - Any tool that is not listed is DESTRUCTIVE and needs confirmation. The default provider denies, so it is always denied for now.
  - If the permission check itself errors, the tool is denied (fail closed).
  - The level comes from config only. Nothing the LLM outputs can change it.
- **Logs:** `[PetBrain]`, `[Mood]` (debug), `[LifeCycle]`, `[Emotion]`, `[ToolRequest]`, `[Permission] ALLOWED/DENIED … [Reason] …`.

### Behavior when disabled (default)
- `context.pet_brain is None`, the ToolExecutor has no guard, and every hook short-circuits.
- The websocket payload is **identical** to before; this is verified by a test that compares enabled vs. disabled payloads.

---

## 2. Intentionally deferred

| Item | Deferred to | Reason |
|---|---|---|
| Behavior Scheduler, Idle Behavior | Phase 2 | Listed in spec §22 Phase 1, but the approved Phase 1 scope excluded background loops. Idle motions are currently frontend-driven (`idleMotionGroupName`). |
| Backend proactive-speaking engine | Phase 2 | Proactive speech is still the frontend's fixed idle timer that sends `ai-speak-signal`. |
| Daily rhythm, time-driven lifecycle (idle/sleepy/sleep/away) | Phase 2 | Needs the scheduler tick. |
| User activity / context awareness | Phase 2 | — |
| `listening` activity state (VAD hook) | Phase 2/4 | The enum value exists but nothing drives it yet. |
| Motion fallback | Phase 3/4 | The backend sends no motions today, so there is nothing to validate. |
| Real confirmation UI for DESTRUCTIVE tools | Phase 5 | The `ConfirmationProvider` interface is ready; the default denies. |
| Debug panel / state websocket message | Phase 7 | `PetBrain.snapshot()` exists but is not exposed to the frontend yet. |
| Desktop movement, mischief, frontend changes | Phase 3 | Needs the `Open-LLM-VTuber-Web` source fork. |

---

## 3. Files changed

**New**
- `src/open_llm_vtuber/pet_brain/` — `__init__.py`, `events.py`, `pet_brain.py`, `mood.py`, `lifecycle.py`, `emotion_manager.py`, `permission.py`
- `src/open_llm_vtuber/config_manager/pet_brain.py`
- `tests/test_pet_brain.py` (stdlib `unittest`, no new dependency)
- `mili_docs/PHASE_1_SUMMARY.md` (this file)

**Modified** (215 insertions / 17 deletions)
- `src/open_llm_vtuber/service_context.py` — `pet_brain` attribute, `init_pet_brain()`, the guard passed to `ToolExecutor`, `init_agent(force=...)`
- `src/open_llm_vtuber/mcpp/tool_executor.py` — `permission_guard` parameter + `_permission_denial()` before execution
- `src/open_llm_vtuber/live2d_model.py` — `parse_emotions()`; `extract_emotion()` now delegates to it
- `src/open_llm_vtuber/agent/output_types.py` — `EmotionTag`, `EmotionParse`, internal `Actions.emotion` (excluded from `to_dict()`)
- `src/open_llm_vtuber/agent/transformers.py` — `actions_extractor` uses `parse_emotions()` once
- `src/open_llm_vtuber/conversations/conversation_utils.py` — emotion gate
- `src/open_llm_vtuber/conversations/single_conversation.py` — brain events + passes `pet_brain`
- `src/open_llm_vtuber/conversations/group_conversation.py` — passes `pet_brain` (gate only, 1 line)
- `src/open_llm_vtuber/websocket_handler.py` — shares `pet_brain` with session contexts (1 line)
- `src/open_llm_vtuber/config_manager/character.py`, `config_manager/__init__.py`
- `config_templates/conf.default.yaml`, `config_templates/conf.ZH.default.yaml`

**Not touched:** frontend / submodule, `conf.yaml`, ASR / TTS / VAD, MCP registry and client, chat history, `upgrade_codes/`.

---

## 4. Tests and E2E results

**Unit tests: 43/43 pass**
```bash
uv run python -m unittest tests.test_pet_brain
```
The suite covers:
- config (templates validate, missing section → disabled, invalid permission level rejected);
- EmotionManager, run through the **real** `actions_extractor` with the real `mao_pro` model (where `joy` and `smirk` both map to 3);
- the single parser (identical to the original `extract_emotion` algorithm; the `emotion` field never serialized);
- PermissionGuard (whitelist, unlisted → denied, confirmation approve/fail);
- ToolExecutor (a denied tool never reaches `call_tool`; guard exception → fail closed; `tool_call_status` contract kept);
- Mood (lazy drift, clamping, event effects), Lifecycle transitions, PetBrain event sequence, `ServiceContext.init_pet_brain()` keep/replace/drop;
- the full path LLM tokens → real `BasicMemoryAgent` decorator chain → `handle_sentence_output` → `prepare_audio_payload`, with **enabled vs. disabled payloads identical**.

**Lint:** `ruff check .` is clean. `ruff format` is clean for all changed files. (`config_manager/asr.py` has a pre-existing format diff from upstream and was left untouched.)

**E2E with real Ollama** (a scratch harness ran the server on a spare port with the user's `conf.yaml` and toggled PetBrain **in memory only**; one real text turn through Ollama → edge-tts → websocket):

| Mode | Result |
|---|---|
| Pristine `HEAD` code | OK — baseline payload shape |
| PetBrain disabled | OK — `pet_brain=None`, no guard, same payload shape as HEAD |
| PetBrain enabled | OK — LLM emitted `[smirk]` → frontend got `{"expressions": [3]}`; `current_emotion="smirk"`, `source="llm_tag"`; the agent uses the guarded ToolExecutor; mood/lifecycle updated; activity back to `idle` |

---

## 5. Known issues / risks

- **Shared activity state:** concurrent sessions overwrite `PetBrain.activity`. Group conversation feeds several characters' emotions into one EmotionManager. This is fine for the current single user / single client (see TODO in `pet_brain.py`).
- **Character switch splits the brain:** after a `switch-config`, only that session points to the new brain.
- **`pet_brain_config` lives in `character_config`,** so a character YAML can enable or disable it (including permission enforcement).
- **Agent rebuild:** enabling, disabling or changing `pet_brain_config` rebuilds the agent so it picks up the new guarded ToolExecutor. That resets short-term conversation memory.
- **MCP tools must be whitelisted:** when enabled, every MCP tool not in `tool_levels` is denied. New MCP servers need entries.
- **The disabled path is not entirely untouched:** `parse_emotions()`, `actions_extractor` and the filtering in `Actions.to_dict()` run even when PetBrain is disabled. Output is proven identical by tests.
- **`Actions.emotion` must not leak:** any future code that serializes `Actions` with `asdict()` instead of `to_dict()` would expose it. No such code exists today.
- **`talking` is approximate:** it is set when the backend emits the first sentence, not when frontend playback starts.
- **Not E2E-tested:** a real LLM-initiated tool call being denied (non-deterministic). Covered by unit tests only.
- **One unexplained E2E hang:** one early E2E run with a Vietnamese prompt hung inside edge-tts generation. It was not reproducible (edge-tts works standalone and in later runs). With PetBrain disabled that path contains only no-op hooks.
- **Pre-existing upstream bugs, not fixed:**
  - `handle_audio_output` calls `actions.to_dict()` and then `prepare_audio_payload` calls `.to_dict()` again on the dict (Hume agent path only).
  - `TTSPreprocessorConfig()` cannot be built with defaults, so the fallback inside `tts_filter` would fail if it were ever reached.

---

## 6. Architecture decisions

1. **The permission check sits at the execution boundary**, inside `ToolExecutor.run_single_tool()`, not in prompts. The agent holds the default context's ToolExecutor, so the guard has to live inside the executor object the agent actually uses.
2. **Fail closed:** unlisted → DESTRUCTIVE → confirmation required; a checker error → deny. The default tool level is deliberately **not configurable**.
3. **The emotion gate is placed at `process_agent_output`**, not in `transformers.py`. It is the one choke point before the websocket that sees every agent type, and it has access to the session's `pet_brain`.
4. **Single emotion parser** (`Live2dModel.parse_emotions`). Metadata travels on `Actions.emotion`, which is excluded from serialization, so the frontend contract is unchanged.
5. **Unknown tags are ignored and logged.** Neutral fallback applies only to invalid expression indexes.
6. **PetBrain is shared, not per connection.** It is created in `load_from_config()` and shared by reference like `agent_engine`. It holds no websocket, session or history references, so it can later become a desktop-global instance.
7. **No background loop in Phase 1.** Mood drift is computed lazily from elapsed time.
8. **Activity, Mood and Lifecycle are separate concepts:** what Mili is doing, how she feels, and her high-level phase.
9. **Rollback by config:** `pet_brain_config.enabled: False` is the default.

---

## 7. TODOs for future phases

**Phase 2**
- A BehaviorScheduler with a low-frequency tick. Idle behavior can change expressions through the existing "silent payload" (`audio: null` + `actions`) without frontend changes.
- A backend proactive-speaking engine: cooldowns, `DO_NOTHING`, and mood/context/time gating. It must coordinate with, or replace, the frontend idle timer.
- Daily rhythm and time-driven lifecycle transitions; user activity and context awareness.
- A `listening` state driven by VAD speech detection.
- Ownership split: `CompanionBrain` (desktop-global: mood, lifecycle, emotion, permission policy) + `SessionPresence` (per session: activity). Consider moving `pet_brain_config` to the system level.
- Per-character emotion state for group conversations.

**Later**
- Primary/secondary emotion when a sentence has several tags (today the last valid tag wins).
- Phase 5: a real `ConfirmationProvider` (frontend confirmation dialog) and non-MCP desktop tools through the same `PermissionGuard.check()`.
- Phase 7: expose `PetBrain.snapshot()` through a debug websocket message or panel.

---

## 8. Git state

At the time of writing:
- Branch: `mili-dev`
- HEAD: `992309c docs(readme): Fix Trendshift badge link in README` (no Phase 1 commit yet)
- All Phase 1 work is **uncommitted**:
  - modified: the 13 files listed in §3;
  - untracked: `src/open_llm_vtuber/pet_brain/`, `src/open_llm_vtuber/config_manager/pet_brain.py`, `tests/`, `mili_docs/`.
- No remote, submodule or history changes were made.

**Disable / rollback**
- Instant: `pet_brain_config.enabled: False` (the default), or omit the section.
- Code: delete the new files and `git checkout` the 13 modified files; after committing, `git revert <phase-1 commit>`.

---

## 9. Deviations from the plan / `PROJECT_CONTEXT.md`

- **Behavior Scheduler / Idle Behavior** are in spec §22 Phase 1 but were deferred to Phase 2 by the approved scope.
- **Hook locations** differ from the first proposal: the permission check is in `run_single_tool()` rather than a generic executor wrapper, and the emotion gate is in `conversation_utils` rather than `transformers.py`. This was found necessary after reading the real agent/context sharing.
- **The Live2D emotion pipeline was touched** (`live2d_model.py`, `transformers.py`, `output_types.py`) to get a single parser. This was explicitly requested; the payload contract is unchanged.
- **PetBrain ownership is shared** rather than strictly per ServiceContext (approved).
- **Unknown emotion tags do not fall back to neutral.** Spec §7 "invalid emotion → neutral" is interpreted as invalid expression indexes only (approved).
- **A new `tests/` directory** was added (stdlib `unittest`, no new dependency).
