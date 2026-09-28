# PHASE_2_SPEC.md — Living Behavior

Status: **spec approved.** No code or implementation plan yet.
Date: 2026-09-28. Branch: `mili-dev` (Phase 1 committed as `f2dac06`).

Scope: Behavior Scheduler, Idle Behavior (expressions), Proactive Speaking owned by the backend, User Activity / Context awareness, Daily Rhythm, time-driven Lifecycle and Mood transitions.

Permanent project context: `PROJECT_CONTEXT.md`. Phase 1 record: `PHASE_1_SUMMARY.md`.

---

## 1. Goals and non-goals

**Goals**
1. Mili decides by herself, in deterministic code, whether to speak, show an idle expression, or do nothing.
2. The backend owns proactive speaking. The frontend `ai-speak-signal` becomes a *request* that goes through the same decision path and may be refused.
3. Mili knows lightweight desktop context: user input idle time, foreground process name and its category, fullscreen state, local time.
4. Daily rhythm and time drive mood and lifecycle.
5. The LLM is called only after the scheduler has decided to speak. No LLM call per tick.
6. Everything can be disabled by config; disabled means the pre-Phase-2 behavior is unchanged.

**Non-goals (Phase 2)**
- Motions (the backend sends none today), desktop movement, mischief — Phase 3.
- `listening` state from VAD — Phase 4.
- CompanionBrain / SessionPresence ownership split of `PetBrain` — later. (Phase 2 adds *scheduler-side* per-client presence, see §4, but does not split `PetBrain`.)
- Settings UI / debug panel — Phase 7.
- Any frontend / submodule change.
- Pre-existing upstream issues not directly needed by Phase 2 (for example `handle_disconnect` popping `client_contexts` before calling `context.close()`).

**User decisions recorded**
- Proactive coordination: **backend is the owner.** The user turns off `allowProactiveSpeak` in the frontend; if the frontend still sends `ai-speak-signal`, it is only a request.
- Context privacy: Mili may know **category + process name** (e.g. `coding (Code.exe)`). The **window title is never read**, never put in a prompt, never logged. Process name is context metadata, not screen content. If a later workflow needs titles, it will be a separate permission-controlled feature.

---

## 2. Architecture

```text
WebSocketHandler
  ├─ client_connected(uid) / client_disconnected(uid)
  ├─ ai-speak-signal ──► scheduler.request_proactive(uid)
  └─ owns exactly one BehaviorScheduler
                         │ every tick (20 s)
                         ▼
   1. Context         ContextSensor.sample()  → ContextSnapshot (raw, Optional fields)
   2. Daily Rhythm    DayPart from local wall clock
   3. Mood update     brain.mood.advance(), integrated per DayPart segment
   4. Lifecycle       evaluated on the mood updated in this same tick
   5. Behavior        for each eligible client: evaluate_client(uid, trigger="tick")
                         │
         ┌───────────────┼──────────────────┐
     DO_NOTHING    IDLE_EXPRESSION     PROACTIVE_SPEAK
      (log)       silent payload        reserve client → create conversation task
                  audio=None + actions  (process_single_conversation, enriched prompt)
```

Principles kept from Phase 1: the LLM is not the loop; deterministic code decides; small hooks into existing files; everything testable with injected clock / sensor / random.

---

## 3. Components (new files in `src/open_llm_vtuber/pet_brain/`)

| File | Responsibility |
|---|---|
| `context.py` | `ContextSnapshot`, `ContextSensor` protocol, `WindowsContextSensor` (ctypes only, no new dependency), `NullContextSensor`, `classify_process(name, table)`. |
| `rhythm.py` | `DayPart` enum, `day_part_at(datetime)`, `split_by_day_part(start, end)` → ordered segments. |
| `behavior.py` | `BehaviorKind`, `Trigger`, `Decision` (kind, reason, details), `BehaviorSelector` (pure rules: veto, frequency gates, willingness, idle-expression choice). |
| `presence.py` | `ClientPresence` (per-client bookkeeping) and `SchedulerState` (scheduler-global bookkeeping). |
| `eligibility.py` | `ClientEligibility` policy. |
| `proactive_prompt.py` | Pure function that builds the enriched proactive prompt text. |
| `scheduler.py` | `BehaviorScheduler`: single background task, tick loop, `evaluate_client()`, commit/dispatch. |

