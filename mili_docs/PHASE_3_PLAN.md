# Phase 3A Desktop Pet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mili moves around the desktop in Pet Mode by herself (wander, edge, home, contextual approach), reacts to click / double-click / drag, and plays idle motions. The backend decides *what*, the Electron frontend decides *where and how*, and the LLM is not involved.

**Architecture:**
- **Backend:** a pure `PetSelector` plus a pet lane at the end of the existing 20 s `BehaviorScheduler` tick, sending `pet-command` messages only to clients that sent `pet-hello`.
- **Frontend (separate repo `D:\Open-LLM-VTuber-Web`):** moves the Live2D model **inside** the full-virtual-screen pet window, the same mechanism as the existing drag. It uses pure geometry / planner / motion modules, a thin controller with one rAF loop only while moving, main-process IPC for display layout, cursor position and rect conversion, and a tray checkbox.

**Tech Stack:**
- Backend: Python 3.10, asyncio, FastAPI websockets, pydantic, ctypes Win32, loguru, stdlib `unittest`.
- Frontend: Electron 31, electron-vite, React 18, TypeScript, Cubism WebSDK, vitest (new devDependency).

**Spec:** `mili_docs/PHASE_3_SPEC.md`. Read it before any task. The § references below point to it.

## Global Constraints

- Windows-only. Backend Win32 access is `ctypes` only; **no new Python dependency**. **Never** call `GetWindowText*`.
- Python 3.10: no `asyncio.timeout`, no `TaskGroup`.
- Backend tests: stdlib `unittest` in `tests/test_desktop_pet_phase3.py`. Run with `uv run python -m unittest tests.test_desktop_pet_phase3 -v`. Inject clock, sensor and `random.Random`; no real sleeps longer than a few ms.
- Phase 1 + 2 suites stay green: `uv run python -m unittest tests.test_pet_brain tests.test_behavior_phase2` (228 tests).
- `ruff check .` must be clean, and `ruff format` clean on every changed backend file.
- Frontend: `npm run typecheck`, `npm run lint` and `npx vitest run` pass. The only new dependency is `vitest` (dev).
- Do not touch:
  - the backend `frontend/` submodule, `conf.yaml`, ASR/TTS/VAD, MCP, agents, memory, `upgrade_codes/`;
  - `src/renderer/WebSDK/**` in the frontend.
- Both `conf.default.yaml` and `conf.ZH.default.yaml` get every new key. `pet_brain_config.enabled: False` stays the default.
- A client that never sent `pet-hello` receives byte-identical Phase 2 output (spec §8.6).
- Log tags:
  - backend: `[Pet]`, `[Motion]`, `[Context]`, `[Emotion]`;
  - frontend console: `[Pet]`, `[Motion]`.
- Commits:
  - Backend commits go to `D:\Open-LLM-VTuber` on `mili-dev`; frontend commits go to `D:\Open-LLM-VTuber-Web` on `mili-dev`.
  - Never push, amend or force.
  - End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Protocol values, command names, config keys and defaults are exactly those in spec §5 and §10.

## Review Focus

1. **A `pet-status` result is lost** (frontend reloaded mid-move, websocket reconnect). Expect: the backend clears the pending command after `command_timeout_s` (60) and movement resumes. Test: Task 5 `test_pending_command_times_out`.
2. **Mili walks away from a cursor that is not moving.** Expect: within ~100 ms the window stops catching the mouse, so a click on the old spot reaches the app below. Tests: Task 8 `refreshHover sends false when model moved away` and E2E 2.
3. **A display is removed or the home position is off-screen** (laptop undocked). Expect: `go_home` is rejected with `home_invalid`, home is reset to the current position, and no target lies outside any work area. Test: Task 7 `go_home rejects and resets when home is on no display`.
4. **Malformed or hostile websocket input** (wrong types, huge numbers, unknown command, 100 clicks/s). Expect: dropped with a debug log, no exception, rate-limited to 10/s. Tests: Task 4 `test_parse_rejects_bad_types`, Task 5 `test_interaction_rate_limit`, Task 6 `parsePetCommand rejects unknown command and bad params`.
5. **User drag or conversation start during a move.** Expect: the move is cancelled at once, exactly one `cancelled` result is sent, and no further automatic motion happens while dragging. Test: Task 8 `drag cancels move and reports once`.

---

## File Structure

**Backend (`D:\Open-LLM-VTuber`)**

| File | Responsibility |
|---|---|
| `src/open_llm_vtuber/config_manager/pet_brain.py` (modify) | `DesktopPetConfig` and its sub-configs; attached as `PetBrainConfig.desktop_pet` |
| `config_templates/conf.default.yaml`, `conf.ZH.default.yaml` (modify) | `desktop_pet` section |
| `src/open_llm_vtuber/pet_brain/events.py`, `mood.py`, `pet_brain.py` (modify) | `PET_CLICKED`, `PET_SPAMMED`, `PET_POKED`, `PET_DRAGGED`; effects; `ensure_active` on `PET_POKED` |
| `src/open_llm_vtuber/pet_brain/emotion_manager.py` (modify) | `EmotionSource.INTERACTION`, `apply_reaction()` |
| `src/open_llm_vtuber/live2d_model.py` (modify) | `motion_map` loaded from `model_dict.json` |
| `src/open_llm_vtuber/pet_brain/context.py` (modify) | `foreground_rect` with a DPI-aware read |
| `src/open_llm_vtuber/pet_brain/desktop_pet.py` (new) | message parsing, `PetPresence`, `PetSelector`, `choose_reaction` — all pure |
| `src/open_llm_vtuber/pet_brain/scheduler.py` (modify) | pet presences, pet lane, timeout, interaction handling, sending |
| `src/open_llm_vtuber/websocket_handler.py` (modify) | register `pet-hello`, `pet-status`, `pet-interaction` |
| `model_dict.json` (modify) | `motionMap` for `mao_pro` |
| `tests/test_desktop_pet_phase3.py` (new) | all backend Phase 3 tests |

