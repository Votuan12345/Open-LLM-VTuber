# PHASE_2_SUMMARY.md — Living Behavior

Status: **implemented, E2E-verified, committed on `mili-dev`.**
Date: 2026-09-28. Spec: `PHASE_2_SPEC.md`. Plan: `PHASE_2_PLAN.md`.

This file records what Phase 2 did and why. The permanent project description stays in `PROJECT_CONTEXT.md`.

---

## 1. What was implemented

Mili now decides **by herself, in deterministic code**, whether to speak proactively, show an idle expression, or do nothing. The LLM is only called after the scheduler has decided to speak.

### New modules in `src/open_llm_vtuber/pet_brain/`

| Module | Responsibility |
|---|---|
| `rhythm.py` | `DayPart` (morning 05–11, afternoon 11–17, evening 17–22, late_night 22–05) and `split_by_day_part()`. |
| `context.py` | `ContextSnapshot` with Optional fields (unknown ≠ 0/False). `WindowsContextSensor` (ctypes only): input idle, foreground process name, fullscreen. **Never reads window titles.** `NullContextSensor`, `classify_process()`. |
| `presence.py` | `ClientPresence` (per-client cooldowns, proactive history, ignored backoff, reservation, preemption flags) and `SchedulerState` (desktop-global). |
| `behavior.py` | Pure `BehaviorSelector`: veto order, frequency gates, willingness formula, idle-expression choice. `Decision` carries a reason string for logs. |
| `eligibility.py` | `ClientEligibility`: connected, handled (brain present + `behavior.enabled`), not in a group, not reserved or pending, no running task. Brain is resolved per client (`client_contexts[uid].pet_brain`). |
| `proactive_prompt.py` | Context block appended to the proactive prompt: time, day part, lifecycle, mood traits, `category (process.exe)`, idle minutes. No window title; unknown fields omitted. |
| `scheduler.py` | `BehaviorScheduler`: exactly one background task (starts on 0→1 clients, stops on 1→0). The tick runs Context → Daily Rhythm → Mood → Lifecycle → Behavior. `evaluate_client()` is the single decision path for both the tick and `ai-speak-signal`. Synchronous reserve-then-`create_task` commit. `preempt_for_user()` / `user_turn_registered()`. Idle expression dispatch. |

### Changes to existing modules
- `mood.py`: drift is integrated per DayPart segment using an anchor pair `(monotonic, wall)`. A wall-clock jump of more than 120 s skips the rhythm trend for that interval and logs it. A focus trend applies while the user is coding or in Unity. New event effects are added.
- `lifecycle.py`: `evaluate()` adds time-driven transitions: active→idle, idle→sleepy, sleepy→sleep, →away, and away→active on `user_returned`. `sleep` only wakes on user input.
- `pet_brain.py`: `tick(BrainTickInputs)`, a `wall_clock` parameter, and `update_config()`.
- `emotion_manager.py`: `apply_idle()` with source `idle_behavior`. A missing key returns None and never raises.
- `events.py`: `USER_RETURNED`, `PROACTIVE_SPOKEN`, `PROACTIVE_IGNORED`.
- `config_manager/pet_brain.py`: new `behavior`, `proactive`, `idle_expression` and `context` sections, with validation (probabilities in [0,1], `max_backoff_min >= min_interval_min`, known categories). `service_context.init_pet_brain()` updates Phase 2 config in place, with no brain or agent rebuild.
- `conversation_handler.py`:
  - adds `load_proactive_prompt()` (extracted verbatim) and `run_proactive_turn()`;
  - for scheduler-handled clients, `ai-speak-signal` becomes a request to the scheduler, and its legacy images are discarded;
  - user text or voice first `await preempt_for_user()`, then creates the user task, with `user_turn_registered()` in a `finally`.
- `websocket_handler.py`: owns the scheduler; adds connect / disconnect / failed-cleanup hooks.
- Both config templates gain the new sections, with `pet_brain_config.enabled: False` still the default.

### Behavior when disabled
If `pet_brain_config.enabled: False` (the default) or `behavior.enabled: False`, clients are not handled by the scheduler. `ai-speak-signal` then runs the legacy branch unchanged, including the frontend images and the "AI wants to speak something..." message. This is covered by tests.

---

## 2. Intentionally deferred