Modified files are listed in §12.

---

## 4. State ownership

### 4.1 `PetBrain` keeps companion state only
`PetBrain` keeps exactly what it owns in Phase 1: **Mood, Lifecycle, Emotion, Permission** (plus the Phase 1 `activity`). Phase 2 adds to it only:
- `tick(inputs)` — runs Mood update then Lifecycle evaluation (steps 3–4) from inputs supplied by the scheduler;
- new mood events (§7.3).

No scheduler or per-client bookkeeping (`last_conversation`, `last_proactive`, proactive history, backoff…) is stored in `PetBrain`.

### 4.2 `ClientPresence` (one per connected client, owned by the scheduler)

| Field | Meaning |
|---|---|
| `connected_at` | when the client connected |
| `last_user_interaction` | last `text-input` / `mic-audio-end` from this client |
| `last_conversation_end` | when this client's last conversation task finished (user or proactive) |
| `last_proactive` | when a proactive turn was last committed for this client |
| `proactive_timestamps` | deque for the per-hour sliding window |
| `ignored_count` | consecutive proactive turns with no user reply |
| `awaiting_reply_since` | set only when a proactive turn **completes normally**; cleared by user interaction |
| `last_idle_expression`, `idle_expression_timestamps` | idle-expression gates |
| `reserved` | `True` from commit until the proactive task finishes (§8.5) |
| `proactive_task` | the committed proactive task, used for preemption (§8.6) |
| `user_turn_pending` | `True` while a user turn is preempting a proactive turn and not yet registered (§8.6) |

Client A's conversation never changes client B's cooldowns.

### 4.3 `SchedulerState` (desktop-global, owned by the scheduler)
- `last_context: ContextSnapshot | None`
- `previous_user_idle_seconds: float | None` — for `user_returned` detection (desktop input is global, not per client)
- `task`, `lock` (§9)

### 4.4 Brain resolution
The scheduler never reads `default_context_cache.pet_brain` for a client. Flow:

```text
eligible client_uid → client_contexts[uid] → context.pet_brain
```

After a `switch-config`, the session that switched points to its own brain, and the scheduler follows it.

For steps 3–4 the scheduler collects the **distinct** brains (by identity) referenced by connected clients and ticks each **once** per scheduler tick. Inputs derived from presence (e.g. seconds since last conversation) use the most recent value across the clients that share that brain.

A client whose brain is `None`, or whose `behavior.enabled` is `False`, is skipped by the scheduler and keeps the legacy behavior (§10).

---

## 5. Context awareness

### 5.1 `ContextSnapshot`
```python
@dataclass(frozen=True)
class ContextSnapshot:
    taken_at_wall: datetime                 # local time, always known
    user_idle_seconds: Optional[float]      # None = unknown
    process_name: Optional[str]             # e.g. "Code.exe"; None = unknown
    fullscreen: Optional[bool]              # None = unknown
```
`category` is computed per brain from `process_name` with that brain's `context.process_categories` table:
- `process_name is None` → category `None` (unknown sensor result);
- name not in the table → category `"unknown"` (known process, unclassified).

**Unknown is never converted to zero/false.** A sensor error must not look like "the user just acted".

### 5.2 `WindowsContextSensor` (ctypes, no new dependency)
- Idle: `GetLastInputInfo` + `GetTickCount` (handle 32-bit tick wrap-around).
- Foreground process: `GetForegroundWindow` → `GetWindowThreadProcessId` → `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` → `QueryFullProcessImageNameW` → basename only.
- Fullscreen: foreground window rect covers its monitor rect (`MonitorFromWindow` + `GetMonitorInfoW`); the desktop/shell window is not fullscreen.
- **Never calls `GetWindowText`.**
- Each field is read independently. A failure in one field sets only that field to `None` and logs at debug level.
- Non-Windows platforms or import failure → `NullContextSensor` (all fields `None`).

### 5.3 Fail-closed rules
| Missing field | Effect |
|---|---|
| `user_idle_seconds is None` | No `PROACTIVE_SPEAK`. No `away` transition. No `user_returned`. |
| `fullscreen is None` | No `PROACTIVE_SPEAK`, no `IDLE_EXPRESSION`. |
| `category is None` | No `PROACTIVE_SPEAK`. |

Idle expression otherwise depends only on mood.