**Frontend (`D:\Open-LLM-VTuber-Web`)**

| File | Responsibility |
|---|---|
| `package.json`, `vitest.config.ts` (new) | vitest, `test` script, version `1.2.1-mili.1` |
| `src/renderer/src/pet/protocol.ts` | types and `parsePetCommand` |
| `src/renderer/src/pet/geometry.ts` | coordinate conversions, model bounds, clamping |
| `src/renderer/src/pet/motion.ts` | easing, duration, bob |
| `src/renderer/src/pet/pet-settings.ts` | movement toggle and home in localStorage |
| `src/renderer/src/pet/planner.ts` | target selection per command |
| `src/renderer/src/pet/desktop-pet-controller.ts` | state machine, move loop, hover refresh, results |
| `src/renderer/src/pet/hover-store.ts` | shared model-hover flag (zustand, like `use-force-ignore-mouse.ts`) |
| `src/renderer/src/pet/use-desktop-pet.ts` | React wiring |
| `src/renderer/src/pet/__tests__/*.test.ts` | vitest |
| `src/main/pet-ipc.ts` (new), `src/main/index.ts`, `src/main/menu-manager.ts`, `src/preload/index.ts`, `src/preload/index.d.ts` (modify) | IPC and menu checkbox |
| `src/renderer/src/hooks/canvas/use-live2d-model.ts`, `src/renderer/src/components/canvas/live2d.tsx`, `src/renderer/src/services/websocket-handler.tsx` (modify) | interactions, mounting, message routing |

---

### Task 0: Frontend repository and toolchain setup

**Blocked on the user:** the GitHub fork `Votuan12345/Open-LLM-VTuber-Web` must exist, and Node.js 20 or 22 LTS must be installed. Do not create either one yourself.

**Files:**
- Modify: `D:\Open-LLM-VTuber-Web\package.json`
- Create: `D:\Open-LLM-VTuber-Web\vitest.config.ts`, `src/renderer/src/pet/__tests__/smoke.test.ts`

**Interfaces:**
- Produces: `npm test` → `vitest run`; the alias `@` resolves to `src/renderer/src` in tests.

- [ ] **Step 1: Verify the prerequisites.** Run `git ls-remote https://github.com/Votuan12345/Open-LLM-VTuber-Web` (expect refs) and `node --version` (expect `v20.*` or `v22.*`). If either fails, stop and ask the user.
- [ ] **Step 2: Remotes and branch.** In `D:\Open-LLM-VTuber-Web`: `git remote add origin https://github.com/Votuan12345/Open-LLM-VTuber-Web.git`, then `git checkout -b mili-dev`. Expect `git remote -v` to show `origin` and `upstream`.
- [ ] **Step 3: Install dependencies.** Run `npm ci`, then `npm run typecheck` and record the result as the baseline. Pre-existing upstream errors are recorded, not fixed.
- [ ] **Step 4: Add vitest.**
  - `npm i -D vitest`.
  - Add the `"test": "vitest run"` script.
  - Set `"version": "1.2.1-mili.1"`.
  - `vitest.config.ts`: `environment: 'node'`, `include: ['src/renderer/src/pet/__tests__/**/*.test.ts']`, alias `@` → `src/renderer/src`.
- [ ] **Step 5: Smoke test.** Write `smoke.test.ts` with `expect(1 + 1).toBe(2)`. Run `npm test`; expect 1 passed.
- [ ] **Step 6: Commit** (frontend repo): `chore(mili): add vitest and mili version tag`.

---

### Task 1: Backend `desktop_pet` configuration

**Files:**
- Modify: `src/open_llm_vtuber/config_manager/pet_brain.py`, `config_templates/conf.default.yaml`, `config_templates/conf.ZH.default.yaml`
- Test: `tests/test_desktop_pet_phase3.py`

**Interfaces:**
- Produces:
  - `PetMovementConfig(enabled, min_interval_min, max_per_hour, chance, wander_threshold, post_conversation_quiet_min, command_timeout_s)`
  - `PetContextualConfig(enabled, categories: List[Category], min_curiosity, cooldown_min)`
  - `PetInteractionConfig(enabled, reaction_cooldown_s, spam_clicks, spam_window_s, drag_pause_min, attention_pause_min, reactions: Dict[ReactionKey, str])`
  - `PetIdleMotionConfig(enabled, min_interval_min, max_per_hour, chance)`
  - `DesktopPetConfig(enabled, movement, contextual, interaction, idle_motion)`
  - `PetBrainConfig.desktop_pet`
  - `ReactionKey = Literal["click_happy", "click_neutral", "spam", "double_click", "drag_playful", "drag_annoyed"]`
  - Defaults and validation are exactly those in spec §10. Every class has `DESCRIPTIONS` (en + zh), like the Phase 2 classes.

