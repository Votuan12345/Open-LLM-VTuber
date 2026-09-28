# Phase 2 Living Behavior Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend-owned behavior loop from the Phase 2 spec. Mili decides by herself, in deterministic code, whether to speak proactively, show an idle expression, or do nothing, using mood, daily rhythm, lifecycle and lightweight Windows context.

**Architecture:** One `BehaviorScheduler` per `WebSocketHandler` runs a single 20 s asyncio tick in this order: Context → Daily Rhythm → Mood → Lifecycle → Behavior.
- Pure units hold the rules: rhythm, selector, prompt, eligibility.
- `PetBrain` keeps companion state only.
- Per-client bookkeeping lives in `ClientPresence`.
- The periodic tick and the legacy `ai-speak-signal` share one synchronous `evaluate_client()`.
- A synchronous reserve-then-`create_task` commit plus unbounded user preemption keep the no-overlap invariant.

**Tech Stack:** Python 3.10, asyncio, FastAPI websockets (existing), pydantic config (existing), ctypes Win32, loguru, stdlib `unittest`.

**Spec:** `mili_docs/PHASE_2_SPEC.md` (commits `960a3b3`, `2093364`). Read it before any task. Section numbers below (§) refer to it.

## Global Constraints

- Windows-only project. `ctypes` is the only Win32 access; **no new dependency** (no pywin32/psutil imports).
- **Never** call `GetWindowText*`. Window titles are never read, prompted or logged.
- Python 3.10: no `asyncio.timeout`, no `TaskGroup`. Use `asyncio.wait`.
- Tests: stdlib `unittest`, all in `tests/test_behavior_phase2.py`. Run: `uv run python -m unittest tests.test_behavior_phase2 -v`. Inject clocks, sensors and `random.Random`; no real sleeps longer than a few ms.
- Phase 1 suite must stay green: `uv run python -m unittest tests.test_pet_brain -v` (43 tests).
- `ruff check .` clean; `ruff format` clean on every changed file.
- Do not touch: `frontend/` submodule, `conf.yaml`, ASR/TTS/VAD, MCP, agents, chat history, `upgrade_codes/`, or pre-existing upstream bugs (spec §1).
- Both `config_templates/conf.default.yaml` and `conf.ZH.default.yaml` get every new key.
- Log tags: `[Behavior]`, `[Proactive]`, `[Context]`, `[LifeCycle]`, `[Mood]`, `[Emotion]`.
- `pet_brain_config.enabled: False` stays the default. With PetBrain disabled or `behavior.enabled: False`, every existing path is byte-for-byte unchanged.
- Commit after each task on `mili-dev`. Never push, amend or force. End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Client disconnects mid-flight** (after a proactive commit, or during the preempt wait). Expect: no `KeyError`, the done-callback tolerates a missing presence, and the scheduler task stops when the last client leaves. Test: Task 10.
2. **The user-input handler is cancelled while awaiting `preempt_for_user`.** Expect: `user_turn_pending` is released (the handler calls `user_turn_registered(uid, None)` in `finally`), so proactive is not blocked forever. Test: Task 11.
3. **`switch-config` changes or disables a client's brain mid-run.** Expect: the next tick resolves the new brain through `client_contexts[uid]`, and a client whose new brain is `None` becomes unhandled with no error. Test: Task 9.
4. **The legacy frontend timer is left on** (an `ai-speak-signal` every few seconds). Expect: every request inside the cooldown is refused, no websocket output, no task created. Test: Task 9.
5. **Odd sensor values:** `GetTickCount` wrap-around, a process name with a path or different case. Expect: idle is never negative, and classification is basename-based and case-insensitive. Test: Task 4.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/open_llm_vtuber/config_manager/pet_brain.py` (modify) | `BehaviorConfig`, `ProactiveConfig`, `IdleExpressionConfig`, `ContextConfig` |
| `src/open_llm_vtuber/service_context.py` (modify) | `init_pet_brain()` updates Phase 2 config in place (no brain/agent rebuild) |
| `src/open_llm_vtuber/pet_brain/rhythm.py` (new) | `DayPart`, `day_part_at`, `split_by_day_part` |
| `src/open_llm_vtuber/pet_brain/mood.py` (modify) | anchor pair, per-DayPart integration, jump policy, focus trend, new event effects |
| `src/open_llm_vtuber/pet_brain/events.py` (modify) | `USER_RETURNED`, `PROACTIVE_SPOKEN`, `PROACTIVE_IGNORED` |
| `src/open_llm_vtuber/pet_brain/context.py` (new) | `ContextSnapshot`, sensors, `classify_process` |
| `src/open_llm_vtuber/pet_brain/lifecycle.py` (modify) | `LifecycleInputs`, `Lifecycle.evaluate()` |
| `src/open_llm_vtuber/pet_brain/pet_brain.py` (modify) | `BrainTickInputs`, `PetBrain.tick()`, `wall_clock` param |
| `src/open_llm_vtuber/pet_brain/emotion_manager.py` (modify) | `EmotionSource.IDLE_BEHAVIOR`, `apply_idle()` |
| `src/open_llm_vtuber/pet_brain/presence.py` (new) | `ClientPresence`, `SchedulerState`, `prune_window` |
| `src/open_llm_vtuber/pet_brain/behavior.py` (new) | `BehaviorKind`, `Trigger`, `Decision`, `SelectionInput`, `BehaviorSelector`, `willingness`, `choose_idle_emotion` |
| `src/open_llm_vtuber/pet_brain/eligibility.py` (new) | `ClientEligibility` |
| `src/open_llm_vtuber/pet_brain/proactive_prompt.py` (new) | `build_context_block` |
| `src/open_llm_vtuber/pet_brain/scheduler.py` (new) | `BehaviorScheduler` |
| `src/open_llm_vtuber/pet_brain/__init__.py` (modify) | export new public names, **not** `BehaviorScheduler` (import it from `.scheduler` directly to keep imports light) |
| `src/open_llm_vtuber/conversations/conversation_handler.py` (modify) | `run_proactive_turn`, `load_proactive_prompt`, scheduler routing and preemption |
| `src/open_llm_vtuber/websocket_handler.py` (modify) | own the scheduler; connect/disconnect/cleanup hooks; pass it to the trigger handler |
| `config_templates/conf.default.yaml`, `conf.ZH.default.yaml` (modify) | new sections |
| `tests/test_behavior_phase2.py` (new) | all Phase 2 tests |
| `mili_docs/PHASE_2_SUMMARY.md` (new, Task 12) | handoff |

---

### Task 1: Phase 2 configuration

**Files:**
- Modify: `src/open_llm_vtuber/config_manager/pet_brain.py`, `src/open_llm_vtuber/config_manager/__init__.py`, `src/open_llm_vtuber/service_context.py:385-399`, both templates
- Test: `tests/test_behavior_phase2.py` (create; header docstring `"""Phase 2 behavior tests. Run from repo root: uv run python -m unittest tests.test_behavior_phase2 -v"""`)

**Interfaces:**
- Produces:
  - `BehaviorConfig(enabled: bool = True)`
  - `ProactiveConfig(enabled=True, min_interval_min=10.0, post_conversation_quiet_min=3.0, max_per_hour=3, threshold=0.30, chance=0.35, ignored_after_min=5.0, max_backoff_min=60.0)`
  - `IdleExpressionConfig(enabled=True, min_interval_min=3.0, max_per_hour=10, chance=0.3)`
  - `ContextConfig(enabled=True, away_after_min=10.0, process_categories: Dict[str, Category])` where `Category = Literal["coding","unity","office","browser","media","gaming","unknown"]`, default = the spec §5.4 table
  - `PetBrainConfig` gains `behavior`, `proactive`, `idle_expression`, `context` (all `default_factory`)
  - `PetBrain.update_config(config: PetBrainConfig) -> None`, used by `init_pet_brain`
- Validation: probabilities and `threshold` use `Field(ge=0, le=1)`; intervals and counts use `ge=0`; `ProactiveConfig` has a `model_validator` rejecting `max_backoff_min < min_interval_min` (otherwise the verbatim backoff formula could undercut the minimum interval). Every field has an `I18nMixin` description (en + zh), following the existing classes.

- [ ] **Step 1: Write failing tests** in class `Phase2ConfigTests`:
  - `test_defaults` — `PetBrainConfig()` has the values listed above, and `process_categories["Code.exe"] == "coding"`.
  - `test_templates_validate_with_phase2_sections` — both templates validate; `character_config.pet_brain_config.behavior.enabled is True`; `proactive.min_interval_min == 10`.
  - `test_missing_sections_use_defaults` — `PetBrainConfig.model_validate({"enabled": True})` validates.
  - `test_invalid_values_rejected` — `chance: 1.5`, `threshold: -0.1`, a category `"work"`, and `min_interval_min: 10` with `max_backoff_min: 5` each raise `ValidationError`.
  - `test_phase2_change_keeps_brain_and_agent` — `ctx = ServiceContext(); ctx.init_pet_brain(cfg_a)`; `b = ctx.pet_brain`; `cfg_b` = `cfg_a` with `proactive.chance=0.9` → `init_pet_brain(cfg_b)` returns `False`, `ctx.pet_brain is b`, `b.config.proactive.chance == 0.9`.
  - `test_phase1_change_still_replaces_brain` — a changed `permission.tool_levels` returns `True` and gives a new instance.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_behavior_phase2 -v` → FAIL (import errors).
