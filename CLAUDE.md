# EAG Session 10 — Computer Agent

## Purpose

Session 10 extends the Session 9 Browser Agent with a `computer` skill that drives
native desktop applications on macOS. The browser skill controls web pages via Playwright;
the computer skill controls native apps via two complementary substrates:

- **System Events / AppleScript** — macOS-native keystrokes and app scripting dictionary (no extra binary needed)
- **cua-driver** — OS-level AX tree access and screenshot via `cua-driver call <tool>` (Unix socket CLI, not the Python SDK)

A web UI server (`computer_use_agent/`) sits on top of `flow.py` and provides a browser-based
interface for submitting tasks and watching the live DAG.

Built for the EAG V3 course, Session 10.

---

## Architecture Overview

```
Browser (http://localhost:8002)
    │
    ▼
computer_use_agent/server.py   ← web UI server (port 8002)
    │  spawns subprocess per query
    ▼
flow.py  (graph orchestrator — unchanged from S9)
    │
    ▼
skills.py  (dispatch)
    │
    ├── computer skill (code/computer/skill.py) ← NEW in S10
    │       Pre-flight:  AppleScript activate   (always; brings app to foreground)
    │       Layer 1:     Extract                (passive AX read — needs cua-driver)
    │       Layer 2:     AppleScript            (scriptable apps — no cua-driver)
    │       Layer 3:     Deterministic hotkeys  (planner-supplied — no cua-driver)
    │       Layer 4:     AX tree + LLM          (Scan-Act-Verify — needs cua-driver)
    │       Layer 5:     Vision / Set-of-Marks  (screenshot + VLM — needs cua-driver)
    │
    └── browser skill (code/browser/skill.py) ← unchanged from S9
```

**Key point:** Layers 2 and 3 work on any Mac with no cua-driver binary.
Only Layers 1, 4, and 5 require `cua-driver serve` to be running.

---

## The Two Substrates

| | AppleScript / System Events | cua-driver (CLI) |
|---|---|---|
| **What it sees** | App's own scripting dictionary OR raw keystrokes | OS accessibility tree (roles, rects, element indices) |
| **Works on** | Any Mac; scripting dictionary only for supported apps | Any app that exposes an AX tree |
| **Used in** | Pre-flight + Layers 2 and 3 | Layers 1, 4, 5 |
| **Needs binary** | No — built into macOS | Yes — `cua-driver serve` (Unix socket) |
| **Example** | `make new note with properties {name:"Hello"}` | Scan element 5 → click it → re-scan |

---

## 5-Layer Cascade (Computer Skill)

The cascade tries each layer in order and stops at the first success.

| Layer | Name | Needs cua-driver? | LLM calls | When it runs |
|---|---|---|---|---|
| Pre-flight | AppleScript activate | No | 0 | Always — brings app to foreground |
| **1** | **Extract** | Yes | 0 | Read-only goals ("what is on screen?") |
| **2** | **AppleScript** | No | 1 (script generation) | App has a scripting dictionary (Mail, Notes, Calendar, Numbers, Finder, Pages, Keynote…) |
| **3** | **Deterministic hotkeys** | No | 0 | Planner supplied an exact keystroke sequence in metadata |
| **4** | **AX tree + LLM** | Yes | Multiple (Scan-Act-Verify × up to 12 turns) | Interactive goal, app exposes AX tree |
| **5** | **Vision / Set-of-Marks** | Yes | Multiple (vision LLM × up to 12 turns) | AX tree empty (canvas app, game, Figma) OR goal is inherently visual |

### Why this order?

- **Layer 1 (Extract)** comes first because it costs nothing — if the goal is read-only, return the AX text immediately.
- **Layer 2 (AppleScript)** comes before hotkeys because AppleScript talks to the *app's data model directly* — one command does what might take five UI clicks, and it can't break on UI reflows or version changes. It is architecturally stronger when available.
- **Layer 3 (Deterministic hotkeys)** comes next — zero LLM cost, zero cua-driver, but requires the planner to already know the exact key sequence. Used for well-understood patterns (arithmetic, menu shortcuts).
- **Layer 4 (AX tree + LLM)** is the general-purpose interactive layer: cua-driver reads the UI tree, a cheap text LLM decides which element to act on, and each turn runs Scan → Act → Verify before the next.
- **Layer 5 (Vision)** is the fallback for apps with no accessible UI tree (canvas, games, Figma). Most expensive — avoid unless necessary.