- [ ] **Step 1: Write the failing tests** in the new `tests/test_desktop_pet_phase3.py`, class `ConfigTests`:
  - `test_defaults`: `PetBrainConfig().desktop_pet.movement.min_interval_min == 4`, `.max_per_hour == 8`, `.chance == 0.4`, `.wander_threshold == 0.35`, `.command_timeout_s == 60`; `contextual.categories == ["coding", "unity"]`; `interaction.reactions["drag_playful"] == "smirk"`; `idle_motion.chance == 0.3`.
  - `test_rejects_bad_values`: `ValidationError` for `chance=1.5`, `spam_clicks=1`, `categories=["games"]`, `reactions={"poke": "joy"}`, `min_interval_min=-1`.
  - `test_templates_validate`: load both templates the way Phase 2 tests do; `desktop_pet.enabled is True` and `pet_brain_config.enabled is False`.
  - `test_desktop_pet_change_updates_in_place`: `ServiceContext.init_pet_brain` with a changed `desktop_pet.movement.chance` returns False and `brain.config.desktop_pet.movement.chance` equals the new value. The existing comparison in `service_context.py:394-412` already does this; the test pins it.
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_desktop_pet_phase3 -v`. Expect FAIL (`AttributeError: desktop_pet`).
- [ ] **Step 3: Implement** the config classes and add the `desktop_pet` block from spec §10 to both templates under `pet_brain_config`, with one-line comments (the ZH template uses Chinese comments).
- [ ] **Step 4: Run** the new suite plus the Phase 1 + 2 suites. Expect all PASS. Run `ruff check .`.
- [ ] **Step 5: Commit** `feat(pet_brain): add Phase 3 desktop_pet config`.

---

### Task 2: Interaction events, reaction expressions and motion map

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/events.py`, `mood.py`, `pet_brain.py`, `emotion_manager.py`, `src/open_llm_vtuber/live2d_model.py`
- Test: `tests/test_desktop_pet_phase3.py`

**Interfaces:**
- Produces:
  - `BrainEvent.PET_CLICKED | PET_SPAMMED | PET_POKED | PET_DRAGGED` (values `"pet_clicked"`, `"pet_spammed"`, `"pet_poked"`, `"pet_dragged"`);
  - `EmotionSource.INTERACTION = "interaction"`;
  - `EmotionManager.apply_reaction(name: str, live2d_model: Any) -> Optional[Actions]`;
  - `Live2dModel.motion_map: Dict[str, Dict[str, Any]]` with entries `{"group": str, "index": int}`.

- [ ] **Step 1: Write the failing tests**, class `InteractionPrimitiveTests`:
  - `test_event_effects`: the effects exactly as in spec §6.5, e.g. after `PET_CLICKED`, starting from the default mood, social_need is −0.03, boredom −0.05, happiness +0.02 (clamped); `PET_SPAMMED` gives happiness −0.05; `PET_POKED` gives social_need −0.05 and curiosity +0.05; `PET_DRAGGED` gives boredom −0.05.
  - `test_poked_wakes_from_sleep`: lifecycle forced to `SLEEP`, `notify(PET_POKED)` → phase `ACTIVE`. `notify(PET_CLICKED)` from `SLEEP` leaves it `SLEEP`.
  - `test_pet_events_do_not_change_activity`: activity is unchanged after each of the four events.
  - `test_apply_reaction`: with the real `mao_pro` model, `apply_reaction("joy", model).expressions == [3]` and `snapshot()["emotion_source"] == "interaction"`; an unknown name → `None`, with no exception.
  - `test_motion_map_loading`: with a temporary model_dict whose `motionMap` is `{"yawn": {"group": "", "index": 3}, "bad": {"group": 1, "index": -1}}`, the result is `motion_map == {"yawn": {"group": "", "index": 3}}` and a warning is logged. With no `motionMap` → `{}`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
  - Events and `EVENT_EFFECTS` entries.
  - `PetBrain.notify`: extend the `ensure_active` tuple with `PET_POKED`.
  - `apply_reaction`: same body shape as `apply_idle`, but with `EmotionSource.INTERACTION`; factor out a private helper so the two share code.
  - `motion_map`: built in `set_model()` from `model_info.get("motionMap", {})`, keeping only entries where `group` is a `str` and `index` is an `int ≥ 0`.
- [ ] **Step 4: Run** all three suites. Expect PASS; then `ruff`.
- [ ] **Step 5: Commit** `feat(pet_brain): pet interaction events, reaction expressions, motion map`.

---

### Task 3: Foreground window rectangle in the context sensor

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/context.py`
- Test: `tests/test_desktop_pet_phase3.py`

**Interfaces:**
- Produces:
  - `ContextSnapshot.foreground_rect: Optional[Tuple[int, int, int, int]] = None` (left, top, right, bottom, physical pixels). It is the **last** field and has a default, so existing constructors keep working.
  - `WindowsContextSensor._read_foreground_rect() -> Optional[Tuple[int, int, int, int]]`.
  - `OWN_PROCESS_NAMES = frozenset({"open-llm-vtuber-electron.exe", "electron.exe"})`.

- [ ] **Step 1: Write the failing tests**, class `ForegroundRectTests`, using a fake `user32`/`kernel32` injected the same way as the Phase 2 sensor tests:
  - `test_rect_read`: fake `GetWindowRect` returns (10, 20, 810, 620) → `sample().foreground_rect == (10, 20, 810, 620)`.
  - `test_rect_none_cases`: minimized (`IsIconic` → 1), desktop or shell hwnd, an empty rect, process `open-llm-vtuber-electron.exe`, and a `GetWindowRect` failure. Each gives `None`, while the other fields are still filled.
  - `test_dpi_context_restored`: the fake `SetThreadDpiAwarenessContext` records its calls; after a read that raises, the last call restores the previous value. The first call's argument is `-4`.
  - `test_unknown_has_no_rect`: `ContextSnapshot.unknown(now).foreground_rect is None`.
  - `test_no_window_text_api`: the source of `context.py` contains no `GetWindowText` (same check as Phase 2).
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
  - Declare `SetThreadDpiAwarenessContext` (argtypes `[c_void_p]`, restype `c_void_p`) and `IsIconic` in `_get_user32()`. If `SetThreadDpiAwarenessContext` is missing (older Windows), read without switching.
  - Reuse `_read_process_name()` for the own-process check.
  - `sample()` wraps the rect read in the same try/except-to-`None` pattern as the other fields.
- [ ] **Step 4: Run.** Expect PASS; Phase 2 context tests still pass. Then `ruff`.
- [ ] **Step 5: Commit** `feat(pet_brain): foreground window rect for contextual movement (no titles)`.

---

### Task 4: `desktop_pet.py` — parsing, presence, selector, reactions

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/desktop_pet.py`
- Test: `tests/test_desktop_pet_phase3.py`