| Item | Deferred to | Reason |
|---|---|---|
| Motions, movement, desktop mischief | Phase 3 | Needs the frontend source fork. |
| `listening` state via VAD | Phase 4 | Not needed for Phase 2. |
| A `mili` category for the Mili window | Later | No verified way to detect the Electron process reliably. The observed name `open-llm-vtuber-electron.exe` could be used, but it is not in the defaults. |
| CompanionBrain / SessionPresence split of `PetBrain` | Later | Scheduler-side `ClientPresence` covers per-client state for Phase 2. |
| Settings UI, debug panel | Phase 7 | — |
| Upstream websocket send race (see §5) | Separate task | Not caused by Phase 2. |

---

## 3. Commits (all on `mili-dev`, none amended or pushed)

| Task | Commit | Subject |
|---|---|---|
| Spec / plan | `960a3b3`, `2093364`, `28bb431`, `10f8704` | spec, preemption fix, plan, plan tweaks |
| 1 | `b76356f` | Phase 2 config |
| 2 | `5161bd0` | daily rhythm day parts |
| 3 | `23cf3dd` | mood drift per day part + clock-jump policy |
| 4 | `c32574e` | Windows context sensor |
| 5 | `ebb9a0d` | time-driven lifecycle + `PetBrain.tick` |
| 6 | `493d3c0` | presence + behavior selector |
| 7 | `cced658` | eligibility + proactive context block |
| 8 | `be612e0` | scheduler lifecycle + tick pipeline |
| 9 | `1612ee6` | single evaluate path, atomic commit, idle expressions |
| 10 | `eff4072` | user preemption with unbounded wait |
| 11 | `75cef4b` | wiring into websocket / conversation handlers |
| 12 | this commit | summary + project status |

Diff for Tasks 1–11: 21 files, +4928 / −43. There are no changes to `frontend/` or `conf.yaml`.

---

## 4. Tests and E2E

**Unit tests: 228 / 228 pass**, run with `uv run python -m unittest tests.test_behavior_phase2 tests.test_pet_brain`. That is 185 Phase 2 tests in `tests/test_behavior_phase2.py` plus the 43 Phase 1 tests, unchanged. `ruff check .` is clean and `ruff format` is clean on the changed files.

Tasks 1–10 were each reviewed by an independent reviewer (spec + quality). Task 11 was implemented directly by the controller, at the user's request to cut token usage, and is covered by the final checklist review below.

**E2E.** The real server ran on spare port 12394 with the user's `conf.yaml`. PetBrain and behavior were enabled **in memory only**, with `tick_seconds=1`, `proactive.min_interval_min=0.2`, `post_conversation_quiet_min=0.1`, and `social_need`/`boredom` set to 0.95. The LLM was Ollama (`qwen2.5`) and TTS was edge-tts. The harness was scratch code and is deleted.

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Scheduler-initiated proactive | ✅ PASS | `[Proactive] committed` on the first eligible tick, then "AI wants to speak something..." and real audio. While Visual Studio (`coding`) or Unity was focused and the user was active, `user_busy` blocked it, as the spec requires. |
| 2 | `ai-speak-signal` right after a turn | ✅ PASS | `trigger=request → DO_NOTHING (post_conversation_quiet)`; nothing sent for 3 s. |
| 3 | Tick vs. request race | ✅ PASS | 34 requests in 2 s across the eligibility edge → exactly **1** proactive turn. One request won, and the rest saw `reserved` or `chance`. |
| 4 | Idle expression on the desktop frontend | ✅ PASS | Verified by the user on the desktop app. |
| 5 | Last client disconnects | ✅ PASS | `[Behavior] scheduler stopped`. |
| 6 | User input during a proactive turn | ✅ PASS | Instrumented timeline (below). |

**Case 6 timeline** (instrumented harness log, 17:15, desktop frontend):

```text
17:14:59.485  P#2 task START (proactive), SEND P#2 … 4 audio chunks until 17:15:11.035
17:15:11.133  frontend interrupt-signal → handle_individual_interrupt cancels P#2
17:15:11.144  P#2 task DONE cancelled=True
17:15:11.145  preempt_for_user START / END (proactive task already released)
17:15:11.145  U#1 task START ("Ê Mili"), SEND U#1 … until 17:15:26.607
```