- [ ] **Step 3: Implement the config models and templates.** Template YAML goes under `pet_brain_config` with the exact keys and values of spec §11 and the full §5.4 category table. The ZH template uses the same keys and values, with Chinese comments.
- [ ] **Step 4: Implement the in-place update.** In `init_pet_brain`, when a brain exists and `(enabled, emotion, permission)` are equal, call `self.pet_brain.update_config(pet_brain_config)` and return `False`. The existing equal-config debug log path stays.
- [ ] **Step 5:** Run the Phase 2 tests and the Phase 1 suite → PASS. Run `ruff check .`.
- [ ] **Step 6: Commit** `feat(pet_brain): add Phase 2 behavior/proactive/idle/context config`

---

### Task 2: Daily rhythm segmentation

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/rhythm.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Produces:
  - `class DayPart(str, Enum)` with values `MORNING="morning"`, `AFTERNOON="afternoon"`, `EVENING="evening"`, `LATE_NIGHT="late_night"`
  - `BOUNDARY_HOURS = (5, 11, 17, 22)`
  - `day_part_at(t: datetime) -> DayPart`
  - `split_by_day_part(start: datetime, end: datetime) -> List[Tuple[DayPart, float]]` — seconds per segment in chronological order; `end <= start` → `[]`.

- [ ] **Step 1: Write failing tests** `RhythmTests`:
  - `day_part_at` at 04:59 → LATE_NIGHT, 05:00 → MORNING, 10:59 → MORNING, 11:00 → AFTERNOON, 17:00 → EVENING, 22:00 → LATE_NIGHT, 00:30 → LATE_NIGHT.
  - `split(04:40, 05:10)` → `[(LATE_NIGHT, 1200), (MORNING, 600)]`.
  - `split(21:30, 02:00 next day)` → `[(EVENING, 1800), (LATE_NIGHT, 14400)]` — the segment is **not** split at midnight.
  - A 30 h span: segment seconds sum to `30*3600`; adjacent segments never share a DayPart.
  - `split(t, t)` → `[]`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** Walk from `start`: next boundary = the smallest boundary hour today strictly after the current time, else 05:00 tomorrow. Merge adjacent segments with the same DayPart (22→05 crosses midnight).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): add daily rhythm day parts`

---

### Task 3: Mood timeline integration, clock-jump policy, new events

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/mood.py`, `src/open_llm_vtuber/pet_brain/events.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: `DayPart`, `split_by_day_part` (Task 2).
- Produces:
  - `Mood(initial=None, clock=time.monotonic, wall_clock: Callable[[], datetime] = datetime.now)`
  - attribute `Mood.focus_context_active: bool = False`
  - `BrainEvent.USER_RETURNED = "user_returned"`, `PROACTIVE_SPOKEN = "proactive_spoken"`, `PROACTIVE_IGNORED = "proactive_ignored"`
- Constants in `mood.py`:
  - `RHYTHM_TREND_PER_HOUR = {DayPart.MORNING: {"energy": 0.05}, DayPart.LATE_NIGHT: {"sleepiness": 0.15, "energy": -0.05}}`
  - `FOCUS_TREND_PER_HOUR = 0.2`
  - `CLOCK_JUMP_TOLERANCE_S = 120.0`
  - `EVENT_EFFECTS` += `USER_RETURNED: {"happiness": 0.05, "curiosity": 0.05}`, `PROACTIVE_SPOKEN: {"social_need": -0.10}`, `PROACTIVE_IGNORED: {"happiness": -0.03}`

**Algorithm** (the one non-obvious part of this task):

```text
advance():
  now_mono, now_wall = clock(), wall_clock()
  elapsed = now_mono - last_mono
  if elapsed <= 0: last_mono, last_wall = now_mono, now_wall; return
  if |(now_wall - last_wall).total_seconds() - elapsed| <= CLOCK_JUMP_TOLERANCE_S:
      segments = split_by_day_part(last_wall, last_wall + timedelta(seconds=elapsed))
  else:
      logger.info("[Mood] wall-clock jump detected (Δ=…s); rhythm trend skipped for this interval")
      segments = [(None, elapsed)]
  for day_part, seconds in segments:  _apply_segment(seconds, day_part)  # base trend + relax + rhythm (if day_part) + focus (if flag); clamp
  last_mono, last_wall = now_mono, now_wall
