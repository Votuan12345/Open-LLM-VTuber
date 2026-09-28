# PROJECT_CONTEXT.md

## Project

- Project: **Mili Desktop AI Companion**
- Base project: **Open-LLM-VTuber**
- Backend fork: `Votuan12345/Open-LLM-VTuber`
- Frontend source will be forked separately when Phase 3 starts.
- Platform: **Windows-only**
- Current development branch: `mili-dev`

## Git

- `origin` → `https://github.com/Votuan12345/Open-LLM-VTuber.git`
- `upstream` → `https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git`
- Keep `main` close to upstream when practical.
- Development/custom work goes on `mili-dev`.
- Do not force-push, rewrite history, delete branches, change remotes, or convert submodules into ordinary folders without explicit approval.

## Current Local Setup

- Python in project `.venv`: **3.10.21**
- System Python also has 3.12.10 installed; the project currently uses the 3.10.21 virtual environment.
- `uv`: **0.12.19**
- FFmpeg: **9.0.2**
- LLM backend: **Ollama**
- ASR: **sherpa_onnx_asr**
- TTS: **edge_tts**
- Current server command: `uv run run_server.py`
- Current server port: `12393`
- Desktop app/Pet Mode works with the currently built Electron frontend.

## Frontend Situation

The backend repository currently contains the frontend as an initialized Git submodule containing a **built bundle**, not the Electron/React source needed for deep Pet Mode changes.

For Phase 3 and later desktop behavior:
- Fork/clone `Open-LLM-VTuber/Open-LLM-VTuber-Web`.
- Keep it as a separate repository under the user's account.
- Use its source branch/repository, not only the build bundle.
- Do not modify the frontend/submodule during Phase 1–2 unless explicitly required.

## Vision

Mili should become a **living anime desktop companion**, not merely a chatbot with a Live2D model.

Desired capabilities over time:
- Anime/Live2D character
- Multiple expressions and motions
- Text + voice conversation
- Natural personality
- Mood and internal state
- Ability to proactively speak at appropriate times
- Idle behaviors
- Ability to stay silent/do nothing
- Autonomous desktop movement
- Context-aware behavior
- Light playful desktop interaction
- Work assistance
- Tools/MCP integration
- Long-term memory
- Strong permission/safety boundaries
- Strong local/offline orientation where practical

Target feeling:
> A companion that appears to be living on the desktop with the user, rather than a chatbot placed on the screen.

## Personality – Mili

Target personality:
- Anime-like but natural, not a VTuber parody.
- Friendly and cheerful.
- Slightly mischievous/playful.
- Has a distinct personality.
- Sometimes teases the user.
- Sometimes takes initiative.
- Can mildly disagree or push back.
- Can be talkative or quiet depending on context.
- Can want to interact, but can also want to be left alone.
- Can be lazy, sleepy, curious, focused, bored, etc.
- Can genuinely choose to do nothing.
- Should not constantly introduce herself.
- Should not repeatedly mention that she is an AI.
- Should not call the user "User" once the user's name is known.
- Should not always agree with the user.
- Should not always sound enthusiastic.
- Personality should remain coherent rather than randomly changing between messages.

Important distinction:
- **Persona** = who Mili is / how she tends to think and speak.
- **Behavior system** = what Mili decides to do at the current moment.

### Internal contradictions

Mood should allow conflicting tendencies.

Examples:
- high curiosity + high focus → wants to ask something but tries not to interrupt
- high boredom + low energy → wants stimulation but feels too lazy
- high sleepiness + active conversation → still wants to talk but is sleepy

These contradictions are intentional and should contribute to natural behavior.

## Core Architecture Goal

Desired high-level architecture:

```text
                         LLM
                          ↓
                  Intent / Response
                          ↓
                       PetBrain
              ┌───────────┼────────────┐
              ↓           ↓            ↓
            Mood        Context     Permission
              └───────────┼────────────┘
                          ↓
                   Behavior System
              ┌───────────┼────────────┐
              ↓           ↓            ↓
           Live2D       Voice       Desktop Pet
```

Rules:
- LLM is not the game loop.
- LLM should not directly control Windows/desktop.
- LLM can provide high-level intent/action proposals.
- Deterministic code decides whether actions are allowed.
- Permission must be enforced at the actual execution boundary.
- Prefer small integration hooks over broad rewrites.
- Preserve existing contracts when practical.
- Keep modules testable and easy to disable/rollback.

## Permission Model