### Scan-Act-Verify — applies to ALL action layers (2, 3, 4, 5)

Every layer that takes an action follows the same three-step loop.
Skipping any step causes stale-state failures — the professor's core invariant.

```
SCAN   →  cua-driver get_window_state  (snapshot current UI state)
ACT    →  perform the action (script / keystrokes / click / coordinate)
VERIFY →  cua-driver get_window_state again  (confirm state changed)
           if no change detected → return None → cascade to next layer
```

What differs between layers is **who decides the action**, not the loop itself:

| Layer | Who decides the action | Loop turns |
|---|---|---|
| 1 (Extract) | Nobody — read-only, no action | 1 scan only |
| 2 (AppleScript) | LLM generates the script (one call) | 1 SAV round |
| 3 (Hotkeys) | Planner pre-computed the sequence (zero LLM) | 1 SAV round |
| 4 (AX tree + LLM) | LLM reads AX tree and picks an element each turn | Up to 12 SAV turns |
| 5 (Vision / SoM) | Vision LLM reads screenshot and picks coordinates each turn | Up to 12 SAV turns |

**Why the verify matters:** if the scan and verify show identical state, the action had no visible effect — the keystroke landed on the wrong app, the AppleScript did nothing, or the element was already gone. Returning `None` at that point lets the cascade try the next, smarter layer instead of falsely claiming success.

### Scriptable apps (Layer 2 targets)

Apps with an AppleScript scripting dictionary: Mail, Notes, Calendar, Reminders, Contacts, Numbers, Pages, Keynote, Finder, Safari, and most first-party Apple apps. Modern third-party apps (Telegram, Slack, VS Code) are NOT scriptable — they go to Layer 3 or 4.

---

## Project Structure

```
EAG Session10 Computer Agent/
├── pyproject.toml              ← project dependencies (at root, not inside code/)
├── CLAUDE.md                   ← this file
├── computer_use_agent/         ← NEW: web UI server
│   ├── server.py               ←   HTTP server (port 8002), spawns flow.py per query
│   ├── index.html              ←   search input + result card + Stop button
│   ├── graph_viewer.html       ←   live DAG viewer (vis-network, polls /api/graph-status)
│   └── static/results.css     ←   dark theme styles
├── code/
│   ├── computer/               ← NEW: computer skill package
│   │   ├── daemon.py           ←   cua-driver connection + activate_app()
│   │   ├── applescript.py      ←   is_scriptable(), keystroke_sequence(), read_ax_value()
│   │   ├── highlight.py        ←   screenshot annotation (set-of-marks)
│   │   ├── driver.py           ←   A11yDriver, VisionDriver
│   │   └── skill.py            ←   4-layer cascade bypass skill
│   ├── computer_mcp_server.py  ← NEW: FastMCP server exposing computer tools
│   ├── browser/                ← S9 browser skill (unchanged)
│   ├── prompts/
│   │   ├── computer.md         ← NEW: planner prompt for computer skill
│   │   └── planner.md          ← updated: computer skill instructions added
│   ├── agent_config.yaml       ← computer skill entry added
│   ├── skills.py               ← computer bypass dispatch added
│   ├── schemas.py              ← ComputerOutput dataclass added
│   └── flow.py                 ← unchanged from S9
├── gateway/                    ← llm_gatewayV9 (port 8110)
├── shopping_agent/             ← S9 shopping pipeline (carried forward, not active)
└── logs/                       ← per-session output (screenshots, DAG, log.md)
```

---

## Ports

| Service | Port |
|---|---|
| Computer Use Agent web UI | **8002** |
| Shopping Agent (S9, inactive) | 8001 |
| LLM Gateway (S10) | **8110** |
| LLM Gateway (S9) | 8109 |

---

## How to Run

### One-time setup

```bash
# From the project root:
cd "/Users/pradeep/Library/CloudStorage/OneDrive-Personal/ML/2026 ML Projects/EAG Session10 Computer Agent"

# Install dependencies (pyproject.toml is at project root)
uv sync

# Set API keys in gateway/.env (copy from S9 if needed)
```

### Start the web UI (primary way to use the agent)