```

- [ ] **Step 1: Write failing tests** `MoodTimelineTests`, using `FakeClock` plus a `FakeWall` (a mutable `datetime`):
  - `test_crossing_five_am_splits_rates` — start 04:40, advance mono and wall by 30 min. Expected sleepiness = start + `0.02*0.5 + 0.15*(20/60)`. Expected energy = start `- 0.03*0.5 - 0.05*(20/60) + 0.05*(10/60)`. `assertAlmostEqual(places=6)`.
  - `test_long_equals_many_short` — 6 h from 20:00 in one advance vs. 72 advances of 5 min: every key within `0.01`.
  - `test_forward_jump_skips_rhythm` — at 23:00, mono +30 min, wall +2 h 30 min. Expected: sleepiness = start + `0.02*0.5` only (no late-night +0.15/h); a warning or info log contains `wall-clock jump`; a following advance of 10 min (both clocks) applies the late-night rate.
  - `test_backward_jump_skips_rhythm` — wall −1 h, mono +10 min → same policy.
  - `test_small_drift_is_continuous` — wall +10 min + 90 s, mono +10 min → rhythm applied.
  - `test_focus_trend_only_when_flag` — flag False: focus relaxes toward baseline. Flag True for 1 h from focus 0.3 → focus > 0.3.
  - `test_new_event_effects` — each new event changes the listed keys by the listed deltas.
  - `test_phase1_behavior_unchanged_in_afternoon` — at 13:00, 1 h: boredom +0.15, social_need +0.10 (Phase 1 values).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** per the algorithm and constants above.
- [ ] **Step 4:** Run Phase 2 + Phase 1 suites → PASS (Phase 1 `MoodTests` use `FakeClock` only; `wall_clock` defaults to `datetime.now`, so they keep passing).
- [ ] **Step 5: Commit** `feat(pet_brain): integrate mood drift per day part with clock-jump policy`

---

### Task 4: Context snapshot and Windows sensor

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/context.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ContextSnapshot(taken_at_wall: datetime, user_idle_seconds: Optional[float], process_name: Optional[str], fullscreen: Optional[bool])`, plus `@classmethod unknown(cls, now_wall: datetime) -> ContextSnapshot`
  - `class ContextSensor(Protocol): def sample(self) -> ContextSnapshot`
  - `NullContextSensor(wall_clock=datetime.now)` — always all-`None`
  - `WindowsContextSensor(wall_clock=datetime.now)`. Each of `_read_idle_seconds() -> float`, `_read_process_name() -> str`, `_read_fullscreen() -> bool` may raise; `sample()` catches **each separately**, logs at debug level, and sets only that field to `None`.
  - `idle_seconds_from_ticks(now_tick: int, last_input_tick: int) -> float` = `((now_tick - last_input_tick) & 0xFFFFFFFF) / 1000`
  - `classify_process(name: Optional[str], table: Mapping[str, str]) -> Optional[str]` — `None` → `None`; basename via `ntpath.basename`, `casefold()` match against the casefolded keys; no match → `"unknown"`
  - `make_default_sensor() -> ContextSensor` — `sys.platform == "win32"` and construction succeeds → Windows sensor; otherwise Null
- Win32 calls: `user32.GetLastInputInfo` (`LASTINPUTINFO`), `kernel32.GetTickCount`, `GetForegroundWindow`, `GetWindowThreadProcessId`, `kernel32.OpenProcess(0x1000, False, pid)`, `QueryFullProcessImageNameW`, `CloseHandle` (in `finally`). Fullscreen: `GetWindowRect`, `MonitorFromWindow(hwnd, 2)`, `GetMonitorInfoW` (`rcMonitor`). The foreground window equal to `GetDesktopWindow()` or `GetShellWindow()` → `False`. `hwnd == 0` → raise, so the field is `None`.
- `[Context]` log: debug level, only when `category(process)` changes. Emitted by the scheduler (Task 8), not the sensor.

- [ ] **Step 1: Write failing tests** `ContextTests`:
  - `idle_seconds_from_ticks(5000, 2000) == 3.0`; wrap: `idle_seconds_from_ticks(1000, 0xFFFFFFFF - 999) == 2.0`.
  - `classify_process("CODE.EXE", table) == "coding"`; `classify_process(r"C:\Program Files\Unity\Unity.exe", table) == "unity"`; `"notepad.exe"` → `"unknown"`; `None` → `None`.
  - Per-field failure: subclass `WindowsContextSensor` overriding `_read_process_name` to raise → snapshot has `process_name is None` while idle/fullscreen keep the values returned by the overridden readers (override all three in the subclass).
  - Idle failure → `user_idle_seconds is None` (not `0`).
  - `NullContextSensor().sample()` → all three `None`.
  - Source guard: `"GetWindowText" not in inspect.getsource(context_module)`.
  - `@unittest.skipUnless(sys.platform == "win32")` smoke test: the real `WindowsContextSensor().sample()` does not raise, and `user_idle_seconds` is `None` or `>= 0`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** with lazily created `ctypes.WinDLL` handles and explicit `argtypes`/`restype` for the calls above.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): add Windows context sensor (no window titles)`

---

### Task 5: Time-driven lifecycle and `PetBrain.tick`

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/lifecycle.py`, `src/open_llm_vtuber/pet_brain/pet_brain.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: `ContextSnapshot` (Task 4), `Mood.focus_context_active` and the new events (Task 3), `PetBrainConfig.context` (Task 1).
- Produces:
  - `@dataclass(frozen=True) LifecycleInputs(sleepiness: float, user_idle_seconds: Optional[float], quiet_seconds: float, conversation_running: bool, away_after_seconds: float, user_returned: bool)`
  - constants `ACTIVE_TO_IDLE_S = 120`, `SLEEPY_AT = 0.7`, `SLEEP_AT = 0.85`, `SLEEP_USER_IDLE_S = 900`
  - `Lifecycle.evaluate(inputs: LifecycleInputs) -> None`
  - `@dataclass(frozen=True) BrainTickInputs(context: ContextSnapshot, category: Optional[str], quiet_seconds: float, conversation_running: bool, user_returned: bool)`
  - `PetBrain(config, confirmation=None, clock=time.monotonic, wall_clock=datetime.now)`
  - `PetBrain.tick(inputs: BrainTickInputs) -> None`
  - `PetBrain.update_config(config)` (Task 1) is unchanged

**Rules**, from spec §7.2. The first matching rule applies and `evaluate` returns:
1. `user_returned` and phase `AWAY` → `WAKE_UP`, then `ACTIVE`.
2. Phase in {ACTIVE, IDLE, SLEEPY}, idle known and `>= away_after_seconds` → `AWAY`.
3. Phase in {ACTIVE, WAKE_UP}, `quiet_seconds >= 120` and not running → `IDLE`.
4. `IDLE` and `sleepiness >= 0.7` → `SLEEPY`.
5. `SLEEPY`, `sleepiness >= 0.85`, idle known and `>= 900` → `SLEEP`.

`SLEEP` is never left here; only the Phase 1 `ensure_active` on user input leaves it.

**`tick` order:**
1. `mood.advance()` — uses the previous tick's focus flag.
2. Set `mood.focus_context_active = category in {"coding","unity"} and idle is not None and idle < 120`.
3. If `user_returned`: `notify(USER_RETURNED)`.
4. `lifecycle.evaluate(...)` with `sleepiness` from `mood.snapshot()` and `away_after_seconds = config.context.away_after_min * 60`.

The **scheduler** computes `quiet_seconds` (Task 8). When it has no conversation timestamps, it uses `clock() - lifecycle.since`.

- [ ] **Step 1: Write failing tests** `LifecycleEvaluateTests` and `BrainTickTests`:
  - One test per rule 1–5.
  - Rule 2 does **not** fire with `user_idle_seconds=None`.
  - `SLEEP` + `user_returned` stays `SLEEP`; `SLEEP` + `notify(USER_INPUT)` → `ACTIVE` (via `WAKE_UP`).
  - Running conversation blocks `ACTIVE → IDLE`.
  - `test_tick_uses_mood_updated_this_tick`: brain in `IDLE`, sleepiness 0.69, wall 23:00. Advance the clocks 1 h and tick → `SLEEPY` in that same tick (sleepiness is now ≥ 0.7 from the late-night trend).
  - `test_tick_sets_focus_flag_after_advance`: coding + idle 10 → flag True after the tick, and focus rises on the *next* tick.
  - `test_user_returned_applies_event_and_wakes_from_away`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run Phase 2 + Phase 1 → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): time-driven lifecycle and PetBrain.tick`