### READ
May be automatic within an allowlist:
- time
- user/activity metadata
- active application
- system metadata
- explicitly allowed files/data

### INTERACT
May be automatic within an allowlist:
- opening applications
- focusing windows
- moving the Mili desktop pet
- cursor interaction
- light playful desktop actions

### DESTRUCTIVE
Must require explicit confirmation:
- deleting files
- modifying files
- renaming files
- arbitrary command execution
- installing/uninstalling software
- killing processes
- registry changes
- actions that may cause data loss or system changes

Security rules:
- Do not rely on LLM instructions/prompts as the security boundary.
- Permission is enforced in code/tool execution.
- Fail closed if the permission check errors.
- Unknown/unclassified tools should not silently become allowed.

## Context Awareness

Mili may eventually observe lightweight metadata:
- whether the user is active
- idle duration
- active application
- current time
- fullscreen state
- current process category

Examples:
- Unity open → user probably working in Unity
- VS Code open → user probably coding
- user idle for a while → possibly away from machine

### Screen capture / perception

Do not capture screenshots continuously.

Screenshots/heavy perception should require:
- explicit user request, or
- a specific allowed workflow/tool that actually needs it.

Do not continuously send screenshots to the LLM.

## Desktop Behavior Vision

Movement should eventually support a combination of:
1. Random wandering
2. Waypoint/screen-area movement
3. Contextual movement

Potential actions:
- `MoveTo`
- `MoveRandomly`
- `Wander`
- `Stop`
- `FaceDirection`
- `GoToScreenEdge`
- `ReturnToHome`
- `FollowCursor`
- `Hide`
- `Sleep`

Movement should:
- remain inside allowed screen areas
- be smooth
- avoid teleport-like motion unless intentional
- use cooldowns
- avoid moving too often
- support enable/disable settings
- support multi-monitor if practical

Desktop mischief can eventually include light actions such as:
- moving desktop icon positions
- approaching an icon
- interacting with the cursor
- standing beside a window
- other harmless playful behaviors

Desktop mischief must remain separate from destructive file/system operations.

If desktop icon positions are manipulated:
- modify visual/icon placement, not the shortcut/file itself
- never infer deletion/modification permissions from icon movement
- consider providing a restore-layout function

## Mood / Lifecycle

Mood should be real state in code, not prompt-only.

Candidate mood variables:
- happiness
- energy
- curiosity
- boredom
- social_need
- focus
- sleepiness

Lifecycle:
- WakeUp
- Active
- Idle
- Sleepy
- Sleep
- Away

Daily rhythm:
- Morning
- Afternoon
- Evening
- Late night

Mood/lifecycle can influence:
- speech frequency
- voice tone
- expressions
- animation choice
- willingness to interact
- movement frequency
- idle behavior

## Emotion / Live2D

Desired flow:

```text
LLM output
   ↓
Emotion extraction
   ↓
EmotionManager
   ↓
Live2D expression/motion
```

Important:
- Keep the existing frontend contract `Actions.expressions`.
- Avoid guessing emotion names solely from expression indexes when multiple tags share an index.
- Preserve the original LLM emotion tag when possible.
- Example: `[smirk]` and `[joy]` may map to the same expression index, but internal state should still distinguish `"smirk"` from `"joy"`.
- Invalid expression indexes can fall back to `neutral`.
- Ordinary bracketed text such as `[note]` should not automatically become an emotion.
- Avoid multiple independent emotion parsers becoming competing sources of truth.

## Voice

Current stack:
- LLM = Ollama
- ASR = sherpa_onnx_asr
- TTS = edge_tts

Desired future pipeline:

```text
Microphone
    ↓
STT
    ↓
LLM
    ↓
Emotion / Intent
    ↓
Response
    ↓
TTS
    ↓
Live2D
```

TTS should remain replaceable so that a local TTS backend can be added later.

## Work Assistant

Mili should eventually be able to assist with:
- coding
- debugging
- Unity
- reading allowed files
- seeing the screen when explicitly permitted
- searching for information
- opening applications
- MCP/tools
- reminders/task support
- workflow assistance

All system interaction must remain behind the permission layer.

## Memory

Short-term:
- current conversation context

Long-term candidates:
- user name
- preferences
- important facts
- habits
- useful recurring information

Memory entries should ideally support:
- importance
- timestamp
- category
- optional expiration
- user control

Do not store everything blindly.

## Performance

Never turn Mili into a high-frequency LLM loop.