```bash
# From the project root:
python computer_use_agent/server.py
# → http://localhost:8002
```

- Gateway (port 8110) auto-starts on the first query
- Press **Enter** to submit a query; **Shift+Enter** for newline
- Click **⚙️ How do I work?** to open the live DAG viewer in a new tab
- Click **⏹ Stop task** to kill a running task without stopping the server

### Stop the server

```bash
# From any terminal:
kill -9 $(lsof -ti :8002)
```

Or press **Ctrl+C** in the terminal where the server is running.

### Run a query directly from the CLI

```bash
# From the project root:
uv run code/flow.py "open Calculator and compute 42 × 18"
```

### Optional: cua-driver (for Layer 2b AX tree and Layer 3 Vision)

Layer 2a works without cua-driver (System Events). To unlock Layers 2b and 3:

```bash
# Start the daemon (keep running in a separate terminal):
cua-driver serve

# One-time macOS permission grant:
cua-driver permissions grant   # accept both TCC dialogs
```

---

## flow.py Lifecycle

`flow.py` is **not** a long-running daemon. The server spawns it fresh as a subprocess
for every query and it exits when the DAG completes. This means:

- Changes to any file under `code/` (skill.py, prompts/, etc.) take effect on the **next query** — no restart needed
- Only `server.py` itself requires a server restart when changed
- Static files (`index.html`, CSS) only need a browser refresh

---

## NodeSpec Metadata for the Computer Skill

```yaml
# Planner emits computer nodes with these metadata fields:
app_name:   "Calculator"              # display name — for AppleScript activation
bundle_id:  "com.apple.calculator"   # macOS bundle ID — for cua-driver launch_app
goal:       "compute 42 × 18 and return the result"
hotkeys:    [{keys: ["4"]}, ...]      # optional: Layer 3 (planner pre-computes the sequence)
force_path: "a11y"                   # optional: skip to a specific layer (testing only)
```

- If the app is **scriptable** (e.g. Notes), Layer 2 fires automatically — no `hotkeys` needed.
- If the planner knows the exact key sequence, add `hotkeys` to trigger Layer 3 directly.
- If neither is provided and the goal is interactive, the skill falls through to Layer 4 (AX tree + LLM).

---

## Common Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| All layers skipped, `server_unavailable` error | cua-driver daemon not running | Run `cua-driver serve` in a terminal first |
| Daemon takes 30 s to detect | cua-driver wasn't running when query started | Pre-start the daemon; detection is instant when already running |
| `element_count=0` on scan (Layer 4/5) | TCC Accessibility permission not granted | `cua-driver permissions grant`, then restart daemon |
| `element_count=0` on Electron app | AX tree opaque without CDP debugging port | Pass `electron_debugging_port` in metadata |
| `element_count=0` on Qt app | QT accessibility disabled | Set `QT_ACCESSIBILITY=1` at app launch |
| Layer 2 (AppleScript) silently does nothing | App not actually scriptable | Skill falls through to Layer 3/4 automatically |
| Layer 3 (hotkeys) sends to wrong contact/chat | Blind keystrokes, no Scan-Act-Verify | Planner should NOT provide hotkeys for navigation tasks; let Layer 4 handle it |
| Ctrl+C doesn't stop server | Worker thread blocked on subprocess | Use `kill -9 $(lsof -ti :8002)` instead |

---

## Session 9 Legacy

Carried forward unchanged — do not modify:
- `code/browser/` — browser skill (extract → selectors → a11y → vision)
- `shopping_agent/` — shopping UI (port 8001, not active for S10 tasks)
- `code/mcp_server.py` — web_search, fetch_url, search_knowledge tools

---

## Assignment Tasks (Session 10)

Three tasks to be chosen from the six options. Constraints: at least one vision task,
one Electron task, one zero-vision task.

1. **Calculator / arithmetic** — Layer 2a deterministic (System Events, no cua-driver)
2. **Spreadsheet or Notes** — Layer 2b AX tree + LLM
3. **Electron app** (VS Code, Slack, Cursor) — CDP path via `electron_debugging_port`
4. **Canvas / game** (Figma, browser game) — Layer 3 pure vision
5. **Email or message draft** — Layer 2b with post-send verification
6. **Multi-app workflow** — context switch between two apps

*Task-specific prompts will be added to `code/prompts/` when tasks are selected.*