---

### Task 6: Presence and behavior selector

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/presence.py`, `src/open_llm_vtuber/pet_brain/behavior.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: configs (Task 1), `DayPart`/`day_part_at` (Task 2), `ContextSnapshot` (Task 4), `ActivityState`, `LifecyclePhase`.
- Produces (`presence.py`):
  - `@dataclass ClientPresence` with fields `uid: str`, `connected_at: float`, `last_user_interaction: Optional[float] = None`, `last_conversation_end: Optional[float] = None`, `last_proactive: Optional[float] = None`, `proactive_timestamps: Deque[float]`, `ignored_count: int = 0`, `awaiting_reply_since: Optional[float] = None`, `last_idle_expression: Optional[float] = None`, `idle_expression_timestamps: Deque[float]`, `reserved: bool = False`, `proactive_task: Optional[asyncio.Task] = None`, `proactive_preempted: bool = False`, `user_turn_pending: bool = False`, `returned_bonus_pending: bool = False`, `returned_after_s: Optional[float] = None`
  - `@dataclass SchedulerState(last_context: Optional[ContextSnapshot] = None, previous_user_idle_seconds: Optional[float] = None, last_category_label: Optional[str] = None)`
  - `prune_window(ts: Deque[float], now: float, window_s: float = 3600.0) -> None`
- Produces (`behavior.py`):
  - `BehaviorKind(str, Enum)`: `DO_NOTHING`, `IDLE_EXPRESSION`, `PROACTIVE_SPEAK`
  - `Trigger(str, Enum)`: `TICK = "tick"`, `REQUEST = "request"`
  - `@dataclass(frozen=True) Decision(kind: BehaviorKind, reason: str, willingness: Optional[float] = None, emotion: Optional[str] = None)`
  - `@dataclass(frozen=True) SelectionInput(trigger, now: float, presence: ClientPresence, activity: ActivityState, lifecycle: LifecyclePhase, mood: Mapping[str, float], context: ContextSnapshot, category: Optional[str], config: PetBrainConfig, emo_map: Mapping[str, Any])`
  - `willingness(mood, category: str, day_part: DayPart, returned_bonus: bool) -> float`
  - `choose_idle_emotion(mood, emo_map) -> Optional[str]`
  - `effective_min_interval_s(cfg: ProactiveConfig, ignored_count: int) -> float` = `min(cfg.min_interval_min * 2**ignored_count, cfg.max_backoff_min) * 60` (spec formula verbatim; `max_backoff_min >= min_interval_min` is guaranteed by config validation, Task 1)
  - `BehaviorSelector(rng: random.Random)` with `select(inp: SelectionInput) -> Decision`
- Constants:
  - `WANT_WEIGHTS = {"social_need": 0.5, "boredom": 0.3, "curiosity": 0.2}`
  - `CATEGORY_MULT = {"coding": 0.3, "unity": 0.3, "office": 0.5, "media": 0.5, "browser": 1.0, "unknown": 0.8, "gaming": 0.0}`
  - `DAYPART_MULT = {MORNING: 1.0, AFTERNOON: 1.0, EVENING: 0.9, LATE_NIGHT: 0.5}`
  - `CODING_PAUSE_S = 120`, `RETURNED_BONUS = 0.2`
  - `IDLE_EMOTION_RULES = (("sleepiness", 0.6, "sleepy"), ("boredom", 0.6, "bored"), ("curiosity", 0.7, "curious"), ("happiness", 0.7, "joy"))`, then `"neutral"`

**`select` order.** Eligibility is checked by the scheduler before `select` is called. Reasons are short snake-case strings, used in logs and asserted by tests.
1. **Vetoes:**
   - `activity != IDLE` → `conversation_lock`
   - lifecycle in {SLEEP, AWAY} → `lifecycle_<phase>`
   - `fullscreen is True` → `fullscreen`
   - `fullscreen is None` → `context_unknown`
2. **Proactive stage.** Skipped with reason `proactive_disabled` if `proactive.enabled` is False. The first failing gate gives the reason:
   - idle `None` or category `None` → `context_unknown`
   - since `last_proactive` < effective interval → `cooldown`
   - since `max(last_conversation_end, last_user_interaction)` < quiet → `post_conversation_quiet`
   - after pruning, `len(proactive_timestamps) >= max_per_hour` → `hourly_cap`
   - category in {coding, unity} and idle < 120 → `user_busy`
   - `w = willingness(...)`; `w < threshold` → `low_willingness` (Decision carries `willingness=w`)
   - `rng.random() >= chance` → `chance` (carries `w`)
   - otherwise `PROACTIVE_SPEAK` (carries `w`)
