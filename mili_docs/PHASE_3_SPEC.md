# PHASE_3_SPEC.md — Desktop Pet (Phase 3A)

Status: **draft, awaiting user review.** No code yet.
Date: 2026-09-29. Backend branch: `mili-dev` (Phase 2 committed up to `8e8aae7`).
Frontend repo: `D:\Open-LLM-VTuber-Web` (clone of `Open-LLM-VTuber/Open-LLM-VTuber-Web`, `main` = `d176e7d`, package version **1.2.1**, the same version as the installed desktop app).

Phase 3 is split in two. **This spec covers Phase 3A only:** autonomous movement, wander, waypoints, contextual movement, desktop interaction reactions and idle motions. **Phase 3B** (desktop mischief such as moving desktop icons) gets its own spec after 3A is stable (§15).

Permanent context: `PROJECT_CONTEXT.md`. Earlier phases: `PHASE_1_SUMMARY.md`, `PHASE_2_SPEC.md`, `PHASE_2_SUMMARY.md`.

---

## 0. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Movement moves the **Live2D model inside the pet window**, not the `BrowserWindow` (§3). | proposed default |
| D2 | The context sensor may read the **foreground window rectangle** (geometry only, never the title) for contextual movement. | proposed default |
| D3 | While the user is busy (coding / Unity / office and recently active), Mili **moves to the screen edge once, then stays still**. | proposed default |
| D4 | The frontend gets **vitest** as a devDependency to unit-test its pure modules. | proposed default |
| D5 | Phase 3 is split: 3A (this spec), then 3B. | approved |
| D6 | Frontend source lives in a separate repo `D:\Open-LLM-VTuber-Web`; the `frontend/` submodule of the backend is not touched. | approved |

The proposed defaults were chosen by Claude because the user asked to proceed. Changing one before implementation only affects the sections that name it.

---

## 1. Goals and non-goals

**Goals**
1. In Pet Mode, Mili moves around the desktop by herself: short and long wandering, going to a screen edge, going home, and approaching the window the user just opened when she is curious.
2. **The backend decides *whether* and *what kind* of movement** (deterministic rules over mood, lifecycle, context, cooldowns). **The frontend decides *where exactly* and *how*** (coordinates, bounds, easing). The LLM is not involved at all.
3. Click, double-click and drag on Mili become events that change mood and trigger a reaction (expression, and an idle motion if the model has one), without calling the LLM.
4. Movement is smooth, stays inside the work area of a display, never makes the pet window swallow clicks meant for other apps, and can be switched off both in config and from the tray/context menu.
5. Old clients are unaffected: the web frontend (`frontend/` submodule) and the installed v1.2.1 app never receive a new message type.
6. Everything is disabled by default. Disabled means the Phase 2 behavior is unchanged.

**Non-goals (3A)**
- Desktop icons, other windows or the cursor being moved — Phase 3B.
- Facing direction / mirroring the model (a mirrored Live2D model often looks wrong; YAGNI).
- A walk-cycle animation. `mao_pro` has none; movement is a glide with a small vertical bob (§7.4).
- Hiding Mili, follow-cursor mode — later.
- Mixed-DPI multi-monitor fixes for the pet window itself (an upstream limitation, §14).
- Settings UI / debug panel — Phase 7. Only a tray/context-menu checkbox is added.
- Any change to ASR/TTS/VAD, MCP, agents, memory.

---

## 2. Repositories, toolchain, install and rollback

**Frontend repository**
- `D:\Open-LLM-VTuber-Web`, remote `upstream` → `Open-LLM-VTuber/Open-LLM-VTuber-Web`.
- **Blocked on the user:** the fork `Votuan12345/Open-LLM-VTuber-Web` does not exist yet. After the user forks it: add remote `origin` → the fork, create branch `mili-dev` from `main`. Development happens on `mili-dev`; `main` stays equal to upstream.
- No change to remotes, history or submodules of the backend repo.

**Toolchain**
- **Blocked on the user:** Node.js is not installed. Install Node.js **20 LTS or 22 LTS** (includes npm). Then `npm ci` in the frontend repo.
- Dev run: backend `uv run run_server.py`, frontend `npm run dev` (Electron dev window connecting to `ws://127.0.0.1:12393/client-ws`, the stored default).
- Checks: `npm run typecheck`, `npm run lint`, `npx vitest run` (after D4).