### 5.4 Categories
Default table (config, both templates; editable):

| Category | Default processes |
|---|---|
| `coding` | `Code.exe`, `devenv.exe`, `pycharm64.exe`, `idea64.exe`, `rider64.exe`, `WindowsTerminal.exe` |
| `unity` | `Unity.exe`, `Unity Hub.exe` |
| `office` | `WINWORD.EXE`, `EXCEL.EXE`, `POWERPNT.EXE`, `OUTLOOK.EXE`, `ONENOTE.EXE` |
| `browser` | `chrome.exe`, `msedge.exe`, `firefox.exe`, `brave.exe`, `opera.exe` |
| `media` | `vlc.exe`, `Spotify.exe`, `PotPlayerMini64.exe`, `mpc-hc64.exe` |
| `gaming` | `steam.exe`, `EpicGamesLauncher.exe` (actual games are mostly caught by the fullscreen veto) |

Matching is case-insensitive on the basename.

A `mili` category (the Mili window itself is focused) is **not** included: the current source has no verified, reliable way to identify the Electron frontend process. It stays future/optional; no unproven heuristic.

Logging: `[Context] category(process)` at debug level on change only. Window titles never appear.

---

## 6. Daily rhythm

`DayPart`: `morning` 05:00–11:00, `afternoon` 11:00–17:00, `evening` 17:00–22:00, `late_night` 22:00–05:00 (local time).

Rhythm-dependent mood trend per hour (added to the Phase 1 `TREND_PER_HOUR`):

| DayPart | Extra trend per hour |
|---|---|
| morning | energy +0.05 |
| afternoon | — |
| evening | — |
| late_night | sleepiness +0.15, energy −0.05 |

Focus trend: while the user is active (`user_idle_seconds < 120`) in `coding` or `unity`, focus +0.2/h. This comes from the context inputs, not the day part.

### 6.1 Correct integration across boundaries
`Mood.advance()` must not apply the current day part's rate to the whole elapsed interval.

`Mood` keeps an anchor pair `(last_monotonic, last_wall)`, updated together at the end of every `advance()`.

- `elapsed_mono = now_mono − last_monotonic`: how much time really passed. This value drives all trends.
- `elapsed_wall = now_wall − last_wall`.
- **Continuous case:** `|elapsed_wall − elapsed_mono| ≤ 120 s`. The interval `[last_wall, last_wall + elapsed_mono]` is split at every day-part boundary (05, 11, 17, 22, crossing midnight and multiple days if needed). Each segment applies the trend of its own day part, in chronological order, clamping to `[0, 1]` after each segment.
  - Example: an advance at 05:10 after 30 minutes → 20 minutes at the late-night rate + 10 minutes at the morning rate.
- **Discontinuity** (the wall clock jumped forward or backward, e.g. the user changed the Windows time or a time sync corrected a large drift). Policy: the timeline for this interval is unknown, so it is not guessed.
  - Apply the rhythm-**independent** trends (Phase 1 `TREND_PER_HOUR` / `RELAX_PER_HOUR`) for `elapsed_mono`.
  - **Skip** rhythm-dependent trends for this interval.
  - Log `[Mood] wall-clock jump detected (Δ=…s); rhythm trend skipped for this interval`.
  - Re-anchor on `(now_mono, now_wall)`, so the next advance is continuous again.
- Normal sleep/hibernate is **not** a discontinuity on Windows: `time.monotonic()` (`GetTickCount64`) keeps counting while suspended, so both clocks advance together. This should be confirmed by a manual check but is not relied on for correctness: if they diverge, the discontinuity policy applies.

This design does not claim full robustness to clock changes. It only guarantees that a detected jump never applies a day part's rate to time that was not spent in that day part.

There is one code path: the lazy `advance()` called from conversation events and the call from the scheduler tick produce the same result.

Context-dependent trends (focus) apply to the elapsed interval since the previous tick using the previous snapshot. If that snapshot is unknown, the context trend is not applied.

---

## 7. Mood and Lifecycle per tick

### 7.1 Order
```text
Context → Daily Rhythm → Mood update → Lifecycle evaluation → Behavior selection
```
Lifecycle uses the mood values updated in the same tick.

### 7.2 Time-driven lifecycle
Time-based transitions are evaluated per tick, so a transition can happen up to one scheduler interval (≈20 s) after its threshold. Tests do not require exact timestamps.