3. **Idle stage** (only when `trigger is TICK` and the proactive stage did not select). With `idle_expression.enabled`, interval, hourly cap, then `choose_idle_emotion` (`None` → `no_expression_available`), then `rng.random() < chance` → `IDLE_EXPRESSION(emotion=…)`.
4. Otherwise `DO_NOTHING` with the proactive-stage reason.

`REQUEST` never produces `IDLE_EXPRESSION` (spec §13: a refusal sends nothing).

`choose_idle_emotion`: the first rule whose mood value is strictly greater than its threshold gives the preferred key; the candidates are `[preferred, "neutral"]` filtered by `key in emo_map`; return the first one or `None`.

- [ ] **Step 1: Write failing tests** `SelectorTests`, with a helper `make_input(**overrides)` that builds a passing baseline:
  - `activity=IDLE`, `ACTIVE`, fullscreen False, idle 600 s, category browser;
  - mood `social_need=0.9`, `boredom=0.9`, `curiosity=0.5`, `sleepiness=0.0`, `energy=1.0`;
  - wall 14:00, `rng` returning 0.0, empty presence.

  Tests:
  - Baseline → `PROACTIVE_SPEAK`.
  - One test per veto with its reason.
  - One test per gate with its reason. Include `cooldown` at 9 min vs. allowed at 10 min, and `hourly_cap` with 3 timestamps inside 60 min vs. allowed when one is 61 min old.
  - `effective_min_interval_s`: 0 → 600, 1 → 1200, 2 → 2400, 3 → 3600 (capped at 60 min), 5 → 3600.
  - `willingness` reference values: default mood, browser, afternoon ≈ 0.275 (`places=3`); mood after 30 min (`social_need=0.45`, `boredom=0.275`, `curiosity=0.5`, `sleepiness=0.21`, `energy=0.685`) ≈ 0.307 (`places=2`); gaming → 0; late_night halves the value; the returned bonus adds 0.2.
  - `user_busy`: coding with idle 60 → refused; idle 130 → allowed.
  - `REQUEST` with proactive refused and idle-eligible mood → `DO_NOTHING` (never `IDLE_EXPRESSION`).
  - Idle: `sleepiness=0.7` with `{"sleepy": 5, "neutral": 0}` → `sleepy`; `sleepy` missing but `neutral` present → `neutral`; both missing (`{"joy": 3}`) → `DO_NOTHING` / `no_expression_available` and **no exception**; `emo_map={}` → same.
  - Deterministic rng: `random.Random(1)` gives the same decision sequence across two runs.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): add client presence and deterministic behavior selector`

---

### Task 7: Eligibility policy and proactive context block

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/eligibility.py`, `src/open_llm_vtuber/pet_brain/proactive_prompt.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Produces:
  - `ClientEligibility(client_connections: Dict[str, Any], client_contexts: Dict[str, Any], current_conversation_tasks: Dict[str, Optional[asyncio.Task]], chat_group_manager: Any)` with:
    - `brain_for(uid) -> Optional[PetBrain]` — returns `client_contexts[uid].pet_brain` only if it is not `None` **and** `brain.config.behavior.enabled`
    - `handles(uid) -> bool` — `uid in client_connections and uid in client_contexts and brain_for(uid) is not None and not in_group(uid)`
    - `in_group(uid) -> bool` — the group exists and `len(group.members) > 1`
    - `check(uid, presence: Optional[ClientPresence]) -> Optional[str]` — `None` = eligible; otherwise one of `not_connected`, `not_handled`, `in_group`, `reserved`, `user_turn_pending`, `conversation_running`
    - `order(uids: Iterable[str], presences: Mapping[str, ClientPresence]) -> List[str]` — by `last_user_interaction` descending, `None` last, `uid` as the tie-break
  - `build_context_block(*, now_wall: datetime, day_part: DayPart, lifecycle: LifecyclePhase, mood: Mapping[str, float], category: Optional[str], process_name: Optional[str], user_idle_seconds: Optional[float], returned_after_s: Optional[float], trigger: Trigger) -> str`
- Exact copy for `build_context_block`. Lines whose data is `None` are omitted.

```text
[Context for this moment. Use it naturally; do not read it out.]
- Local time: {HH:MM} ({day_part.value})
- Your state: {lifecycle.value}; feeling {traits}
- User activity: {category} ({process_name}), idle {N} min
- The user just came back after {M} min away.
- Why you are speaking: {"you chose to" if TICK else "the app asked you to"}
```

- Traits: up to 3 mood keys with value `>= 0.6`, sorted by value descending, mapped via `{"happiness": "cheerful", "energy": "energetic", "curiosity": "curious", "boredom": "bored", "social_need": "like chatting", "focus": "focused", "sleepiness": "sleepy"}`. If none qualify → `"calm"`.
- The user-activity line appears only when `category` is not `None`. If the process name is unknown, the parenthesis is dropped.

- [ ] **Step 1: Write failing tests** `EligibilityTests` and `PromptTests`:
  - Each `check` reason.
  - A running task → `conversation_running`; a done task → eligible.
  - `order` puts the most recent interaction first.
  - A brain with `behavior.enabled=False` → not handled.
  - The prompt contains `14:05 (afternoon)` and `coding (Code.exe), idle 3 min`.
  - Unknown idle and category → no `User activity` line.
  - Traits are ordered by value.
  - No `window`/`title` substring ever appears in the output (assert over several inputs).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): add client eligibility policy and proactive context block`

---

### Task 8: BehaviorScheduler lifecycle and tick pipeline

**Files:**
- Create: `src/open_llm_vtuber/pet_brain/scheduler.py`
- Modify: `src/open_llm_vtuber/pet_brain/__init__.py` (export `ContextSnapshot`, `DayPart`, `BehaviorKind`, `Trigger`, `Decision`, `ClientPresence`; **not** the scheduler)
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces:
  - Constructor:
    ```python
    BehaviorScheduler(
        client_connections, client_contexts, current_conversation_tasks, chat_group_manager,
        run_proactive_turn: Callable[[Any, Callable[[str], Awaitable[None]], str, str], Awaitable[None]],
        sensor: Optional[ContextSensor] = None,   # None → make_default_sensor()
        tick_seconds: float = 20.0,
        preempt_warn_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    )
    ```
  - `run_proactive_turn(context, websocket_send, client_uid, context_block)` — injected to avoid an import cycle (Task 11 provides it).
  - `async client_connected(uid) -> None`, `async client_disconnected(uid) -> None`
  - `is_running: bool` (property), `presences: Dict[str, ClientPresence]`, `state: SchedulerState`, `eligibility: ClientEligibility`
  - `async tick_once() -> None`
  - `_select_and_dispatch(now, ctx)` — introduced here as `async def … -> None: return`. Task 9 fills it.