**Install and rollback**
- Real use: `npm run build:win` → NSIS installer in `dist/` → install over the current v1.2.1 app. The package version is bumped to `1.2.1-mili.1` so the two builds can be told apart.
- Rollback of the app: reinstall the upstream 1.2.1 release (or keep the current installer before upgrading).
- Rollback of behavior without reinstalling: `pet_brain_config.desktop_pet.enabled: False` (backend) or untick **Autonomous Movement** (frontend).

---

## 3. Key decision D1: move the model inside the pet window

### 3.1 What the source does today
- `src/main/window-manager.ts` → `continueSetWindowModePet()`: in Pet Mode the single `BrowserWindow` is transparent, always-on-top (`screen-saver` level), not focusable, and its bounds are set to **the bounding box of all displays** (the whole virtual screen). Mouse events are ignored with `{ forward: true }`.
- `src/renderer/src/hooks/canvas/use-live2d-model.ts`: on every forwarded `mousemove`, a hit test against the model decides whether the window should catch the mouse (`update-component-hover 'live2d-model'` → `WindowManager.updateComponentHover()` → `setIgnoreMouseEvents(shouldIgnore)`).
- **Dragging Mili today changes the model matrix translation** (`_modelMatrix[12]`, `[13]`, via `LAppAdapter.setModelPosition`). The window never moves.
- `use-live2d-resize.ts`: in Pet Mode the canvas is `window.innerWidth × innerHeight × devicePixelRatio`, i.e. the whole virtual screen.

### 3.2 Options

| | A. Move the `BrowserWindow` | **B. Move the model inside the pet window** |
|---|---|---|
| Fit with current architecture | Needs a model-sized window, a new drag implementation, a new click-through scheme, and handling for crossing monitors. Contradicts `continueSetWindowModePet()`. | Same mechanism as the existing drag. No change to window mode switching. |
| Smoothness | `setBounds` ~60×/s on a transparent Windows window flickers and stutters; one IPC round-trip per frame. | `requestAnimationFrame` in the renderer; no IPC per frame. |
| Multi-monitor | Must be implemented. | The window already spans every display. |
| Cost | Unchanged. | Unchanged (the canvas is already full-screen and redrawn every frame by the Cubism loop). |

**Chosen: B.**