**Interfaces:**
- Consumes: `DesktopPetConfig` (Task 1), `LifecyclePhase`, `ContextSnapshot.foreground_rect` (Task 3).
- Produces:
  - `PROTOCOL_VERSION = 1`
  - `PetHello(protocol: int, mode: str, movement_enabled: bool)`, `PetStatus(mode, movement_enabled, moving: bool, anchor: Optional[str], command_id: Optional[str], result: Optional[str], reason: Optional[str])`, `PetInteraction(kind: str, hit_area: Optional[str])` — frozen dataclasses
  - `parse_pet_message(data: dict) -> Optional[PetHello | PetStatus | PetInteraction]`
  - `PetPresence` — dataclass with the fields in spec §6.1. Defaults: `mode="window"`, `movement_enabled=False`, `moving=False`, `anchor="free"`, deques empty, times `None`.
  - `PetKind(str, Enum)`: `STAY, WANDER, GO_EDGE, GO_HOME, APPROACH_WINDOW, IDLE_MOTION`
  - `PetDecision(kind: PetKind, reason: str, params: Dict[str, Any] = {})` — frozen
  - `PetSelectionInput(now: float, cfg: DesktopPetConfig, presence: PetPresence, mood: Mapping[str, float], lifecycle: LifecyclePhase, context: ContextSnapshot, category: Optional[str], conversation_active: bool, last_conversation_end: Optional[float], motion_map: Mapping[str, Mapping[str, Any]])`
  - `PetSelector(rng: random.Random).select(inp: PetSelectionInput) -> PetDecision`
  - `choose_reaction(kind: str, mood: Mapping[str, float], spam: bool) -> Optional[str]` (returns a `ReactionKey` or `None` for `drag_start`)
  - `movement_command(decision: PetDecision) -> Optional[Tuple[str, Dict[str, Any]]]` → `("wander", {"distance": ..., "speed": ...})`, `("go_edge", {"side": "nearest"})`, `("go_home", {})`, or `("approach_rect", {"rect": {"x", "y", "width", "height"}})`; `IDLE_MOTION` → `("play_motion", {"group", "index"})`; `STAY` → `None`

- [ ] **Step 1: Write the failing parser tests**, class `ParseTests`:
  - `test_parse_valid`: one valid message of each type → the typed object.
  - `test_parse_rejects_bad_types`: `protocol="1"`, `mode="desk"`, `movement_enabled="yes"`, `kind="kick"`, `hit_area=5`, `result="done"`, a `command_id` longer than 64 characters, and a missing `type` → `None`, with no exception.
  - `test_unknown_fields_ignored`.
- [ ] **Step 2: Write the failing selector tests**, class `SelectorTests`. Use a builder `make_input(**overrides)` whose defaults make a WANDER possible: the pet-eligible presence has anchor `free`; mood has boredom 0.8, curiosity 0.5, energy 0.7, sleepiness 0.1, happiness 0.6; lifecycle `ACTIVE`; context has idle 300, fullscreen False, category `browser`; the RNG is seeded so that `random() < 0.4`. The tests:
  - `test_vetoes`: each of the eight vetoes in spec §6.3 in order gives `STAY` with reasons `disabled`, `moving`, `conversation`, `post_conversation`, `paused`, `lifecycle_sleep`, `lifecycle_away`, `fullscreen`, `context_unknown`.
  - `test_user_returned_goes_home`: `returned_pending=True`, anchor `free` → `GO_HOME` even when the frequency gates fail. With anchor `home` → no `GO_HOME`.
  - `test_contextual_approach`: `category_changed=True`, category `unity`, curiosity 0.6, rect set → `APPROACH_WINDOW` with `params["rect"]`. It is blocked by: curiosity 0.4, a missing rect, `last_contextual` within 30 min, category `browser`, or `contextual.enabled=False`.
  - `test_busy_user_goes_edge_then_stays`: category `coding`, idle 5, anchor `free`, last move 90 s ago → `GO_EDGE (user_busy)`. Anchor `edge` → `STAY (user_busy)`. Last move 30 s ago → `STAY`.
  - `test_frequency_gates`: last move 3 min ago → not `WANDER`; 8 moves in the last hour → not `WANDER`.
  - `test_sleepy_goes_edge`: sleepiness 0.8, anchor `free` → `GO_EDGE (sleepy)`.
  - `test_too_lazy`: energy 0.1 → not `WANDER`.
  - `test_wander_distance_and_speed`: energy 0.7 → `long`/`normal`; energy 0.45 → `short`/`normal`; energy 0.3 → `short`/`slow`. Score below 0.35 → no wander.
  - `test_idle_motion`: sleepiness 0.65 (below 0.7), boredom 0.1, `motion_map={"yawn": {...}}` → `IDLE_MOTION` with `params == {"group": "", "index": 3, "name": "yawn"}`. An empty map → `STAY (nothing_to_do)`.
  - `test_selector_does_not_mutate_presence` (deepcopy comparison).