**Lifecycle.** An `asyncio.Lock` guards connect/disconnect:
- connect: add presence; if no task exists or it is done → `asyncio.create_task(self._run())`;
- disconnect: pop presence; if none remain and a task exists → `cancel()`, `await` it (suppress `CancelledError`), set `self._task = None`, reset `self.state = SchedulerState()`.

`_run` loops `await asyncio.sleep(tick_seconds)` then `tick_once()` inside `try/except Exception` → `logger.exception("[Behavior] tick failed")`. `CancelledError` propagates.

**`tick_once` pipeline** (spec §4.4, §7):
1. `handled = [uid for uid in presences if eligibility.handles(uid)]`. Empty → return **without sampling**.
2. `ctx = sensor.sample()`; store it in `state.last_context`; log `[Context] category(process)` at debug level when the label changes.
3. Distinct brains: `{id(b): b for b in (eligibility.brain_for(u) for u in handled)}`. For each brain:
   - `brain_ctx = ctx` if `brain.config.context.enabled`, else `ContextSnapshot.unknown(ctx.taken_at_wall)`;
   - `category = classify_process(brain_ctx.process_name, brain.config.context.process_categories)`;
   - `returned = prev_idle is not None and prev_idle >= away_s and brain_ctx.user_idle_seconds is not None and brain_ctx.user_idle_seconds < 30`;
   - `quiet_seconds` = `now - max(timestamps)` over the brain's clients, using `last_conversation_end` and `last_user_interaction`, or `now - brain.lifecycle.since` if none;
   - `conversation_running` = any of the brain's clients has a running task in `current_conversation_tasks` or `reserved`;
   - `brain.tick(BrainTickInputs(...))`;
   - if `returned`: set `returned_bonus_pending = True` and `returned_after_s = prev_idle` on those clients.
4. `state.previous_user_idle_seconds = ctx.user_idle_seconds`.
5. **Ignored detection** per handled presence: `awaiting_reply_since` set and `now - awaiting >= ignored_after_min*60` → `ignored_count += 1`, clear awaiting, `brain.notify(PROACTIVE_IGNORED)`, log `[Proactive] ignored count=… backoff=…min`.
6. `await self._select_and_dispatch(now, ctx)`.

- [ ] **Step 1: Write failing tests** `SchedulerLifecycleTests` and `TickPipelineTests`. Fixtures:
  - `FakeSensor(snapshot)` with a counter of `sample()` calls;
  - `FakeWS` with an `async send_text` that records messages;
  - fake contexts `SimpleNamespace(pet_brain=brain, live2d_model=SimpleNamespace(emo_map=...))`;
  - a group manager stub whose `get_client_group` returns `None`;
  - `run_proactive_turn` stub.

  Tests:
  - 0→1 creates exactly one task; a second `client_connected` → the same task object; 1→0 → `is_running` False and presences empty.
  - `asyncio.gather` of 5 connects and 5 disconnects (interleaved) → at most one live task at every point (instrument `_run` with a counter of concurrently running loops, asserted `<= 1`).
  - `brain` identity is unchanged across stop/start.
  - A `tick_once` that raises inside `_run` (patch `tick_once` to raise once) → the loop continues (`tick_seconds=0.001`, await a few ticks).
  - No handled clients → `sensor.sample` is not called.
  - Two clients sharing one brain → `brain.tick` called once per tick (wrap `tick` with a counter).
  - `user_returned`: prev idle 700, now 5, `away_after_min=10` → `USER_RETURNED` applied and `returned_bonus_pending` set; prev idle `None` → not.
  - `context.enabled=False` → the brain receives an all-unknown snapshot.
  - Ignored detection fires once at ≥ 5 min and increments `ignored_count`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): add BehaviorScheduler lifecycle and tick pipeline`

---

### Task 9: Single evaluation path, commit and idle dispatch

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/scheduler.py`, `src/open_llm_vtuber/pet_brain/emotion_manager.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: Task 8 scheduler; `BehaviorSelector` (Task 6); `build_context_block` (Task 7); `prepare_audio_payload` from `..utils.stream_audio`.
- Produces:
  - `EmotionSource.IDLE_BEHAVIOR = "idle_behavior"`
  - `EmotionManager.apply_idle(name: str, live2d_model) -> Optional[Actions]` — looks up `emo_map[name]`; missing → `None` (no raise); otherwise records with `IDLE_BEHAVIOR` and returns `Actions(expressions=[idx])`
  - `BehaviorScheduler.evaluate_client(uid: str, trigger: Trigger) -> Decision` — **synchronous**; the only decision path
  - `BehaviorScheduler.request_proactive(uid: str, images: Optional[list] = None) -> Decision` — logs `[Behavior] discarded N legacy proactive image(s)` at debug level when images are given, refreshes `state.last_context = sensor.sample()` synchronously, then returns `evaluate_client(uid, Trigger.REQUEST)`
  - `_select_and_dispatch(now, ctx)` — for `uid in eligibility.order(handled, presences)`: `d = evaluate_client(uid, Trigger.TICK)`; after the first `PROACTIVE_SPEAK`, the remaining clients are evaluated with proactive commits skipped (pass an internal flag); `IDLE_EXPRESSION` → `await _dispatch_idle(uid, d.emotion)`

**`evaluate_client` body.** Everything is synchronous; there is no `await` in this function.
1. `presence = presences.get(uid)`; `reason = eligibility.check(uid, presence)`; a reason → `Decision(DO_NOTHING, reason)`.
2. `brain = eligibility.brain_for(uid)`; `ctx` = `state.last_context`, or `ContextSnapshot.unknown(now)` when it is `None` or the brain's context is disabled; compute category.
3. `d = selector.select(SelectionInput(...))`. If `d.willingness is not None`, consume the returned bonus.
4. `PROACTIVE_SPEAK` → `self._commit_proactive(uid, brain, ctx, category, trigger)`. It returns `False` if the re-check fails, and then `d` becomes `DO_NOTHING("conversation_lock")`.
5. Log per spec §8.10: info when `trigger is REQUEST` or `d.kind != DO_NOTHING`, else debug.

**`_commit_proactive`**, synchronous, in this exact order:
1. Re-run `eligibility.check` and `brain.activity == IDLE`.
2. Set `reserved = True`, `last_proactive = now`, append to `proactive_timestamps`, `proactive_preempted = False`.
3. Build the block with `build_context_block`.
4. `task = asyncio.create_task(run_proactive_turn(context, ws.send_text, uid, block))`.
5. Store it in `current_conversation_tasks[uid]` and `presence.proactive_task`, and add the done-callback.
6. Log `[Proactive] committed uid=… willingness=… category=…(…)`.

**Done-callback:** gets the presence (it may be gone → return). Clears `reserved` and `proactive_task`, sets `last_conversation_end = clock()`. `normal = not task.cancelled() and task.exception() is None and not presence.proactive_preempted`. Resets `proactive_preempted`. If `normal`: `awaiting_reply_since = clock()` and `brain.notify(PROACTIVE_SPOKEN)`.

**`_dispatch_idle(uid, emotion)`:** `actions = brain.emotion.apply_idle(emotion, context.live2d_model)`; `None` → return. Otherwise record `last_idle_expression` and the timestamp, then `await ws.send_text(json.dumps(prepare_audio_payload(audio_path=None, display_text=None, actions=actions)))`.

- [ ] **Step 1: Write failing tests** `EvaluatePathTests`, `RaceTests` and `IdleDispatchTests`. Use a high-willingness mood, `rng=Random` stub returning 0.0 and fake sensor idle 600 / browser (`chrome.exe`) / not fullscreen:
  - `test_tick_commits_one_proactive` — after `tick_once()` the stub was started once; `current_conversation_tasks["a"]` is that task; `reserved` is True until it finishes.
  - `test_request_refused_under_cooldown_sends_nothing` — `last_proactive = now - 60` → `request_proactive("a")` is `DO_NOTHING/cooldown`; `FakeWS.sent == []`; no task.
  - `test_request_and_tick_share_gates` — the same presence setups (lock, hourly cap, low willingness, sleep) produce the same `reason` for `Trigger.TICK` and `Trigger.REQUEST`.
  - `test_legacy_timer_spam_refused` (Review Focus 4) — after one committed proactive, 10 `request_proactive` calls in a loop → all `DO_NOTHING`, one task total, nothing sent.
  - `test_images_discarded` — `request_proactive("a", images=[{...}])`; the stub receives no images (its signature has none) and the debug log mentions `discarded 1`.
  - **Race:**
    - `test_request_then_tick_same_iteration_one_task` — `async def req(): return scheduler.request_proactive("a")`; `await asyncio.gather(req(), scheduler.tick_once())` → the stub was started exactly once.
    - `test_tick_then_request_one_task` — the reverse order: `gather(scheduler.tick_once(), req())`. In both tests, count the tasks created by patching `asyncio.create_task` inside the scheduler module → exactly 1.
  - `test_recheck_sees_reserved` — set `reserved=True` after the selector ran (patch `selector.select` to set it) → commit refused, `DO_NOTHING/conversation_lock`.
  - `test_switch_config_follows_new_brain` (Review Focus 3) — swap `client_contexts["a"].pet_brain` for a new brain → the next tick ticks the new brain, not the old; set it to `None` → the client is unhandled, no error.
  - `test_client_a_does_not_affect_client_b` — two handled clients: A has `last_proactive = now - 60` (in cooldown), B is fresh → one `tick_once()` commits for B, and A's decision is `DO_NOTHING/cooldown`. B's commit leaves A's presence unchanged.
  - Done-callback: normal completion → `awaiting_reply_since` set and `PROACTIVE_SPOKEN` applied (`social_need` decreased); stub raises → no awaiting; cancelled → no awaiting.
  - Idle: dispatch sends a payload whose `json.loads` has `audio is None`, `actions == {"expressions": [5]}` and no `emotion` key; `brain.emotion.current_emotion_source.value == "idle_behavior"`. A missing key → nothing sent, no exception.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the Phase 2 and Phase 1 suites → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): single evaluate path with atomic proactive commit and idle expressions`