Rules:
- no LLM call every frame
- no continuous screenshot loop
- no continuous heavy perception
- proactive speech needs cooldown/frequency control
- idle behavior should be lightweight
- use event-driven logic where practical
- use low-frequency ticks for background state updates
- avoid unnecessary allocations in long-running idle loops

## Current Phase Status

### Phase 1 – Core foundation

Implemented/being finalized:
- `pet_brain/`
- PetBrain
- Mood
- Lifecycle
- EmotionManager gatekeeper
- Permission Core
- PetBrain config
- event definitions
- tests

Latest reported verification (details: `mili_docs/PHASE_1_SUMMARY.md`):
- **43/43 unit tests pass**
- Ruff clean on changed files
- E2E with real Ollama works with PetBrain enabled and disabled
- Existing `Actions.expressions` / websocket contract remains unchanged
- Single emotion parser: `Live2dModel.parse_emotions()` keeps both tag name and expression index; unknown tags are ignored + logged
- PetBrain can be disabled via config to preserve old behavior
- No frontend source changes yet

### Phase 1 scope clarification

Phase 1 intentionally does **not** yet implement the full living-behavior loop.

Not yet implemented:
- Behavior Scheduler
- Idle Behavior as a real backend scheduler
- New backend proactive-speaking engine
- Autonomous desktop movement
- Desktop mischief
- Full context-awareness loop

These are for later phases.

### Phase 1 architecture notes

Current PetBrain ownership is currently shared in the backend architecture. This is acceptable for current usage (one user/client), but future architecture may need:

```text
CompanionBrain
    = desktop-global companion state

SessionPresence
    = per-connection/session transient activity
```

Group conversations may eventually need separate emotion state per character.

Do not refactor this ownership split unless explicitly planned for a later phase.

## Planned Phases

### Phase 1 – Core foundation
- PetBrain
- Mood
- Lifecycle
- EmotionManager
- Permission Core

### Phase 2 – Living behavior
- Behavior Scheduler
- Idle Behavior
- Proactive Speaking
- User activity awareness
- Context awareness
- Daily rhythm
- Mood transitions

### Phase 3 – Desktop Pet
- Autonomous movement
- Wandering
- Waypoints
- Contextual movement
- Desktop interaction
- Playful desktop actions
- Electron/Windows Pet Mode source integration

Requires the source repository for `Open-LLM-VTuber-Web`.

### Phase 4 – Voice
- TTS integration improvements
- voice states
- lip sync if supported
- voice/emotion integration
- local TTS option

### Phase 5 – Work Assistant
- tools
- MCP
- screen perception
- application interaction
- permission/confirmation workflow

### Phase 6 – Memory
- long-term memory
- preferences
- habits
- personality continuity

### Phase 7 – Polish
- settings UI
- debug panel
- behavior tuning
- performance optimization
- persistence

## Development Rules for Claude Code

Before changing architecture:
1. Read the actual repository source.
2. Locate real code paths and existing contracts.
3. Do not assume APIs/files exist.
4. Prefer reuse over rewrite.
5. Keep changes modular.
6. Explain proposed architecture before large changes.
7. Work phase-by-phase.
8. Test after each phase.
9. Report files changed and regression risks.
10. Keep a rollback path.

Do not:
- rewrite the project wholesale
- modify unrelated modules without reason
- alter Git remotes/history without approval
- modify the frontend/submodule before its designated phase
- let LLM output bypass permission enforcement
- create high-frequency LLM/background loops

## Claude Handoff Convention

At the end of each phase, produce a short summary containing:
- What was implemented
- What was intentionally deferred
- Files changed
- Tests and E2E results
- Known issues
- Architecture decisions
- TODOs for future phases
- Exact current Git state/branch
- Any deviations from this document

Store useful phase summaries in the repo, for example:

```text
docs/
├── PROJECT_CONTEXT.md
├── PHASE_1_SUMMARY.md
├── PHASE_2_SUMMARY.md
└── ...
```

The phase summary should not replace the permanent project context; it records the history/decisions of that phase.

## Current Priority

Finish and verify Phase 1 cleanly.

Before committing:
- verify EmotionManager behavior and tests
- verify Permission Core boundary
- verify PetBrain enable/disable behavior
- keep frontend untouched
- keep `conf.yaml` safe/rollback-friendly
- review diff

Then commit Phase 1 to `mili-dev`.

After that, move to Phase 2:
**Behavior Scheduler + Idle Behavior + Proactive Speaking + User Activity/Context.**

Do not start Phase 3 desktop movement until the frontend source fork/integration is ready.