- The proactive task was done before the user task started.
- No `SEND P#2` occurred after `U#1 START`.
- The user response ran after the preempt, and the output ranges do not overlap.
- On this frontend the proactive turn is cancelled by the frontend's own `interrupt-signal` (the existing handler; spec §8.6 allows it). `preempt_for_user()` then finds the task already released. The preempt branch that cancels by itself is verified by unit tests: cancel once, unbounded wait, the slow-cancellation invariant sampled throughout the wait, and concurrent preempts.

---

## 5. Known issues / risks

- **Upstream websocket send race (not Phase 2).** An `AssertionError` in `websockets/legacy/protocol.py` `_drain_helper` appeared in about 1 of 4 proactive E2E runs, and also on LLM-error replies.
  - Cause: `TTSTaskManager._sender_task` is still writing an audio frame while the conversation task sends `backend-synth-complete`. The two concurrent drains trip the assertion, and the turn ends with an empty "Conversation error".
  - The same code runs for user-initiated turns. Phase 2 does not touch it.
  - Kept as a separate task: "Fix concurrent websocket send race in TTSTaskManager".
- **Proactive reservation follows the upstream playback wait.** A proactive turn ends only after the frontend sends `frontend-playback-complete`, which upstream waits for without a timeout.
  - If the frontend never acknowledges, that client stays `reserved` and gets no further proactive turns until it reconnects or the user speaks (user input still preempts).
  - This was observed once in E2E after an LLM connection-error reply.
- **Harness-only settings.** Real config defaults are much slower on purpose: 20 s tick, 10 min interval, 3 min quiet. With default mood, Mili typically starts proactive speech only after about 30 min without interaction (spec §8.4).
- **Frontend timer.** Turn off `allowProactiveSpeak` in the frontend; the backend now owns proactive speech. If the timer stays on, its requests are refused and logged.
- **Process names** go into the local LLM prompt and debug logs; this was accepted. Window titles are never read.
- **Shared brain activity** (from Phase 1): one client talking blocks proactive speech for other clients on the same brain. This is intended.
- **Deferred review minors** (tracked, none load-bearing):
  - the YAML/Python category table is duplicated without an equality test;
  - race tests rely partly on cooldown;
  - `_select_and_dispatch` has unused parameters;
  - a few real 0.05–0.1 s sleeps in preempt tests.

---

## 6. Architecture decisions and rulings

1. **Backend owns proactive speech.** `ai-speak-signal` is only a request through the same `evaluate_client()` path. There is no separate proactive path.
2. **One evaluation path, synchronous commit.** Nothing is awaited between the eligibility re-check and `create_task`. Invariant: at most one conversation-producing behavior per client.
3. **Absolute no-overlap on preemption.** The user task is created only after the proactive task has really finished. The 5 s threshold is diagnostic only and the wait is unbounded.
4. **`PetBrain` keeps companion state only.** Per-client bookkeeping lives in `ClientPresence`, and brains are resolved per client (this follows `switch-config`).
5. **Fail closed on unknown context.** Unknown idle, category or fullscreen blocks proactive speech. Unknown values are never converted to 0/False.
6. **`tick_seconds` is a constructor argument, not YAML.** The scheduler is desktop-global, while config is per character.
7. **Rulings made during execution:**
   - `evaluate_client(..., allow_proactive=False)` limits the whole tick to one proactive turn, via `proactive_slot_taken`;
   - during the preempt wait the refusal reason may read `reserved` rather than `user_turn_pending`, because eligibility checks `reserved` first;
   - an unknown idle sample between away and back drops the `user_returned` bonus, which is fail-closed;
   - Task 6's missing RED evidence was accepted after the reviewer hand-verified the logic;
   - Tasks 2 and 3 were implemented and reviewed together;
   - Task 11 was implemented by the controller without a per-task review, at the user's request.

---

## 7. TODOs for later phases

- Fix the upstream websocket send race (separate task).
- Consider a playback-ack timeout for proactive turns only, if stuck `reserved` shows up in real use.
- A `mili` category once detection of the Mili window is verified.
- Phase 3: movement and motions through the frontend source fork. Phase 4: the `listening` state from VAD.
- Phase 7: expose `PetBrain.snapshot()` and scheduler decisions in a debug panel.

---

## 8. Git state

- Branch `mili-dev`, not pushed. No remote, submodule or history changes.
- Rollback:
  - `pet_brain_config.behavior.enabled: False` turns off Phase 2 only;
  - `pet_brain_config.enabled: False` (the default) turns off both phases;
  - in code, `git revert` the Phase 2 commits.