---

### Task 10: User preemption

**Files:**
- Modify: `src/open_llm_vtuber/pet_brain/scheduler.py`
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Produces:
  - `async preempt_for_user(uid: str) -> None`
  - `user_turn_registered(uid: str, task: Optional[asyncio.Task]) -> None` — clears `user_turn_pending`; if `task` is given, adds a done-callback setting `last_conversation_end`

**`preempt_for_user`** (spec §8.6). Missing presence → return. Then:
1. Synchronously: `last_user_interaction = now`, `ignored_count = 0`, `awaiting_reply_since = None`, `user_turn_pending = True`.
2. If a proactive task is running: `proactive_preempted = True`, `task.cancel()`.
3. `done, _ = await asyncio.wait({task}, timeout=preempt_warn_seconds)`. If not done: `logger.error("[Proactive] preempt still waiting after {s}s uid=…")`, then `await asyncio.wait({task})` (no timeout).

`asyncio.wait` never cancels or abandons the task on timeout, so no shield is needed.

- [ ] **Step 1: Write failing tests** `PreemptTests`. The fake proactive turn is a stub that records `"proactive_out"` messages in a shared `events` list every 1 ms until cancelled. The fake user turn records `"user_start"`.
  - `test_user_preempts_running_proactive`:
    - commit a proactive, let it emit;
    - `await scheduler.preempt_for_user("a")`, then create and register the user task via `user_turn_registered`;
    - assert the proactive task is `done()` **before** `"user_start"`, no `"proactive_out"` appears after `"user_start"`, `reserved` is False, `awaiting_reply_since is None` and `ignored_count == 0`;
    - advance the clock 10 min + `tick_once()` → no `PROACTIVE_IGNORED`.
  - `test_pending_blocks_commit_during_wait` — a stub that delays its cancellation (catches `CancelledError`, then `await asyncio.sleep(0.05)` and re-raises); during the wait `request_proactive("a")` → `DO_NOTHING/user_turn_pending`.
  - `test_slow_preempt_keeps_invariant`:
    - `preempt_warn_seconds=0.01`; the stub delays cancellation by 0.1 s;
    - run `preempt_for_user` as a task and sample every 5 ms while it runs: assert that never both (proactive not done) and (a user task registered) hold;
    - the error log contains `preempt still waiting`;
    - the user task is created only after `preempt_for_user` returns, and the proactive task is `done()` at that moment.
  - `test_frontend_interrupt_not_ignored` — cancel the proactive task directly (the existing `handle_individual_interrupt` path) → no awaiting reply, never ignored.
  - `test_completed_and_unanswered_ignored_once` — the stub returns normally; the clock goes +5 min → one `PROACTIVE_IGNORED`; +10 min more → still one.
  - `test_disconnect_during_preempt_wait` (Review Focus 1) — `client_disconnected("a")` while `preempt_for_user` awaits → no exception from either; the scheduler stops.
  - `test_registered_none_clears_pending` — `user_turn_registered("a", None)` → `user_turn_pending` False.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(pet_brain): user input preempts proactive with unbounded wait`

---

### Task 11: Wire into websocket and conversation handlers

**Files:**
- Modify: `src/open_llm_vtuber/conversations/conversation_handler.py:19-107`, `src/open_llm_vtuber/websocket_handler.py` (constructor, `handle_new_connection`, `handle_disconnect`, `_cleanup_failed_connection`, `_handle_conversation_trigger`)
- Test: `tests/test_behavior_phase2.py`

**Interfaces:**
- Consumes: `BehaviorScheduler` API (Tasks 8–10).
- Produces:
  - `load_proactive_prompt(context: ServiceContext) -> str` — the existing prompt-loading block extracted verbatim, including the `"Please say something."` fallbacks and logs
  - `async run_proactive_turn(context, websocket_send, client_uid, context_block) -> None`:
    1. sends `{"type": "full-text", "text": "AI wants to speak something..."}`;
    2. `user_input = load_proactive_prompt(context) + "\n\n" + context_block`;
    3. awaits `process_single_conversation(context=context, websocket_send=websocket_send, client_uid=client_uid, user_input=user_input, images=None, session_emoji=np.random.choice(EMOJI_LIST), metadata={"proactive_speak": True, "skip_memory": True, "skip_history": True})`
  - `handle_conversation_trigger(..., behavior_scheduler: Optional[BehaviorScheduler] = None)` — a new keyword argument, default `None` (the legacy behavior)
  - `WebSocketHandler.behavior_scheduler: BehaviorScheduler`, built in `__init__` with `run_proactive_turn=run_proactive_turn` and this handler's dicts

**Routing in `handle_conversation_trigger`:**
- `handled = behavior_scheduler is not None and behavior_scheduler.eligibility.handles(client_uid)`.
- **`ai-speak-signal`:**
  - handled → `behavior_scheduler.request_proactive(client_uid, data.get("images"))`; return;
  - not handled → the existing branch is unchanged (it now calls `load_proactive_prompt`).
- **`text-input` / `mic-audio-end`**, in the single-client branch, when `handled`:
  ```python
  task = None
  try:
      await behavior_scheduler.preempt_for_user(client_uid)
      task = asyncio.create_task(process_single_conversation(...))  # unchanged args
      current_conversation_tasks[client_uid] = task
  finally:
      behavior_scheduler.user_turn_registered(client_uid, task)
  ```
  `user_input` is read from the buffer **before** the preempt, exactly as today. Group and unhandled paths are unchanged.

**`WebSocketHandler` hooks:**
- `await self.behavior_scheduler.client_connected(client_uid)` as the last step of the `try` in `handle_new_connection`;
- `await self.behavior_scheduler.client_disconnected(client_uid)` as the first line of `handle_disconnect` and in `_cleanup_failed_connection` (a no-op for unknown uids);
- `_handle_conversation_trigger` passes `behavior_scheduler=self.behavior_scheduler`.

- [ ] **Step 1: Write failing tests** `IntegrationTests`. Patch `conversation_handler.process_single_conversation` with `unittest.mock.patch` and fake coroutines:
  - `test_disabled_brain_uses_legacy_speak_path` — the context has `pet_brain=None`. `ai-speak-signal` with `images=[img]` → the websocket gets `"AI wants to speak something..."`; the fake conversation receives `images=[img]` and the legacy metadata; `request_proactive` is never called (spy).
  - `test_behavior_disabled_uses_legacy_path` — the brain has `behavior.enabled=False` → same as above.
  - `test_handled_speak_signal_goes_to_scheduler` — refused (cooldown) → nothing sent, no task.
  - `test_text_input_preempts_proactive` — the full path through `handle_conversation_trigger`: a proactive is running (a fake turn emitting messages), then `text-input` arrives → the proactive is done before the fake user conversation starts, and there is no proactive output after it.
  - `test_cancelled_handler_releases_pending` (Review Focus 2) — cancel the `handle_conversation_trigger` task while it awaits the preempt → `user_turn_pending` is False afterwards.
  - `test_websocket_handler_hooks` — build `WebSocketHandler` with a `SimpleNamespace` `default_context_cache` (add whatever attributes `__init__` touches); spy on `behavior_scheduler.client_connected` / `client_disconnected`; call `handle_disconnect("x")` and `_cleanup_failed_connection("y")` → the spies are awaited with those uids and nothing raises.
  - `test_run_proactive_turn_prompt` — `run_proactive_turn` passes `user_input` that starts with the configured prompt text and contains the context block, with `images=None`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run everything:** `uv run python -m unittest tests.test_behavior_phase2 tests.test_pet_brain -v` → all PASS. `ruff check .` → clean. `ruff format --check` on the changed files → clean.
- [ ] **Step 5: Commit** `feat: route proactive speech through BehaviorScheduler with user preemption`

---

### Task 12: E2E verification and handoff docs

**Files:**
- Create: `mili_docs/PHASE_2_SUMMARY.md`
- Modify: `mili_docs/PROJECT_CONTEXT.md` ("Current Phase Status" and "Current Priority" only)
- Scratch (never committed): an E2E harness under the session scratchpad

**Harness.** Same approach as Phase 1:
- the server runs on a spare port with the user's `conf.yaml`, and PetBrain + behavior are enabled **in memory only**;
- `handler.behavior_scheduler.tick_seconds = 1`;
- the test brain gets `proactive.min_interval_min=0.2` and `post_conversation_quiet_min=0.1`;
- one websocket client drives it.

- [ ] **Step 1: Scenario 1 (proactive by scheduler).** Raise `social_need` and `boredom` in memory. Pass when, within ~10 s, the client receives `"AI wants to speak something..."`, then an audio payload from real Ollama + edge-tts, and the brain shows `proactive_spoken` in `snapshot()`/logs.
- [ ] **Step 2: Scenario 2.** `ai-speak-signal` right after scenario 1 → nothing received for 3 s, and the log shows `trigger=request → DO_NOTHING (cooldown)`.
- [ ] **Step 3: Scenario 3 (concurrency).** Once cooldowns have elapsed, send `ai-speak-signal` at the moment a tick fires (loop: send a request every 50 ms for 2 s). Pass: exactly one `"AI wants to speak something..."` is received.
- [ ] **Step 4: Scenario 5.** Close the client → the log shows the scheduler stopped, and `handler.behavior_scheduler.is_running is False`.
- [ ] **Step 5: Scenarios 4 and 6 need the desktop frontend.** Ask the user to run the app with the in-memory config (or a temporary `conf.yaml` copy they approve):
  - (4) idle expression: how the frontend renders a silent payload (subtitle? expression kept?);
  - (6) type a message while Mili is speaking proactively → her proactive speech stops and the answer follows without overlap.

  Record exactly what the user reports. Do not claim these passed without their observation.
- [ ] **Step 6: Final checks.** Both unit suites pass, `ruff check .` is clean, and `git status` shows no changes to `frontend/` or `conf.yaml`.
- [ ] **Step 7: Write `PHASE_2_SUMMARY.md`** using the handoff convention in `PROJECT_CONTEXT.md` (implemented, deferred, files, tests + E2E results, known issues, decisions, TODOs, git state, deviations from the spec). Update the Phase 2 status lines in `PROJECT_CONTEXT.md`.
- [ ] **Step 8: Commit** `docs(mili): Phase 2 summary and project status`
