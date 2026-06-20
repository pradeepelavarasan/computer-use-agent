# Computer Use Agent

A multi-agent native desktop automation platform for macOS. Built to test and demonstrate various GUI control techniques — AppleScript scripting, macOS Accessibility tree navigation, deterministic hotkeys, and vision-based Set-of-Marks interaction — that an autonomous agent can perform inside live native desktop applications.

This project extends the Directed Acyclic Graph (DAG) multi-agent orchestrator from [Browser Agent for Shopping](https://github.com/pradeepelavarasan/Browser-Agent-for-Shopping) with a new **Computer** skill that drives native macOS applications. Where the browser skill controlled web pages via Playwright, the computer skill uses a five-layer cascade backed by AppleScript and the OS-level Accessibility API.

🎥 YouTube Demo: [LINK TO BE ADDED]

![Banner Image](assets/Computer%20use%20banner%20image.png)

---

## DAG-Based Architecture

This system inherits and expands upon the robust concurrent DAG (Directed Acyclic Graph) architecture introduced in the [Browser Agent for Shopping](https://github.com/pradeepelavarasan/Browser-Agent-for-Shopping). The graph orchestrator (`flow.py`) is carried forward unchanged — the key addition is the **Computer** skill node, which replaces the browser as the primary execution unit for native desktop tasks. A lightweight web UI server (`computer_use_agent/server.py`) sits on top, providing a browser-based interface for submitting tasks and watching the live DAG:

1. **Planner**: Receives the user query, decides the execution plan, and compiles the graph structure — emitting `computer` nodes with `app_name`, `bundle_id`, `goal`, and optional `hotkeys` metadata.
2. **Computer**: The core new skill in this project. Drives native macOS applications through a five-layer cascade, dynamically selecting the most cost-effective interaction method for each task and application type.
3. **Critic**: Evaluates and validates the computer skill's output and the overall execution path to ensure goal alignment and verify the action had the intended effect.
4. **Browser (Optional)**: The browser skill from the [Browser Agent for Shopping](https://github.com/pradeepelavarasan/Browser-Agent-for-Shopping) is carried forward and remains available for web-based sub-tasks within a multi-step workflow.
5. **Formatter**: Structures and emits the final result matching the target output schema.

---

## Computer Agent Cascade (Five Layers)

To interact with native macOS applications, the Computer Agent employs a five-layer cascade backed by two complementary substrates:

- **AppleScript / System Events** — macOS-native keystroke injection and app scripting dictionary. Built into macOS; no extra binary required.
- **cua-driver** — OS-level Accessibility (AX) tree access and screenshot capture via a Unix socket CLI (`cua-driver serve`). Required for Layers 1, 4, and 5.

The cascade tries each layer in order and stops at the first success, always escalating from the least expensive to the most powerful method. Before any layer runs, a **Pre-flight** step fires an AppleScript `activate` command to bring the target app to the foreground — this always executes and costs nothing.

### Layer 1: AX Extract (Read-Only)
* **Description**: Passive read of the macOS Accessibility tree via `cua-driver get_window_state`. Returns the current UI state as structured text with no action taken.
* **Cost**: Negligible (zero LLM calls; read-only).
* **Needs cua-driver**: Yes.
* **Usage**: Ideal for read-only goals ("what is currently on screen?", "what is the value of this cell?") where no interaction is required. If the goal is satisfied by a scan alone, the cascade stops here.

### Layer 2: AppleScript
* **Description**: An LLM generates a targeted AppleScript command using the app's own scripting dictionary. The script talks directly to the app's data model — one command does what might take multiple UI clicks, and it is immune to UI reflows or version changes.
* **Cost**: Low (one LLM call for script generation).
* **Needs cua-driver**: No.
* **Usage**: Suitable for scriptable first-party Apple apps — Mail, Notes, Calendar, Reminders, Contacts, Numbers, Pages, Keynote, Finder, and Safari. Modern third-party apps (Telegram, Slack, VS Code) are not scriptable; the cascade falls through to Layer 3 or 4 automatically.

### Layer 3: Deterministic Hotkeys
* **Description**: The Planner pre-computes the exact keystroke sequence needed for a well-understood action and supplies it in the node's `hotkeys` metadata. The skill executes the sequence directly via System Events — zero LLM cost, zero cua-driver.
* **Cost**: Negligible (zero LLM calls).
* **Needs cua-driver**: No.
* **Usage**: Best for predictable, stable operations (arithmetic sequences, known menu shortcuts) where the Planner can reliably pre-compute the key sequence. Every action still follows a Scan-Act-Verify loop to confirm the state changed.

### Layer 4: AX Tree + LLM (Scan-Act-Verify)
* **Description**: `cua-driver` reads the live OS Accessibility tree and presents the interactive elements to a text LLM, which picks the element to act on. Each turn follows a **Scan → Act → Verify** loop: scan current UI state, take the action, re-scan to confirm the state changed. Repeats for up to 12 turns.
* **Cost**: Moderate (one text LLM call per turn).
* **Needs cua-driver**: Yes.
* **Usage**: The general-purpose interactive layer. Covers the vast majority of native macOS apps that expose a standard AX tree. If the post-action scan shows no state change, the layer returns `None` and the cascade escalates to Layer 5.

### Layer 5: Vision / Set-of-Marks (SoM)
* **Description**: `cua-driver` captures a live screenshot, overlays numbered bounding boxes (marks) over interactive elements, and feeds the annotated image to a vision LLM. The VLM picks the mark to interact with and the action to take. Each turn follows the same **Scan → Act → Verify** loop for up to 12 turns.
* **Cost**: High (vision LLM reasoning per turn).
* **Needs cua-driver**: Yes.
* **Usage**: The final fallback for apps with no accessible UI tree — canvas-based apps, games, and applications with opaque AX trees (e.g., Figma, Electron apps without a CDP debugging port, Qt apps). Most expensive layer; used only when all others fail or when the goal is inherently visual.

---

## Interactive Examples

*(Coming soon — examples will document real task sessions across different macOS application types, demonstrating which layer the cascade chose and why.)*

---