- [ ] **Step 3: Write the failing reaction tests**, class `ReactionTests`:
  - `click` with happiness 0.6 → `click_happy`; with 0.4 → `click_neutral`; with `spam=True` → `spam`;
  - `double_click` → `double_click`;
  - `drag_end` → `drag_playful` for happiness 0.6 / energy 0.5, and `drag_annoyed` for 0.6 / 0.3;
  - `drag_start` → `None`.
  - `test_movement_command_mapping` for every `PetKind`.
- [ ] **Step 4: Run.** Expect FAIL (module missing).
- [ ] **Step 5: Implement** `desktop_pet.py` following spec §6.1 and §6.3 exactly.
  - Rule order and reason strings are those in the spec.
  - `wander` score: `0.5*boredom + 0.3*curiosity + 0.2*energy - 0.4*sleepiness`.
  - Busy categories: `{"coding", "unity", "office"}`; busy idle threshold: 60 s; `GO_EDGE (user_busy)` needs ≥ 60 s since the last move.
  - Idle-motion mood rules are in the order yawn → stretch → look_around. Frequency gates count timestamps within the window without pruning, like Phase 2's `_count_within_window`.
- [ ] **Step 6: Run.** Expect PASS; then `ruff`.
- [ ] **Step 7: Commit** `feat(pet_brain): desktop pet selector, message parser and reactions`.

---