### 3.3 Consequences of B that must be handled
1. **Stale click-through.** Hover state is only recomputed when the mouse moves. If Mili walks *away* from a cursor that is not moving, the window keeps catching the mouse and a click on the empty area is swallowed. If she walks *under* a still cursor, clicks pass through her until the mouse moves (harmless). **Fix:** while moving, and once at the end of a move, the renderer asks the main process for the cursor position (`screen.getCursorScreenPoint()`) at most every 100 ms, hit-tests it against the model, and sends `update-component-hover` when the result changes (§7.5).
2. **Allowed area.** The window covers the taskbar. Movement targets must keep the model's bounding box inside the **work area** of one display (`screen.getAllDisplays()[i].workArea`), which the main process provides (§7.2).
3. **Coordinates.** The controller works in **screen DIP** coordinates (Electron's unit). Conversion chain: screen DIP → window client CSS px (subtract window origin = virtual-screen min x/y) → canvas device px (× `devicePixelRatio`) → Cubism logical view coordinates (`LAppView._deviceToScreen`) → model translation (§7.3).

---

## 4. Architecture

```text
Backend (Python, D:\Open-LLM-VTuber)                 Frontend (Electron, D:\Open-LLM-VTuber-Web)

BehaviorScheduler (existing, 20 s tick)
  Context → Rhythm → Mood → Lifecycle
  ├─ Behavior lane (Phase 2, unchanged)
  └─ Pet lane (new)                     pet-command
       PetSelector (pure)  ───────────────────────────►  DesktopPetController (renderer)
       PetPresence per client                            ├─ protocol.ts   validate messages
            ▲                            pet-status      ├─ planner.ts    pick target in work area
            ├──────────────────────────────────────────  ├─ motion.ts     rAF easing + bob
            │                          pet-interaction   ├─ geometry.ts   DIP ⇄ canvas ⇄ Live2D
  PetInteractionHandler ◄──────────────────────────────  └─ settings      movement toggle, home
       BrainEvent → Mood; reaction                       use-live2d-model.ts: emit click /
       (expression via silent payload,                     double_click / drag_start / drag_end
        motion via pet-command play_motion)              main: pet-ipc.ts (display layout,
                                                           cursor point, screenToDipRect), tray
                                                           "Autonomous Movement" checkbox
```

**Ownership**
- `PetBrain` still holds only companion state (mood, lifecycle, activity, emotion).
- `PetPresence` (new, one per client, owned by the scheduler next to `ClientPresence`) holds what the frontend reported and the pet-lane cooldowns.
- The frontend owns the model position. The backend never sees or sends raw pet coordinates, except the foreground window rectangle in `approach_rect`, which the frontend clamps like any other target.
- One writer per concern: the frontend controller is the only code that moves the model automatically; a user drag always wins and cancels it.

---

## 5. Protocol (websocket, JSON)

Protocol version `1`. Both sides validate every message and drop invalid ones with a debug log. Unknown fields are ignored.

### 5.1 Frontend → backend

**`pet-hello`** — sent once per websocket connection, right after it opens (Electron only). Later mode or toggle changes are reported with `pet-status`.
```json
{"type": "pet-hello", "protocol": 1, "mode": "pet", "movement_enabled": true}
```

**`pet-status`**
```json
{"type": "pet-status", "mode": "pet", "movement_enabled": true,
 "moving": false, "anchor": "home",
 "command_id": "c-42", "result": "arrived", "reason": null}
```
- `mode`: `pet` | `window`.
- `anchor`: `home` | `edge` | `free` — where the model is now (used by rules in §6.3).
- `result` (only when reporting a command): `arrived` | `cancelled` | `rejected`; `reason` is a short code, e.g. `user_drag`, `conversation`, `movement_disabled`, `not_pet_mode`, `invalid_rect`, `unknown_command`, `no_room`.
- Sent: after `pet-hello`, on every mode/toggle change, when a command finishes, and when a drag ends (`anchor: home`).

**`pet-interaction`**
```json
{"type": "pet-interaction", "kind": "click", "hit_area": "HitAreaHead"}
```
- `kind`: `click` | `double_click` | `drag_start` | `drag_end`.
- `hit_area`: optional string (model hit area id) or null.
- Only sent in Pet Mode.

### 5.2 Backend → frontend

**`pet-command`**
```json
{"type": "pet-command", "id": "c-42", "command": "wander", "params": {"distance": "short", "speed": "normal"}}
```

| `command` | `params` | Frontend behavior |
|---|---|---|
| `wander` | `distance`: `short`\|`long`; `speed`: `slow`\|`normal` | random target near the current position (§7.3) |
| `go_edge` | `side`: `nearest`\|`left`\|`right` | nearest horizontal edge of the current display |
| `go_home` | — | stored home position |
| `approach_rect` | `rect`: `{x, y, width, height}` in **physical screen pixels** | converted with `screen.screenToDipRect`, target next to the rect's bottom edge, clamped |
| `stop` | — | stop where she is; reports `cancelled` for the running command |
| `play_motion` | `group`: string, `index`: int | `startMotion(group, index, PriorityNormal)` if the model has it; otherwise log and no-op |

`play_motion` does not produce a `pet-status` result (fire and forget). Every movement command produces exactly one result.

### 5.3 Gating and backward compatibility
- The backend sends `pet-command` **only** to a client whose last `pet-hello`/`pet-status` said `protocol: 1`. The web frontend and the installed 1.2.1 app never send `pet-hello`, so they never receive it.
- Backend handlers for the three new message types are registered in `WebSocketHandler._init_message_handlers()` (existing pattern). If PetBrain, the behavior scheduler or `desktop_pet` is disabled, the handlers accept and ignore the messages (debug log), so an updated frontend works with a disabled backend.
- `pet-interaction` is rate-limited per client: at most 10 messages per second; extra messages are dropped.

---

## 6. Backend

### 6.1 New module `pet_brain/desktop_pet.py`
- `PetPresence` (dataclass, per client): `protocol`, `mode`, `movement_enabled`, `moving`, `anchor`, `pending_command_id`, `pending_since`, `last_move`, `move_timestamps` (deque), `last_contextual`, `paused_until`, `last_reaction`, `click_times` (deque), `last_idle_motion`, `idle_motion_timestamps` (deque), `last_category`, `category_changed`, `returned_pending`.
- `PetKind` enum: `STAY`, `WANDER`, `GO_EDGE`, `GO_HOME`, `APPROACH_WINDOW`, `IDLE_MOTION`.
- `PetDecision(kind, reason, params)`.
- `PetSelector` — pure, `random.Random` injected, never mutates presence. Rules in §6.3.
- `choose_reaction(kind, mood, click_burst, cfg)` — pure, returns a reaction key (§6.5).
- `parse_pet_message(data)` — validates the three inbound message types; returns a typed object or `None`.

### 6.2 Pet lane in the scheduler
- `BehaviorScheduler` keeps `pet_presences: Dict[uid, PetPresence]`, created on `pet-hello`, dropped on disconnect.
- At the end of `tick_once()`, after `_select_and_dispatch()`, for each **pet-eligible** client: `evaluate_pet(uid)` → dispatch. Evaluation is synchronous; the send is the only await.
- A client is **pet-eligible** when all hold:
  - it is handled by the scheduler (Phase 2 eligibility: brain present, `behavior.enabled`, not in a group);
  - `desktop_pet.enabled` for its brain;
  - it sent `pet-hello` with `protocol: 1`, reported `mode: pet` and `movement_enabled: true`.
- Command timeout: if `pending_command_id` is older than `movement.command_timeout_s` (default 60), it is cleared with a warning log, so a lost `pet-status` never blocks movement forever.
- The lane never starts or cancels a conversation, never touches `ClientPresence.reserved`, and never runs during the preempt wait.

### 6.3 `PetSelector` rules (in order; the first match wins)

**Vetoes → `STAY`** (reason in brackets):
1. not pet-eligible, or `desktop_pet.movement.enabled` is False [`disabled`];
2. a command is pending (`moving`) [`moving`];
3. a conversation task is running, reserved, or a user turn is pending [`conversation`];
4. within `post_conversation_quiet_min` of the last conversation end [`post_conversation`];
5. `paused_until` in the future (after drag / double-click) [`paused`];
6. lifecycle `sleep` or `away` [`lifecycle_<phase>`];
7. `fullscreen` is True [`fullscreen`];
8. `user_idle_seconds` or `fullscreen` unknown [`context_unknown`] — fail closed.

**Positive rules:**
1. **Returned:** `PetPresence.returned_pending` (set by the scheduler when it detects `user_returned` for this client, cleared when the decision is dispatched) and `anchor != home` → `GO_HOME` [`user_returned`]. Bypasses chance and frequency gates.
2. **Contextual** (D2): `contextual.enabled`, `category_changed` since the last tick, new category in `contextual.categories` (default `coding`, `unity`), `curiosity ≥ contextual.min_curiosity` (0.5), a foreground rect is known, and `last_contextual` older than `contextual.cooldown_min` (30) → `APPROACH_WINDOW` with the rect [`curious_about_<category>`]. Bypasses chance, respects `max_per_hour`.
3. **Busy user** (D3): category in `{coding, unity, office}` and `user_idle_seconds < 60`:
   - `anchor != edge`, at least 60 s since the last move and the hourly cap not reached → `GO_EDGE` [`user_busy`]. This deliberately ignores `min_interval_min`, so after a contextual approach Mili "peeks" at the window and leaves at the next tick instead of sitting on it;
   - otherwise → `STAY` [`user_busy`]. No wander and no idle motion while the user is busy.
4. **Frequency gates** for everything below: `now - last_move ≥ movement.min_interval_min` (4) and moves in the last hour `< movement.max_per_hour` (8); otherwise skip to rule 7.
5. **Sleepy:** lifecycle `sleepy` or `sleepiness > 0.7` → `GO_EDGE` if `anchor != edge` with `movement.chance`, else skip to rule 7 [`sleepy`].
6. **Wander:** `energy < 0.2` → skip to rule 7 [`too_lazy`] ("wants to but is too lazy"). Otherwise `score = 0.5·boredom + 0.3·curiosity + 0.2·energy − 0.4·sleepiness`; if `score ≥ movement.wander_threshold` (0.35) and `rng < movement.chance` (0.4) → `WANDER`, `distance = long` if `energy ≥ 0.5` else `short`, `speed = slow` if `energy < 0.4` else `normal` [`wander`].
7. **Idle motion:** `idle_motion.enabled`, frequency gates (`min_interval_min` 5, `max_per_hour` 6), `rng < idle_motion.chance` (0.3), and a semantic motion chosen by mood exists in the model's `motionMap` (§6.6): `sleepiness > 0.6` → `yawn`; `energy > 0.6 and boredom > 0.4` → `stretch`; `curiosity > 0.6` → `look_around`; else none → `IDLE_MOTION` [`idle_motion_<name>`].
8. Otherwise `STAY` [`nothing_to_do`].

A tick produces at most one pet decision per client. `STAY` is logged at debug; everything else at info.

### 6.4 Context: foreground window rectangle (D2)
- `ContextSnapshot` gains `foreground_rect: Optional[Tuple[int, int, int, int]]` (left, top, right, bottom) in **physical pixels**. Default `None`; `ContextSnapshot.unknown()` keeps it `None`.
- `WindowsContextSensor._read_foreground_rect()`:
  - switches the calling thread to per-monitor-v2 DPI awareness with `SetThreadDpiAwarenessContext(-4)` and restores the previous context in `finally`, so `GetWindowRect` returns physical pixels;
  - returns `None` for: no foreground window, the desktop/shell window, a minimized window (`IsIconic`), an empty rect, or a process whose name is Mili's own Electron app (`open-llm-vtuber-electron.exe`, `electron.exe`);
  - never calls `GetWindowText*`.
- The rect is used only for `approach_rect`. It is never put in a prompt and only logged at debug level.
- `category_changed` is computed in the scheduler by comparing the classified category with `PetPresence.last_category`.

### 6.5 Interactions
`PetInteractionHandler` (in `desktop_pet.py`, called by the websocket handler) for a pet-eligible client:

| Event | `BrainEvent` (new) | Mood effect | Reaction key | Extra |
|---|---|---|---|---|
| `click` | `PET_CLICKED` | social_need −0.03, boredom −0.05, happiness +0.02 | `click_happy` if happiness ≥ 0.5 else `click_neutral` | counts toward click burst |
| click burst (`spam_clicks` 5 within `spam_window_s` 10) | `PET_SPAMMED` | happiness −0.05 | `spam` | — |
| `double_click` | `PET_POKED` | social_need −0.05, curiosity +0.05 | `double_click` | wakes from sleep/away (`ensure_active`); sends `stop`; pause movement `attention_pause_min` (3) |
| `drag_start` | — | — | — | the frontend already cancelled movement |
| `drag_end` | `PET_DRAGGED` | boredom −0.05 | `drag_playful` if happiness ≥ 0.5 and energy ≥ 0.4 else `drag_annoyed` | pause movement `drag_pause_min` (5); anchor becomes `home` |

- Reaction keys map to emotion names in `interaction.reactions` (defaults: `click_happy: joy`, `click_neutral: surprise`, `spam: anger`, `double_click: surprise`, `drag_playful: smirk`, `drag_annoyed: anger`). Names that are not in the model's `emotionMap` → no expression (debug log).
- The expression goes through a new `EmotionManager.apply_reaction(name, live2d_model)` (source `interaction`, same semantics as `apply_idle`) and the existing silent payload (`audio: null` + `actions`).
- **No expression** while a conversation is running or reserved, or while lifecycle is `sleep` (except `double_click`, which wakes first). Mood effects still apply.
- Reactions have a cooldown `reaction_cooldown_s` (3); inside it, only mood effects apply.
- Every interaction sets `ClientPresence.last_user_interaction`, so proactive speech waits its quiet period after the user plays with Mili.
- Click on the model keeps the frontend's existing tap motion (`startTapMotion`); the backend does not add a motion to click reactions.

### 6.6 Motion map
- `model_dict.json` entries may have an optional `motionMap`: `{"yawn": {"group": "", "index": 3}, ...}`. Keys are semantic names: `yawn`, `stretch`, `look_around` (3A). Missing map → idle motions are never selected.
- `Live2dModel.set_model()` loads it into `self.motion_map` (default `{}`), with basic validation (group is a string, index a non-negative int; bad entries dropped with a warning). The payload sent to the frontend is unchanged.
- The frontend validates again before playing: `adapter.getMotionCount(group) > index`; otherwise `[Motion] fallback: ... not found` and no-op.
- The `mao_pro` `motionMap` is filled in during implementation after checking the motions visually with `Live2DDebug.playMotion` (the plan has a manual step for this).

### 6.7 Mood events
`events.py` gains `PET_CLICKED`, `PET_SPAMMED`, `PET_POKED`, `PET_DRAGGED`. `mood.EVENT_EFFECTS` gains the effects in §6.5. `PetBrain.notify()` calls `lifecycle.ensure_active()` for `PET_POKED` as it does for `USER_INPUT`. Activity state is not changed by pet events.

---

## 7. Frontend (`D:\Open-LLM-VTuber-Web`, branch `mili-dev`)

### 7.1 New renderer modules in `src/renderer/src/pet/`

| File | Responsibility | Pure? |
|---|---|---|
| `protocol.ts` | Types for §5; `parsePetCommand(msg)` returning a typed command or an error code. | yes |
| `geometry.ts` | Rect helpers; DIP ⇄ client px ⇄ device px ⇄ Live2D logical; model bounds from drawable vertices × model matrix; `clampBoxToArea`. | yes |
| `planner.ts` | `planTarget(command, state, layout, rng)` → target point (DIP) or rejection reason. | yes |
| `motion.ts` | `easeInOutSine`, `durationFor(distance, speed)` (60 / 120 DIP/s, clamped 0.8–8 s), `bobOffset(t)` (4 DIP amplitude, 2 Hz, zero at start/end). | yes |
| `desktop-pet-controller.ts` | State machine `disabled / idle / moving / dragging`; runs one rAF loop only while moving; cancels on drag, mode change, toggle off, conversation start; reports `pet-status`; refreshes hover (§7.5). | no (thin) |
| `pet-settings.ts` | `movementEnabled` and `home` in localStorage (keys `mili.pet.movementEnabled`, `mili.pet.home`); every access wrapped in try/catch with safe defaults (enabled = true, home = position at pet-mode entry). | yes (storage injected) |
| `use-desktop-pet.ts` | React hook: wires websocket messages, mode, AI state and IPC into the controller. Mounted once in `live2d.tsx`. | no |

### 7.2 Main process (`src/main/pet-ipc.ts`, registered from `index.ts`)
- `ipcMain.handle('pet:get-layout')` → `{ windowOrigin: {x, y}, displays: [{id, bounds, workArea, scaleFactor}] }` (DIP). Also pushed as `pet:layout-changed` on `display-added/removed/metrics-changed`.
- `ipcMain.handle('pet:get-cursor')` → `screen.getCursorScreenPoint()` (DIP).
- `ipcMain.handle('pet:screen-to-dip-rect', rect)` → `screen.screenToDipRect(null, rect)` (Windows); returns `null` on error.
- Tray and pet context menu: checkbox **Autonomous Movement** → sends `pet:toggle-movement` to the renderer; the renderer owns the value and echoes it back with `pet:movement-state` so the menu checkbox stays in sync.
- `preload/index.ts` exposes these as `window.api.pet.*`.

### 7.3 Planner rules
- **Current display:** the display whose work area contains the model's bounding-box centre (nearest display if none).
- **Allowed area:** that display's `workArea` shrunk by 8 DIP. The whole model bounding box must stay inside. If the box is larger than the area → `rejected: no_room`.
- `wander`: distance uniform in `short` 80–250 / `long` 250–600 DIP; direction mostly horizontal (vertical component ≤ 30 % of the distance); if the target clamps to within 20 DIP of the start, try the opposite horizontal direction once; still too short → `rejected: no_room`.
- `go_edge`: x so that the box touches the left or right edge of the allowed area (`nearest` picks the closer one); y unchanged (clamped). Anchor after arrival: `edge`.
- `go_home`: stored home; if it lies on no current display → reset home to the current position and `rejected: home_invalid`. Anchor after arrival: `home`.
- `approach_rect`: rect → DIP via IPC; target = box bottom-right just left of the rect's right edge, box bottom at `min(rect.bottom, area.bottom)`; clamped. Invalid or null rect → `rejected: invalid_rect`. Anchor: `free`.
- `stop`: cancels the running move (`cancelled: stopped`).
- A new movement command while moving: the old one is reported `cancelled: superseded`, the new one starts from the current position.

### 7.4 Motion
- Position interpolates with `easeInOutSine`; a vertical bob is added while moving. The model is moved with `LAppAdapter.setModelPosition(x, y)` (same call as the drag).
- No per-frame IPC, no per-frame React state updates.
- If the model or adapter disappears (model reload), the move is cancelled (`cancelled: model_reloaded`).

### 7.5 Click-through refresh
While `moving`, and once at the end of each move: every ≥100 ms call `pet:get-cursor`, convert to canvas coordinates, run the same hit test as `use-live2d-model.ts` (`anyhitTest || isHitOnModel`), and send `update-component-hover('live2d-model', hit)` only when it changes. The hover flag kept in `use-live2d-model.ts` is moved into a small shared store so both paths agree.

### 7.6 Interaction events from `use-live2d-model.ts`
- Existing tap detection (≤200 ms, <5 px) emits `click` with the hit area.
- A second tap within 350 ms of the previous tap emits `double_click` instead of `click`.
- Drag start (existing threshold) emits `drag_start` and tells the controller to cancel; drag end emits `drag_end`, saves the new home and reports `pet-status` with `anchor: home`.
- Events are sent only in Pet Mode and only if the websocket is open. Existing tap motions and drag behavior are unchanged.

### 7.7 Frontend-side enforcement
The controller rejects commands, whatever the backend says, when:
- not in Pet Mode → `not_pet_mode`;
- Autonomous Movement is off → `movement_disabled`;
- the AI state is `thinking-speaking` or `listening` → `conversation`;
- the user is dragging → `user_drag`.

It also cancels a running move on `conversation-chain-start`, drag start, mode change and toggle off. `play_motion` is allowed in any Pet Mode state except while dragging.

---

## 8. Invariants
1. Only the frontend controller moves the model automatically; a user drag always cancels it.
2. At most one movement command in flight per client; every movement command gets exactly one result or times out on the backend.
3. The model's bounding box ends every move inside a display's work area (unless the user dragged it elsewhere).
4. The pet window never keeps catching the mouse over an empty area for more than ~100 ms after Mili moved away.
5. The pet lane never creates, cancels or delays a conversation turn, and never changes Phase 2 decisions.
6. Clients that never sent `pet-hello` receive exactly the same messages as in Phase 2.
7. No LLM call and no screenshot anywhere in Phase 3A.

---

## 9. Logging
- Backend: `[Pet] uid=… → WANDER (wander) id=c-42`, `[Pet] result id=c-42 arrived anchor=edge`, `[Pet] timeout id=…`, `[Pet] interaction click → reaction joy`, `[Context] foreground rect …` (debug), `[Motion] idle yawn`.
- Frontend console: `[Pet] command …`, `[Pet] rejected … reason`, `[Pet] arrived …`, `[Motion] fallback …`.
- `STAY` decisions at debug level only.

---

## 10. Configuration

Added to `pet_brain_config` in **both** templates (`conf.default.yaml`, `conf.ZH.default.yaml`), with validation in `config_manager/pet_brain.py` and in-place update through `service_context.init_pet_brain()` like Phase 2:

```yaml
desktop_pet:
  enabled: True            # effective only if pet_brain_config.enabled and behavior.enabled
  movement:
    enabled: True
    min_interval_min: 4
    max_per_hour: 8
    chance: 0.4
    wander_threshold: 0.35
    post_conversation_quiet_min: 1
    command_timeout_s: 60
  contextual:
    enabled: True
    categories: ["coding", "unity"]
    min_curiosity: 0.5
    cooldown_min: 30
  interaction:
    enabled: True
    reaction_cooldown_s: 3
    spam_clicks: 5
    spam_window_s: 10
    drag_pause_min: 5
    attention_pause_min: 3
    reactions:
      click_happy: joy
      click_neutral: surprise
      spam: anger
      double_click: surprise
      drag_playful: smirk
      drag_annoyed: anger
  idle_motion:
    enabled: True
    min_interval_min: 5
    max_per_hour: 6
    chance: 0.3
```
Validation: probabilities in [0, 1]; intervals ≥ 0; `spam_clicks ≥ 2`; `categories` ⊆ known categories; `reactions` keys ⊆ the six keys above.

---

## 11. Files to change

**Backend (`D:\Open-LLM-VTuber`)**

| File | Change |
|---|---|
| `src/open_llm_vtuber/pet_brain/desktop_pet.py` | new: `PetPresence`, `PetKind`, `PetDecision`, `PetSelector`, `choose_reaction`, `parse_pet_message`, `PetInteractionHandler` |
| `src/open_llm_vtuber/pet_brain/scheduler.py` | pet presences, pet lane after `_select_and_dispatch`, command timeout, `category_changed`, message entry points |
| `src/open_llm_vtuber/pet_brain/context.py` | `foreground_rect` + DPI-aware read |
| `src/open_llm_vtuber/pet_brain/events.py`, `mood.py`, `pet_brain.py` | new events, effects, `ensure_active` for `PET_POKED` |
| `src/open_llm_vtuber/pet_brain/emotion_manager.py` | `EmotionSource.INTERACTION`, `apply_reaction()` |
| `src/open_llm_vtuber/live2d_model.py` | `motion_map` |
| `src/open_llm_vtuber/websocket_handler.py` | handlers for `pet-hello`, `pet-status`, `pet-interaction` |
| `src/open_llm_vtuber/config_manager/pet_brain.py` | `DesktopPetConfig` (the existing `service_context.init_pet_brain()` comparison already updates it in place; a test pins this) |
| `config_templates/conf.default.yaml`, `conf.ZH.default.yaml` | `desktop_pet` section |
| `model_dict.json` | `motionMap` for `mao_pro` (after the manual motion check) |
| `tests/test_desktop_pet_phase3.py` | new |

**Frontend (`D:\Open-LLM-VTuber-Web`)**

| File | Change |
|---|---|
| `src/renderer/src/pet/*` | new modules (§7.1) and `__tests__` |
| `src/renderer/src/hooks/canvas/use-live2d-model.ts` | emit interactions; shared hover store |
| `src/renderer/src/components/canvas/live2d.tsx` | mount `useDesktopPet` |
| `src/renderer/src/services/websocket-handler.tsx` | route `pet-command` to the controller; send `pet-hello` on open |
| `src/main/pet-ipc.ts` (new), `src/main/index.ts`, `src/main/menu-manager.ts`, `src/preload/index.ts`, `src/preload/index.d.ts` | layout, cursor, rect conversion, menu checkbox |
| `package.json`, `vitest.config.ts` | vitest devDependency and `test` script; version `1.2.1-mili.1` |

**Not touched:** backend `frontend/` submodule, `conf.yaml`, ASR/TTS/VAD, MCP, agents, memory, WebSDK/Framework sources.

---

## 12. Testing

**Backend** — stdlib `unittest`, `tests/test_desktop_pet_phase3.py`, injected clock / sensor / RNG:
- config validation and defaults; templates validate; missing section → defaults with `enabled: True` but still gated by `pet_brain_config.enabled: False`;
- `parse_pet_message`: valid/invalid for each type, unknown fields ignored;
- `PetSelector`: every veto, every positive rule, frequency gates, fail-closed context, `too_lazy`, no mutation of presence;
- scheduler: commands only for clients with `pet-hello`; one decision per tick; pending command blocks; timeout clears; disconnect drops pet presence; a client without `pet-hello` gets byte-identical Phase 2 output;
- interactions: mood effects, click burst, cooldown, no expression during conversation or sleep, double-click wakes and pauses, rate limit;
- context: `foreground_rect` None cases (mocked user32), DPI context restored even on error, `GetWindowText*` never referenced (source check like Phase 2);
- `motion_map` loading and bad-entry filtering;
- Phase 1 + 2 suites stay green (228 tests).

**Frontend** — vitest for pure modules: `protocol`, `geometry` (round-trip conversions with dpr 1/1.25/1.5 and a negative window origin), `planner` (every command, clamping, `no_room`, opposite-direction retry, multi-display choice), `motion`, `pet-settings` (storage throwing). `npm run typecheck` and `npm run lint` pass.

**E2E** (backend on spare port 12394 with in-memory config overrides like Phase 2, frontend `npm run dev`):
1. Wander in Pet Mode; model stays inside the work area; `arrived` result.
2. Click-through: after Mili walks away from a still cursor, a click on the old spot reaches the app below.
3. Drag during a move cancels it; new home saved; movement paused.
4. Click / double-click / click spam reactions; nothing during a conversation.
5. Open Unity or VS Code with high curiosity → `approach_rect` next to the window; while typing in it → `go_edge` then stay.
6. Toggle off in tray → commands rejected `movement_disabled`; web frontend at `http://127.0.0.1:12394` receives no `pet-command`.
7. Fullscreen app → no movement.
8. Two monitors (if available): moves stay on the current display; `approach_rect` on the second monitor.

---

## 13. Rollout order (for the plan)
1. Repo setup (fork, branch, Node, `npm ci`, vitest).
2. Backend config, events, mood, motion map, context rect.
3. Backend `desktop_pet.py` (parser, selector, reactions).
4. Backend scheduler pet lane and websocket handlers.
5. Frontend pure modules with tests.
6. Frontend main-process IPC and menu.
7. Frontend controller, interactions, websocket wiring.
8. `mao_pro` motionMap (manual check), E2E, summary docs.

---

## 14. Risks
- **Mixed DPI across monitors:** a single window spanning monitors with different scale factors can render or hit-test oddly on Windows. This is inherited from upstream; 3A keeps every move on one display and E2E notes the result.
- **Hit-test cost:** `isHitOnModel` loops over all drawables; at 10 Hz only while moving this is negligible.
- **Upstream drift:** the frontend fork diverges from `Open-LLM-VTuber-Web`; changes are kept in new files where possible, with small hooks in existing ones.
- **Two frontends:** the backend `frontend/` submodule (web build) is not updated; the Mili features exist only in the Electron app. This is intended.
- **Installer replacement:** the rebuilt app replaces v1.2.1; keep the old installer for rollback.
- **Known Phase 2 issues still open:** the websocket send race in `TTSTaskManager` could also hit a silent reaction payload sent during TTS; reactions are suppressed during conversations, which avoids the common case.

---

## 15. Phase 3B preview (not designed here)
Desktop mischief as a separate action domain behind `PermissionGuard` at INTERACT level: moving desktop icon positions (visual only, via the desktop `SysListView32`), snapshot and **restore layout**, whitelist/blacklist of icons, and a global off switch. It needs cross-process memory access on Windows and will get its own spec after 3A is stable.