| Transition | Condition |
|---|---|
| `active → idle` | ≥ 2 min since the last conversation end of the clients on this brain, and no conversation running |
| `idle → sleepy` | `sleepiness ≥ 0.7` |
| `sleepy → sleep` | `sleepiness ≥ 0.85` and `user_idle_seconds ≥ 15 min` (known) |
| `active / idle / sleepy → away` | `user_idle_seconds ≥ away_after_min` (default 10 min, known) |
| `away → wake_up → active` | `user_returned` |
| `sleep → wake_up` | only when the user talks to Mili (Phase 1 `ensure_active`). The user returning to the PC does **not** wake her. |

All transitions go through the existing `ALLOWED_TRANSITIONS` table. A disallowed transition is rejected and logged at debug level, as in Phase 1.

### 7.3 Mood events (new `BrainEvent` values)

| Event | Effect |
|---|---|
| `user_returned` | happiness +0.05, curiosity +0.05 |
| `proactive_spoken` | social_need −0.10 |
| `proactive_ignored` | happiness −0.03 |

`user_returned` fires when the previous known idle was ≥ `away_after_min` and the current known idle is < 30 s. It is sent to every distinct brain.

---

## 8. Behavior selection

### 8.1 One evaluation path
```python
def evaluate_client(self, uid: str, trigger: Trigger) -> Decision
```
- `Trigger.TICK` (periodic) and `Trigger.REQUEST` (legacy `ai-speak-signal`) call **the same function**.
- They share veto, conversation lock, cooldowns, frequency limits, willingness and the `BehaviorSelector`.
- `trigger` only affects logging and the prompt context. It does not relax any rule.
- `evaluate_client` is **synchronous**. If the decision is `PROACTIVE_SPEAK`, it commits immediately in the same synchronous section (§8.5).
- For `REQUEST`, the scheduler refreshes `last_context` with a synchronous `sample()` first (a few syscalls). It does not run steps 2–4; brain state may be up to one tick old, which is consistent with §7.2.

### 8.2 Eligibility (`ClientEligibility`)
A client is eligible when:
1. it is in `client_connections` and `client_contexts`;
2. it is not in a group with more than one member;
3. its `context.pet_brain` is not `None` and `behavior.enabled` is `True`;
4. it is not `reserved`, not `user_turn_pending`, and has no running conversation task in `current_conversation_tasks[uid]`.

For `TICK`, when several clients are eligible, the default policy orders them by most recent `last_user_interaction` and evaluates them in that order. At most one proactive turn is committed per tick in total. The ordering rule lives in the policy class so it can change without touching the selector.

For `REQUEST`, only the requesting client is evaluated.

### 8.3 Vetoes → `DO_NOTHING`
Checked in this order; the first match is logged as the reason:
1. not eligible (includes the per-client conversation lock);
2. **conversation lock:** `brain.activity != idle` (the companion is talking to someone);
3. lifecycle is `sleep` or `away`;
4. `fullscreen is True`, or `fullscreen is None` (fail closed).

### 8.4 `PROACTIVE_SPEAK`
Requires, in addition to §8.3: `proactive.enabled`, `user_idle_seconds` known, `category` known.

**Frequency gates (per client)**

| Gate | Default |
|---|---|
| `min_interval_min` since `last_proactive` | 10 |
| `post_conversation_quiet_min` since `last_conversation_end` / `last_user_interaction` | 3 |
| `max_per_hour` (sliding window) | 3 |
| Ignored backoff: an effective interval of `min_interval × 2^ignored_count`, capped at `max_backoff_min` | cap 60 |

A proactive turn counts as ignored only when it **completed normally** (not cancelled, not interrupted, not preempted, no error) and `awaiting_reply_since` is then older than `ignored_after_min` (default 5) with no user interaction. A proactive turn the user interrupted or preempted is never ignored. The next tick detects this, increments `ignored_count` and emits `proactive_ignored`. Any user interaction resets `ignored_count` to 0 and clears `awaiting_reply_since`.

**Coding / Unity rule:** if category is `coding` or `unity`, also require `user_idle_seconds ≥ 120` (the user paused).