### Task 5: Scheduler pet lane, interactions and websocket handlers

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/scheduler.py`, `src/open_llm_vtuber/websocket_handler.py`
- Test: `tests/test_desktop_pet_phase3.py`

**Interfaces:**
- Consumes: everything in Task 4; `EmotionManager.apply_reaction` and the `BrainEvent`s from Task 2; `Live2dModel.motion_map`.
- Produces:
  - on `BehaviorScheduler`:
    - `pet_presences: Dict[str, PetPresence]`;
    - `async handle_pet_message(uid: str, data: dict) -> None` — the single entry point for all three message types;
    - `evaluate_pet(uid: str) -> PetDecision` — synchronous;
    - `async _dispatch_pet(uid: str, decision: PetDecision) -> None`;
    - `async _send_pet_command(uid: str, command: str, params: dict) -> Optional[str]` — returns the command id `"c-<n>"`;
    - `async _handle_interaction(uid: str, msg: PetInteraction) -> None`.
  - In `websocket_handler.py`, `"pet-hello"`, `"pet-status"` and `"pet-interaction"` map to one `_handle_pet_message`, which awaits `self.behavior_scheduler.handle_pet_message(client_uid, data)`.

- [ ] **Step 1: Write the failing tests**, class `PetLaneTests`. Use the same fake-websocket / fake-context harness as `tests/test_behavior_phase2.py`, with `desktop_pet` enabled, `tick_seconds` irrelevant, `tick_once()` called directly and an injected clock.
  - `test_no_hello_no_commands`: a handled client without `pet-hello` receives exactly the same sent messages across 5 ticks as with `desktop_pet.enabled=False`.
  - `test_hello_then_wander_sends_command`: `pet-hello` (pet, movement true) with a wander-favoring mood → one `pet-command` `wander` whose `id` starts with `c-`; `presence.moving` is True.
  - `test_status_result_clears_pending`: `pet-status` with a matching `command_id` and `result=arrived`, `anchor=edge` → `moving` False, `anchor` `edge`, `last_move` set. A non-matching id → still pending.
  - `test_pending_command_times_out`: no result, the clock is advanced 61 s → the next tick clears the pending command (warning logged) and can send again.
  - `test_lane_skipped_when_conversation_running_or_reserved`.
  - `test_category_changed_and_returned_flags`: sensor category `browser` → `unity` sets `category_changed` for that tick only; a Phase 2 `user_returned` sets `returned_pending`, which is cleared once `GO_HOME` is dispatched.
  - `test_disconnect_drops_pet_presence`.
  - `test_disabled_backend_ignores_pet_messages`: with PetBrain disabled, `handle_pet_message` returns without error and nothing is sent.
- [ ] **Step 2: Write the failing interaction tests**, class `PetInteractionTests`:
  - `test_click_reaction_expression`: sends a silent payload with `actions.expressions == [3]` (`joy` on `mao_pro` at happiness ≥ 0.5); mood changes; `ClientPresence.last_user_interaction` is set.
  - `test_reaction_cooldown`: a second click within 3 s → mood only, no payload.
  - `test_click_spam`: the 5th click within 10 s gives the `spam` → `anger` expression (index 2) and the `PET_SPAMMED` mood effect.
  - `test_double_click_wakes_stops_and_pauses`: lifecycle SLEEP → ACTIVE; a `pet-command stop` is sent if a move is pending; `paused_until == now + 180`.
  - `test_drag_end_pauses_and_sets_home_anchor`: `paused_until == now + 300`; `anchor == "home"`.
  - `test_no_expression_during_conversation_or_sleep`: mood changes, but no payload.
  - `test_interaction_rate_limit`: 25 messages in the same second → at most 10 are processed.
  - `test_idle_motion_dispatch`: the selector returns `IDLE_MOTION` → `pet-command play_motion` with the group and index; `moving` stays False.
- [ ] **Step 3: Run.** Expect FAIL.
- [ ] **Step 4: Implement.**
  - At the end of `tick_once()`: for each handled uid with a pet presence, update `category_changed`/`last_category`, expire timeouts, then `evaluate_pet` → `_dispatch_pet`.
  - Set `returned_pending` where Phase 2 sets `returned_bonus_pending`.
  - `pet_presences.pop(uid)` in `client_disconnected`.
  - Sends are wrapped like `_dispatch_idle` (log and swallow).
  - The reaction expression uses `prepare_audio_payload(audio_path=None, display_text=None, actions=...)`.
  - Rate limit: a per-uid deque of timestamps, 1 s window, max 10.
  - Log lines as in spec §9.
- [ ] **Step 5: Run** the new suite plus the Phase 1 + 2 suites. Expect PASS; then `ruff check .`.
- [ ] **Step 6: Commit** `feat(pet_brain): desktop pet lane, interactions and pet websocket messages`.

---

### Task 6: Frontend pure modules — protocol, geometry, motion, settings

**Files:**
- Create: `src/renderer/src/pet/protocol.ts`, `geometry.ts`, `motion.ts`, `pet-settings.ts`, `__tests__/protocol.test.ts`, `__tests__/geometry.test.ts`, `__tests__/motion.test.ts`, `__tests__/pet-settings.test.ts`

**Interfaces:**
- Produces:
  - `protocol.ts`:
    - `PROTOCOL_VERSION = 1`;
    - `type Anchor = 'home'|'edge'|'free'`;
    - `type PetCommand = {id: string} & ({command:'wander', params:{distance:'short'|'long', speed:'slow'|'normal'}} | {command:'go_edge', params:{side:'nearest'|'left'|'right'}} | {command:'go_home', params:{}} | {command:'approach_rect', params:{rect: Rect}} | {command:'stop', params:{}} | {command:'play_motion', params:{group:string, index:number}})`;
    - `parsePetCommand(msg: unknown): {ok: true, cmd: PetCommand} | {ok: false, id?: string, reason: 'unknown_command'|'bad_params'}`;
    - builders `helloMsg(mode, movementEnabled)`, `statusMsg(fields)`, `interactionMsg(kind, hitArea)`.
  - `geometry.ts`:
    - `interface Point {x:number;y:number}`, `interface Rect {x:number;y:number;width:number;height:number}`;
    - `interface Layout {windowOrigin: Point; displays: {id:number; bounds:Rect; workArea:Rect; scaleFactor:number}[]}`;
    - `screenToClient(p: Point, origin: Point): Point`, `clientToScreen(...)`;
    - `clientToDevice(p, dpr)`, `deviceToClient(p, dpr)`;
    - `deviceToLogical(p, canvasW, canvasH)` and `logicalToDevice(...)` — must match `LAppView.initialize()`: scale `2*ratio/width` for width > height, else `2/height`, y flipped, centre at half size;
    - `unionBounds(vertices: Float32Array[], m: number[]): {minX,minY,maxX,maxY}` — model bounds in logical space;
    - `displayFor(center: Point, layout: Layout)`;
    - `shrink(rect, px)`;
    - `clampBoxToArea(box: Rect, area: Rect): Rect | null` — null when the box is bigger than the area.
  - `motion.ts`: `easeInOutSine(t)`, `durationFor(distanceDip, speed: 'slow'|'normal'): number` (ms; 60 / 120 DIP/s; clamped to 800–8000), `bobOffset(t01): number` (`4*|sin(2π·2·tSec)|` scaled to 0 at t = 0 and t = 1; the exact shape is free as long as the tests hold).
  - `pet-settings.ts`: `createPetSettings(storage: Pick<Storage,'getItem'|'setItem'> | null)` → `{getMovementEnabled(): boolean; setMovementEnabled(v): void; getHome(): Point|null; setHome(p: Point): void}`; keys `mili.pet.movementEnabled` and `mili.pet.home`.

- [ ] **Step 1: Write the failing tests.**
  - `protocol.test.ts`:
    - `parsePetCommand accepts each valid command`;
    - `parsePetCommand rejects unknown command and bad params` — `command:'fly'` → `unknown_command` with `id` kept; `distance:'far'`, a negative `index`, a rect with NaN, a non-string id → `bad_params`;
    - the builders produce the exact JSON of spec §5.1.
  - `geometry.test.ts`:
    - a round trip screen → client → device → logical → device → client → screen within 1e-6 for dpr 1, 1.25 and 1.5, with origin (−1920, 0);
    - `deviceToLogical` of the canvas centre is (0, 0), and of the top-left is (−ratio, 1) when width > height;
    - `clampBoxToArea` moves a box that sticks out back inside and returns null when the box is too big;
    - `displayFor` picks the display containing the point, or the nearest one when none contains it.
  - `motion.test.ts`: easing endpoints 0 and 1 and monotonic; `durationFor(120, 'normal') == 1000`; `durationFor(10, 'slow') == 800`; `durationFor(5000, 'normal') == 8000`; `bobOffset(0) == 0 == bobOffset(1)` and `|bob| ≤ 4`.
  - `pet-settings.test.ts`: defaults (true, null); round trip; a storage whose methods throw → defaults and no throw; a `null` storage works in memory.
- [ ] **Step 2: Run** `npm test`. Expect FAIL (modules missing).
- [ ] **Step 3: Implement** the four modules with no imports from React, Electron or the WebSDK.
- [ ] **Step 4: Run** `npm test && npm run typecheck`. Expect PASS.
- [ ] **Step 5: Commit** (frontend) `feat(pet): protocol, geometry, motion and settings modules`.

---

### Task 7: Frontend planner

**Files:**
- Create: `src/renderer/src/pet/planner.ts`, `__tests__/planner.test.ts`

**Interfaces:**
- Consumes: Task 6 types.
- Produces:
  - `interface PlanState {box: Rect /* model box in screen DIP */; home: Point|null}` — `home` is the box's top-left.
  - `type Plan = {ok: true, target: Point /* box top-left */, anchor: Anchor} | {ok: false, reason: 'no_room'|'home_invalid'|'invalid_rect'}`.
  - `planTarget(cmd: PetCommand /* movement commands only */, state: PlanState, layout: Layout, rng: () => number, rectDip?: Rect|null): Plan`.
  - Constants `MARGIN = 8`, `SHORT = [80, 250]`, `LONG = [250, 600]`, `MAX_VERTICAL_RATIO = 0.3`, `MIN_MOVE = 20`.

- [ ] **Step 1: Write the failing tests**, with a single 1920×1080 display (workArea height 1040) and a second display at x = −1920:
  - `wander stays inside the shrunk work area` for 200 seeded rng runs; the distance is within the range unless clamped.
  - `wander tries the opposite direction when blocked`: the box is at the right edge and the rng points right → the target is to the left.
  - `wander returns no_room when the box is bigger than the area`.
  - `go_edge nearest picks the closer side and keeps y`, with anchor `edge`.
  - `go_home returns home with anchor home`.
  - `go_home rejects and resets when home is on no display` → `home_invalid`.
  - `approach_rect targets the rect bottom-right, clamped`, with anchor `free`; a null rect → `invalid_rect`.
  - `targets use the display containing the box centre` (the box on the second display stays there).
- [ ] **Step 2: Run** `npm test`. Expect FAIL.
- [ ] **Step 3: Implement `planTarget`** following spec §7.3. The "reset home" side effect is **not** in the planner; the controller does it on `home_invalid`.
- [ ] **Step 4: Run** `npm test && npm run typecheck`. Expect PASS.
- [ ] **Step 5: Commit** (frontend) `feat(pet): movement planner`.

---

### Task 8: Main-process IPC, menu checkbox and the controller

**Files:**
- Create: `src/main/pet-ipc.ts`, `src/renderer/src/pet/hover-store.ts`, `src/renderer/src/pet/desktop-pet-controller.ts`, `__tests__/controller.test.ts`
- Modify: `src/main/index.ts`, `src/main/menu-manager.ts`, `src/preload/index.ts`, `src/preload/index.d.ts`

**Interfaces:**
- Consumes: Tasks 6–7.
- Produces:
  - `registerPetIpc(getWindow: () => BrowserWindow | null): void` — the handles `pet:get-layout`, `pet:get-cursor` and `pet:screen-to-dip-rect`, plus `pet:layout-changed` pushes on screen events.
  - `window.api.pet = {getLayout(): Promise<Layout>; getCursor(): Promise<Point>; screenToDipRect(r: Rect): Promise<Rect|null>; onLayoutChanged(cb): () => void; onToggleMovement(cb): () => void; reportMovementState(enabled: boolean): void}`.
  - `MenuManager`: an **Autonomous Movement** checkbox in both the tray menu and the pet context menu. Clicking it sends `pet:toggle-movement`; `ipcMain.on('pet:movement-state')` updates the checked state.
  - `useModelHoverStore` (zustand): `{hovering: boolean; setHovering(v: boolean): void}`.
  - `class DesktopPetController`:
    - constructor `(deps: ControllerDeps)`, where `ControllerDeps = {send(msg: object): void; getLayout(): Promise<Layout>; getCursor(): Promise<Point>; screenToDipRect(r: Rect): Promise<Rect|null>; model: ModelPort; settings: PetSettings; rng(): number; now(): number; raf(cb): number; cancelRaf(id): void; setHover(hit: boolean): void}` and `ModelPort = {getBoxDip(): Rect|null; getPositionLogical(): Point|null; setPositionLogical(p: Point): void; dipDeltaToLogical(d: Point): Point; hitTestScreen(p: Point): boolean; hasMotion(group: string, index: number): boolean; startMotion(group: string, index: number): void}`;
    - methods `setMode(mode)`, `setMovementEnabled(v)`, `setAiBusy(v: boolean)`, `onDragStart()`, `onDragEnd()`, `handleCommand(msg: unknown): Promise<void>`, `hello(): void`, `dispose()`;
    - `state: 'disabled'|'idle'|'moving'|'dragging'`.

- [ ] **Step 1: Write the failing controller tests** (vitest, fake deps, fake rAF driven manually, fake clock):
  - `rejects when not pet mode / movement disabled / ai busy / dragging` with the exact reasons of spec §7.7, one `pet-status` result each;
  - `wander moves along eased path and reports arrived once`: `setPositionLogical` is called on each frame and the final position equals the target; exactly one result `arrived` with `anchor`;
  - `drag cancels move and reports once`: `onDragStart()` mid-move → `cancelled` / `user_drag`, no more `setPositionLogical` calls; `onDragEnd()` saves home and sends `pet-status` with `anchor: 'home'`;
  - `new command supersedes running one` → the old id is `cancelled` / `superseded`;
  - `stop cancels with reason stopped`;
  - `setAiBusy(true) mid-move cancels with conversation`;
  - `go_home with invalid home resets home to current box` → the result is `rejected` / `home_invalid`;
  - `refreshHover sends false when model moved away`: the cursor is fixed at the start point, `hitTestScreen` becomes false as the model leaves → `setHover(false)` is called once, and not more often than every 100 ms;
  - `play_motion plays when present, logs fallback when missing`, with no `pet-status`;
  - `unknown command reports rejected unknown_command`;
  - `model disappears mid-move cancels with model_reloaded`.
- [ ] **Step 2: Run** `npm test`. Expect FAIL.
- [ ] **Step 3: Implement** `pet-ipc.ts` (with `screen.screenToDipRect(null, rect)` inside try/catch → `null`), the preload bindings and typings, the menu checkbox, `hover-store.ts` and the controller.
  - The move loop: interpolate the box top-left in DIP with `easeInOutSine`, add `bobOffset` to y, and convert the DIP delta from the start position into a logical delta applied to the start logical position.
  - The hover refresh uses `now()` throttling at 100 ms plus a final check after arrival or cancel.
- [ ] **Step 4: Run** `npm test && npm run typecheck && npm run lint`. Expect PASS.
- [ ] **Step 5: Commit** (frontend) `feat(pet): desktop pet controller, main-process IPC and movement toggle`.

---

### Task 9: Wire the controller into the renderer

**Files:**
- Create: `src/renderer/src/pet/use-desktop-pet.ts`
- Modify: `src/renderer/src/hooks/canvas/use-live2d-model.ts`, `src/renderer/src/components/canvas/live2d.tsx`, `src/renderer/src/services/websocket-handler.tsx`

**Interfaces:**
- Consumes: `DesktopPetController`, `useModelHoverStore`, `window.api.pet` (Task 8).
- Produces:
  - `useDesktopPet(): void`, mounted once in `Live2D`;
  - a module-level `petBus` (tiny event emitter in `use-desktop-pet.ts`) with `emitCommand(msg)`, `emitInteraction(kind, hitArea)`, `emitDrag('start'|'end')`, used by the websocket handler and `use-live2d-model.ts`.
  - `ModelPort` implemented over `getLAppAdapter()` and `LAppDelegate.getInstance().getView()`:
    - the box comes from `geometry.unionBounds` over the visible drawables' vertices and `_modelMatrix`, converted logical → device → client → screen;
    - `hitTestScreen` reuses the `anyhitTest || isHitOnModel` logic;
    - `hasMotion` uses `adapter.getMotionCount(group) > index`.

- [ ] **Step 1: Websocket routing.**
  - In `handleWebSocketMessage`, add `case 'pet-command': petBus.emitCommand(message); break;`.
  - After the socket opens and only when `window.api?.pet` exists, send `helloMsg(mode, movementEnabled)`. The hook subscribes to the ws state and sends it itself; `websocket-handler.tsx` only routes.
  - On `conversation-chain-start`, the hook sees `aiState` change to `thinking-speaking` and calls `setAiBusy(true)`.
- [ ] **Step 2: Interactions in `use-live2d-model.ts`.**
  - The tap branch → `petBus.emitInteraction('click' | 'double_click', hitAreaName)`, where a second tap within 350 ms of the previous one is a `double_click`.
  - The drag start and drag end branches → `petBus.emitDrag(...)`.
  - The hover branch writes through `useModelHoverStore.getState().setHovering()` and keeps sending `update-component-hover`.
  - All additions are guarded by `isPet`. Existing tap motion and drag behavior stay unchanged.
- [ ] **Step 3: The hook.**
  - Builds the controller with real deps; forwards mode, the AI state (`thinking-speaking`/`listening` → busy), the tray toggle (`onToggleMovement` → settings → `reportMovementState` → `pet-status`) and layout changes.
  - Sends `pet-interaction` only in Pet Mode when the ws is open.
  - Disposes everything on unmount.
- [ ] **Step 4: Verify.** Run `npm run typecheck && npm run lint && npm test`. Expect PASS. Then run `npm run dev` with the backend running and PetBrain disabled: the app works as before (window mode, pet mode, drag, tap motion, chat), and in the console `pet-hello` is sent with no errors.
- [ ] **Step 5: Commit** (frontend) `feat(pet): wire desktop pet into renderer, websocket and Live2D interactions`.

---

### Task 10: Motion map, E2E and handoff docs

**Files:**
- Modify: `model_dict.json`, `mili_docs/PROJECT_CONTEXT.md`
- Create: `mili_docs/PHASE_3A_SUMMARY.md`

- [ ] **Step 1: Motion check (manual, with the user).** In the dev app, open DevTools and run `Live2DDebug.getMotionInfo()` and `Live2DDebug.playMotion("", i)` for i = 0..5. Ask the user which motions look like `yawn`, `stretch` and `look_around`. Add only confirmed ones to `mao_pro.motionMap` in `model_dict.json`. Run the backend suites (the motion-map test uses its own temporary file).
- [ ] **Step 2: E2E.**
  - Setup: backend on port 12394 with in-memory overrides as in Phase 2 — PetBrain + behavior + desktop_pet enabled, `tick_seconds=2`, `movement.min_interval_min=0.1`, `chance=1.0`, boredom 0.9. The frontend runs with `npm run dev`, its ws URL set to 12394 in settings.
  - Run scenarios 1–8 of spec §12 and record pass/fail with log evidence.
  - Delete the harness afterwards.
- [ ] **Step 3: Build check.** Run `npm run build:win` and confirm the installer is produced. Do **not** install it; the user decides when to replace v1.2.1.
- [ ] **Step 4: Docs.**
  - Write `PHASE_3A_SUMMARY.md` using the Phase 2 summary structure: implemented, deferred, commits in both repos, tests, E2E, known issues, decisions D1–D6, TODOs for 3B, git state.
  - Update the phase status and "Current Priority" in `PROJECT_CONTEXT.md`.
- [ ] **Step 5: Commit** (backend) `docs(mili): Phase 3A summary and project status` (together with the `model_dict.json` change if it was not committed in Step 1).