**Willingness**
```text
want        = 0.5·social_need + 0.3·boredom + 0.2·curiosity
willingness = want × (1 − 0.5·sleepiness) × (0.5 + 0.5·energy)
                   × category_mult × daypart_mult + returned_bonus
speak if willingness ≥ threshold (0.30) and random() < chance (0.35)
```
- `category_mult`: coding 0.3, unity 0.3, office 0.5, media 0.5, browser 1.0, unknown 0.8, gaming 0.0.
- `daypart_mult`: morning 1.0, afternoon 1.0, evening 0.9, late_night 0.5.
- `returned_bonus`: +0.2 for the first evaluation after `user_returned`. It is consumed either way.
- `random` is injectable (seeded in tests).

Reference points (category `browser`, daytime):
- default mood at start ≈ 0.27 → no speech;
- after ~30 min without interaction ≈ 0.31 → speech becomes possible.

Under `unknown` (×0.8) or `coding` (×0.3) the same mood stays below the threshold much longer; this is intended.

All numbers are config values (§11).

### 8.5 Commit and the race invariant
> **Invariant: at most one conversation-producing behavior may be committed per client at a time.**

Commit sequence, all synchronous with **no `await` between the check and task creation**:
1. re-check eligibility (§8.2) and the conversation lock (§8.3 #2);
2. set `presence.reserved = True`, record `last_proactive`, and append to `proactive_timestamps`;
3. `task = asyncio.create_task(proactive_turn(...))`, store it in `current_conversation_tasks[uid]` and `presence.proactive_task`, and add a done-callback that clears `reserved` and `proactive_task` and sets `last_conversation_end`. It sets `awaiting_reply_since` **only if the task completed normally** (not cancelled, no exception).

Everything that awaits (the "AI wants to speak something..." message, prompt loading, the conversation itself) runs **inside** the task, after the commit.

In the single-threaded asyncio loop this makes a near-simultaneous tick and `ai-speak-signal` produce exactly one proactive conversation. The second evaluation sees `reserved` / the running task and returns `DO_NOTHING (conversation lock)`.

### 8.6 User input preempts proactive
User interaction always has priority over a proactive turn. A proactive task and a user conversation must never run at the same time; overwriting the dict entry is not enough.

For a scheduler-handled client, `handle_conversation_trigger` (`text-input` / `mic-audio-end`) calls `await scheduler.preempt_for_user(uid)` **before** creating the user task:

```text
user input
  ↓  (synchronous)
note_user_interaction: last_user_interaction = now, ignored_count = 0, awaiting_reply_since = None
user_turn_pending = True                  ← blocks any proactive commit from now on
  ↓
proactive_task running?  ── no ──► return
  ↓ yes (synchronous)
mark it preempted, task.cancel()
  ↓  (await)
wait for the cancelled task to finish (timeout 5 s)
  → its CancelledError path runs: Phase 1 INTERRUPTED event, TTS cleanup
  → done-callback releases `reserved`; preempted/cancelled ⇒ never awaiting a reply, never ignored
  ↓
return → caller creates the user task and registers it in current_conversation_tasks[uid]
         then clears user_turn_pending (synchronously, right after create_task)
```

- `user_turn_pending` closes the window during the `await`: a tick or request in that window sees it and returns `DO_NOTHING (conversation lock)`. After the user task is registered, the running task itself keeps the lock.
- `agent_engine.handle_interrupt()` is **not** called for a preempted proactive turn. Proactive turns run with `skip_memory` / `skip_history`, so there is no partial response to record. This differs from a frontend `interrupt-signal`, which keeps its existing path.
- The backend does **not** send `control: interrupt` to the frontend here. That would make the frontend send `interrupt-signal` back, which could cancel the *new* user task. Audio the frontend has already received is stopped by the frontend's own interrupt-on-input behavior. This must be verified in E2E and no frontend change is made.
- If the cancelled task does not finish within 5 s (not expected: the cancel path only notifies and runs synchronous cleanup), log `[Proactive] preempt timeout` at error level and continue with the user turn. User input is never blocked indefinitely.
- A frontend `interrupt-signal` during a proactive turn cancels it through the existing handler. The task ends cancelled, so it is also never counted as ignored.
- Clients not handled by the scheduler keep today's path unchanged.

Scope of the invariant: it covers proactive behavior against any conversation. Two overlapping *user* turns (pre-existing behavior) are out of scope for Phase 2.

### 8.7 The proactive turn
- `proactive_turn` sends the existing `full-text` "AI wants to speak something..." message.
- It calls `process_single_conversation` with the same metadata as the legacy path (`proactive_speak`, `skip_memory`, `skip_history`) and `images=None`.
- The user input text is the configured `proactive_speak_prompt` plus a context block from `proactive_prompt.py`:
  - day part and local time (HH:MM);
  - lifecycle;
  - the 2–3 strongest mood traits in words;
  - `category (process.exe)`;
  - user idle minutes;
  - `returned after N min` if applicable;
  - trigger.
  - No window title. Unknown fields are omitted, never guessed.
- On success the brain receives `proactive_spoken`. Failures follow the Phase 1 `ERROR` event, and the attempt still counts against cooldowns (no retry storm).

### 8.8 Legacy request images
The frontend may attach `images` to `ai-speak-signal`. When the client is handled by the scheduler (Phase 2 enabled), these images are **discarded** (debug log with the count only). A legacy proactive request must never become a screenshot-perception workflow.

### 8.9 `IDLE_EXPRESSION`
Considered only when `PROACTIVE_SPEAK` was not selected and no veto applied. No LLM, no speech.

| Gate | Default |
|---|---|
| `min_interval_min` | 3 |
| `max_per_hour` | 10 |
| `chance` per eligible evaluation | 0.3 |

Emotion choice, first match wins:
1. sleepiness > 0.6 → `sleepy`
2. boredom > 0.6 → `bored`
3. curiosity > 0.7 → `curious`
4. happiness > 0.7 → `joy`
5. otherwise → `neutral`

- Candidate chain: preferred key, then `neutral`, keeping only keys that exist in the model's `emotionMap`. If neither exists → `DO_NOTHING (no expression available)`. Missing keys **never raise**.
- The chosen expression goes through `pet_brain.emotion.gate()` with source `idle_behavior`.
- It is sent with `prepare_audio_payload(audio_path=None, display_text=None, actions=...)` to that client only.
- `IDLE_EXPRESSION` does not produce a conversation, does not reserve the client and is not subject to §8.5.
- Frontend rendering of this payload (subtitle, expression reset) must be verified in E2E.

### 8.10 Logs
- `[Behavior] uid=… trigger=tick|request → DO_NOTHING (reason)`: info when the trigger is `request` or the decision is not `DO_NOTHING`; otherwise debug, so an idle desktop does not flood the log.
- `[Proactive] committed uid=… willingness=0.34 category=coding(Code.exe)`
- `[Proactive] ignored count=2 backoff=40min`

---

## 9. Scheduler lifecycle

- `WebSocketHandler` constructs exactly one `BehaviorScheduler`.
- `client_connected(uid)` and `client_disconnected(uid)` run under an `asyncio.Lock`:
  - 0 → 1 connected clients: create the tick task;
  - more clients: create presence only, never a second task;
  - 1 → 0: cancel the task, await it (swallow `CancelledError`), clear presence and `SchedulerState.last_context`.
- Called at the end of `handle_new_connection` (after the client is stored), at the start of `handle_disconnect`, and from `_cleanup_failed_connection` (only if the client was registered).
- Start/stop never creates, replaces or drops a `PetBrain`.
- The tick interval is a constructor argument (default 20 s), **not a YAML key**. Config is per character/brain while the scheduler is desktop-global.
- Each tick body runs in `try/except Exception`: the failure is logged as `[Behavior] tick failed` and the loop continues. `CancelledError` propagates.
- Sampling is skipped (no syscalls) when no connected client has a brain with `behavior.enabled`.
- The scheduler holds read-only references to `client_connections`, `client_contexts`, `current_conversation_tasks` and `chat_group_manager`. It only writes `current_conversation_tasks[uid]` in the commit step (§8.5), as `handle_conversation_trigger` does today.

---

## 10. `ai-speak-signal` handling and rollback

In `conversation_handler.handle_conversation_trigger`:
- **Scheduler handles the client** (brain present, `behavior.enabled`): call `scheduler.request_proactive(uid)` → `evaluate_client(uid, Trigger.REQUEST)` and return. Nothing is sent to the frontend if the request is refused.
- **Otherwise:** the legacy branch runs unchanged, including the frontend images and the "AI wants to speak something..." message. This is the rollback guarantee.

`text-input` / `mic-audio-end` for a scheduler-handled single client call `await scheduler.preempt_for_user(uid)` before creating the user task, and `scheduler.user_turn_registered(uid)` right after registering it (§8.6). For other clients these are no-ops and the existing path is unchanged.

Rollback levels:
1. `pet_brain_config.behavior.enabled: False` → Phase 2 off, Phase 1 on.
2. `pet_brain_config.enabled: False` (default) → both off.
3. Code: `git revert` of the Phase 2 commit(s).

---

## 11. Configuration (`pet_brain_config`, added to both templates)

```yaml
pet_brain_config:
  enabled: False
  # ... Phase 1 keys unchanged ...
  behavior:
    enabled: True            # only effective when pet_brain_config.enabled
  proactive:
    enabled: True
    min_interval_min: 10
    post_conversation_quiet_min: 3
    max_per_hour: 3
    threshold: 0.30
    chance: 0.35
    ignored_after_min: 5
    max_backoff_min: 60
  idle_expression:
    enabled: True
    min_interval_min: 3
    max_per_hour: 10
    chance: 0.3
  context:
    enabled: True            # False → NullContextSensor semantics (fail closed)
    away_after_min: 10
    process_categories:
      Code.exe: coding
      # ... table from §5.4 ...
```

- Pydantic models with `I18nMixin` descriptions (en + zh), following `config_manager/pet_brain.py`.
- Validation: probabilities and the threshold in `[0, 1]`; intervals and counts `≥ 0`; category values limited to the known set (`coding, unity, office, browser, media, gaming, unknown`).
- A `conf.yaml` without the new sections validates with defaults (tested, as in Phase 1).
- Changing these values does **not** rebuild the agent. Only Phase 1 fields affect the guarded ToolExecutor.

---

## 12. Files to change

**New:** `pet_brain/context.py`, `rhythm.py`, `behavior.py`, `presence.py`, `eligibility.py`, `proactive_prompt.py`, `scheduler.py`; `tests/test_behavior_phase2.py` (a separate file keeps the Phase 1 suite untouched); `mili_docs/PHASE_2_SUMMARY.md` at the end.

**Modified (small hooks):**

| File | Change |
|---|---|
| `pet_brain/pet_brain.py` | `tick(inputs)`; handle new events |
| `pet_brain/mood.py` | wall-clock + per-DayPart integration; context trend; new event effects |
| `pet_brain/lifecycle.py` | `evaluate(inputs)` time-driven transitions |
| `pet_brain/events.py` | `user_returned`, `proactive_spoken`, `proactive_ignored` |
| `pet_brain/__init__.py` | exports |
| `config_manager/pet_brain.py` | new config models |
| `config_templates/conf.default.yaml`, `conf.ZH.default.yaml` | new sections |
| `websocket_handler.py` | own the scheduler; connect/disconnect hooks |
| `conversations/conversation_handler.py` | `ai-speak-signal` → scheduler when handled; user input preempts proactive (`preempt_for_user` / `user_turn_registered`) |

`single_conversation.py` needs no change: the enriched prompt is built by the scheduler and passed as `user_input`.

**Not touched:** frontend / submodule, `conf.yaml`, ASR / TTS / VAD, MCP, agents, chat history, `upgrade_codes/`.

---

## 13. Testing

Stdlib `unittest`. Clock (monotonic + wall), sensor and random are injected; no real sleeps.

**Rhythm / Mood**
- `split_by_day_part` across 05:00, across midnight, and over more than 24 h.
- Advance at 05:10 after 30 min = 20 min late-night + 10 min morning.
- One long advance ≈ many short advances (tolerance).
- Wall clock jumps +2 h during 30 min of monotonic time → base trends applied for 30 min, no rhythm trend, jump logged, anchor reset; the next advance is continuous again.
- Backward jump → same policy.
- Drift within 120 s → treated as continuous.
- Clamping per segment.
- The focus trend is skipped when the previous snapshot is unknown.

**Lifecycle**
- Every row of §7.2.
- `sleep` does not wake on `user_returned`, only on user input.
- Transitions within one tick after the threshold, with no exact-timestamp assertion.

**Context**
- `classify_process` is case-insensitive; an unlisted process → `"unknown"`; `None` → `None`.
- A sensor field failure gives `None`, never `0` / `False`.
- `NullContextSensor` → no proactive, no away.
- `GetWindowText` is never referenced (source assertion).

**Selector / evaluate_client**
- Each veto.
- Each frequency gate.
- Ignored backoff doubles and caps; a user interaction resets it.
- The willingness threshold and each category multiplier; the coding pause rule; the `returned_bonus` is consumed once.
- Client A's conversation does not affect client B's gates.

**Single path**
- `Trigger.REQUEST` is refused under cooldown / lock / low willingness exactly like `TICK`.
- On refusal nothing is sent to the websocket.
- Images are discarded.

**Race invariant**
- A tick evaluation and a request in the same loop iteration → exactly **one** proactive task.
- Re-check inside the commit sees `reserved`.

**User preemption**
- Proactive running + user text/voice input:
  - the proactive task is cancelled and has finished before the user task is created;
  - the user conversation runs;
  - no output from the proactive task is sent after the user task starts (no interleaved conversation output on the websocket);
  - `reserved` is released;
  - the proactive turn is **not** counted as ignored.
- A tick or request during the preempt `await` window → `DO_NOTHING (conversation lock)` because of `user_turn_pending`.
- A frontend `interrupt-signal` during a proactive turn → not counted as ignored.
- A proactive turn that completed normally and got no reply within `ignored_after_min` → ignored exactly once.
- Preempt timeout path: a task that does not finish in time → error logged, user turn still starts.

**Scheduler lifecycle**
- 0→1 creates a task; a second client creates no new task; 1→0 cancels it and clears state.
- Concurrent connect/disconnect still leaves at most one task.
- `PetBrain` identity is unchanged across start/stop.
- A raising tick does not kill the loop.
- The brain is resolved per client after `switch-config`.

**Idle expression**
- Preferred key missing → falls back to `neutral`.
- Preferred key and `neutral` both missing → `DO_NOTHING`, no exception.
- The expression goes through the gate (`source=idle_behavior`).
- The payload has `audio: None`.

**Config**
- Templates validate; missing sections → defaults; invalid probability / category rejected.

**Regression**
- Phase 1 suite (43 tests) passes.
- With PetBrain disabled or `behavior.enabled: False`, `ai-speak-signal` follows the legacy branch unchanged (images included).
- `ruff check .` clean; `ruff format` clean on changed files.

**E2E (real Ollama, spare port, toggled in memory like Phase 1, short intervals via test config)**
1. A scheduler-initiated proactive turn reaches the client with an enriched prompt; `proactive_spoken` is applied.
2. `ai-speak-signal` during cooldown → refused, nothing sent.
3. **Concurrency:** a periodic tick and `ai-speak-signal` fired near-simultaneously → exactly one proactive conversation.
4. Idle-expression silent payload → observe how the frontend renders it (subtitle, expression persistence).
5. Disconnect of the last client → the scheduler task is gone.
6. **Preemption:** the user sends text while a proactive turn is streaming → the proactive turn stops, the user's answer follows, and the frontend does not play both at once.

---

## 14. Risks

- **Idle expression rendering** in the built frontend is unverified. A silent payload may show an empty subtitle or be reset by the frontend idle motion. Mitigation: E2E check; `idle_expression.enabled: False`.
- **Frontend timer still on:** if the user leaves `allowProactiveSpeak` on, requests arrive every few seconds. They are refused by the gates, but they produce info logs. Documented; the user turns the timer off.
- **Frontend playback after preemption:** the backend stops producing proactive output before the user turn starts, but audio already delivered to the frontend is stopped only by the frontend's own interrupt-on-input behavior. Verified in E2E; no frontend change in Phase 2.
- **Overlapping user turns** (a second user input while a user turn runs) keep today's overwrite behavior; out of Phase 2 scope.
- **Wall-clock jumps** skip rhythm trends for the affected interval (§6.1), so mood can briefly lag the "correct" day-part effect. This is accepted.
- **Shared brain activity** (Phase 1 known issue) means one client talking blocks proactive for others on the same brain. This is intended for Phase 2.
- **Process-name privacy:** process names go into the local LLM prompt and debug logs. This was accepted by the user; window titles are never read.
- **ctypes on unusual setups** (e.g. elevated foreground process): `OpenProcess` fails → `process_name=None` → proactive fails closed while such a window is focused.
- **Tick granularity:** time transitions can lag by up to 20 s (accepted).
