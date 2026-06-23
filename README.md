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

We showcase the computer agent's capabilities across native macOS desktop applications, demonstrating different interaction paths through the five-layer cascade.

---

### 1. Create a Word Document about Harry Potter (APPLESCRIPT)

### Session Log: s9-2026-06-20_08-53-41

#### 1. Original User Goal
> Create a new Word document and insert a paragraph about Harry Potter.

![Task Initiated](assets/1.%20Task%20initiated.png)

#### 2. Planner DAG
![Planner DAG](assets/1.%20DAG.png)

#### 3. Computer Path Chosen
The Computer cascade chose the **APPLESCRIPT** interaction path.

#### 4. Computer Actions

**App:** Microsoft Word  
**Goal:** Create a new blank document, type a paragraph about Harry Potter, and save the document.

**Layer 2 — AppleScript:**
```
SCAN complete: element_count=503
VERIFY: element_count=525
```

**Result:** Created and saved Harry Potter document

#### 5. Final Result

I have successfully created a new Microsoft Word document containing a paragraph about Harry Potter and saved the file.

![Proof of Completion](assets/1.%20Proof%20of%20completion.png)

![Task Completed](assets/1.%20Task%20completed.png)

#### 6. Performance Summary

| Node | Skill | Status | Provider | Model | Duration | Tokens In | Tokens Out |
|---|---|---|---|---|---|---|---|
| `n:1` | planner | ✓ complete | gemini_lite_2 | — | 2,049 ms | 0 | 0 |
| `n:2` | computer | ✓ complete | gemini_lite_1 | gemini-3.1-flash-lite | 13,120 ms | 116 | 75 |
| `n:4` | critic | ✓ complete | gemini_lite_1 | — | 1,025 ms | 0 | 0 |
| `n:3` | formatter | ✓ complete | gemini_lite_2 | — | 4,530 ms | 0 | 0 |
| **TOTAL** | | | | | **20,724 ms** | **116** | **75** |

---

### 2. Create a Paneer Butter Masala Shopping List in Notes (HOTKEYS)

### Session Log: s9-2026-06-20_09-31-29

#### 1. Original User Goal
> Can you search online to understand the ingredients for making paneer butter masala? Based on that, create a shopping list as a new note in my notes app on my mac.

![Task Initiated](assets/2.%20Task%20Initiated.png)

#### 2. Planner DAG
![Planner DAG](assets/2.%20DAG.png)

#### 3. Computer Path Chosen
The Computer cascade chose the **HOTKEYS** interaction path.

#### 4. Computer Actions

**App:** Notes  
**Goal:** Create a new note titled 'Paneer Butter Masala Shopping List' and add the list of ingredients obtained from the research.

**Layer 2 — AppleScript (attempted, fell through):**
```
SCAN complete: element_count=441
```

**Layer 3 — Hotkeys:**
```
generated 18 keystrokes for goal
SCAN complete: element_count=441
VERIFY: element_count=442
```

**Keystrokes / Actions:**
- `cmd+n`
- type `Paneer Butter Masala Shopping List`
- `Return`
- type `- 200g Paneer`
- `Return`
- type `- 3 Tomatoes`
- `Return`
- type `- 1 Onion`
- `Return`
- type `- 1/2 cup Cashews`
- `Return`
- type `- 1 tbsp Ginger Garlic Paste`
- `Return`
- type `- 1/2 cup Cream`
- `Return`
- type `- 1 tsp Butter`
- `Return`
- type `- Spices: Chili powder, Garam masala, Turmeric, Salt`

**Result:** Paneer Butter Masala Shopping List

#### 5. Final Result

I have successfully searched for the ingredients for Paneer Butter Masala and created a new note on your Mac titled 'Paneer Butter Masala Shopping List' containing the following items:

- 200g Paneer
- 3 Tomatoes
- 1 Onion
- 1/2 cup Cashews
- 1 tbsp Ginger Garlic Paste
- 1/2 cup Cream
- 1 tsp Butter
- Spices: Chili powder, Garam masala, Turmeric, Salt

Source: https://www.indianhealthyrecipes.com/paneer-butter-masala-restaurant-style/

![Proof of Completion](assets/2.%20Proof%20of%20Completion.png)

![Task Completed](assets/2.%20Task%20Completed.png)

#### 6. Performance Summary

| Node | Skill | Status | Provider | Model | Duration | Tokens In | Tokens Out |
|---|---|---|---|---|---|---|---|
| `n:1` | planner | ✓ complete | gemini_lite_2 | — | 1,953 ms | 0 | 0 |
| `n:2` | researcher | ✓ complete | gemini_lite_2 | — | 23,551 ms | 0 | 0 |
| `n:3` | computer | ✓ complete | gemini_lite_2 | gemini-3.1-flash-lite | 5,579 ms | 395 | 198 |
| `n:5` | critic | ✓ complete | gemini_lite_2 | gemini-3.1-flash-lite | 6,918 ms | 395 | 198 |
| `n:4` | formatter | ✓ complete | gemini_lite_2 | — | 1,701 ms | 0 | 0 |
| **TOTAL** | | | | | **39,702 ms** | **790** | **396** |

---

### 3. Send a Message on Telegram (VISION)

### Session Log: s9-2026-06-24_02-44-20

#### 1. Original User Goal
> Open Telegram and search for my contact Puneeth and send a message "Lets catch this sunday at 6pm"

![Task Initiated](assets/3.%20Task%20Initiated.png)

#### 2. Planner DAG
![Planner DAG](assets/3.%20DAG.png)

#### 3. Computer Path Chosen
The Computer cascade chose the **VISION** interaction path.

#### 4. Computer Actions

**App:** Telegram  
**Goal:** Search for contact Puneeth and send the message: Lets catch this sunday at 6pm

Telegram is an Electron app — its AX tree exposes only the system menu bar with no app window content. The cascade escalated immediately to **Layer 5 (Vision / Set-of-Marks)**, which screenshots the screen each turn and sends it to a vision LLM to identify UI elements and decide the next action.

**Vision Session — 9 turns (Scan → Act → Verify):**

**Turn 1 — Open quick search**

Before: Telegram main window

![](assets/3.%20scan%20t00.png)

Action: `press cmd+k`

After: Search overlay opens

![](assets/3.%20verify%20t00.png)

---

**Turn 2 — First Down arrow**

Before: Search overlay open, no results yet

![](assets/3.%20scan%20t01.png)

Action: `press Down`

After: First result highlighted

![](assets/3.%20verify%20t01.png)

---

**Turn 3 — Second Down arrow**

Before: First search result highlighted

![](assets/3.%20scan%20t02.png)

Action: `press Down`

After: Puneeth highlighted in results, ready to open

![](assets/3.%20verify%20t02.png)

---

**Turn 4 — Open chat**

Before: Puneeth highlighted in search results

![](assets/3.%20scan%20t03.png)

Action: `press Return`

After: Puneeth's chat opened, compose field visible

![](assets/3.%20verify%20t03.png)

---

**Turn 5 — Type the message**

Before: Puneeth's chat open, compose field empty

![](assets/3.%20scan%20t04.png)

Action: `type "Lets catch this sunday at 6pm"`

After: Message text in compose field, ready to send

![](assets/3.%20verify%20t04.png)

---

**Turns 6–8 — Figuring out how to submit**

With the message typed, the vision LLM tried two different approaches to hit the send button before settling on keyboard input. This is the scan-act-verify loop working as designed: each failed verify tells the model the previous action had no visible effect, prompting it to try a different approach.

**Turn 6**

Before: Compose field with message, send icon visible bottom-right

![](assets/3.%20scan%20t05.png)

Action: `click bbox [954, 973, 981, 995]`

After: Send icon still visible — click did not register

![](assets/3.%20verify%20t05.png)

---

**Turn 7**

Before: Same state — send icon still present

![](assets/3.%20scan%20t06.png)

Action: `click bbox [947, 966, 984, 994]` *(adjusted coordinates, second attempt)*

After: State unchanged — click still not registering

![](assets/3.%20verify%20t06.png)

---

**Turn 8 — Switching to keyboard**

Before: Model switches strategy from click to keyboard

![](assets/3.%20scan%20t07.png)

Action: `press Down`

After: Focus confirmed on send — next step is Enter

![](assets/3.%20verify%20t07.png)

---

**Turn 9 — Send**

Action: `press Return`

✅ `goal_complete: The chat with Puneeth is open and the message 'Lets catch this sunday at 6pm' has been successfully sent, as evidenced by the message bubble in the chat history.`

![](assets/3.%20verify%20t08.png)

#### 5. Final Result

I have successfully opened Telegram, located your contact Puneeth, and sent the message: "Lets catch this sunday at 6pm."

![Task Completed](assets/3.%20Task%20Completed.png)

#### 6. Performance Summary

| Node | Skill | Status | Provider | Model | Duration | Tokens In | Tokens Out |
|---|---|---|---|---|---|---|---|
| `n:1` | planner | ✓ complete | gemini_lite_1 | — | 6,992 ms | 0 | 0 |
| `n:2` | computer | ✓ complete | gemini_lite_1, gemini_lite_2 | gemini-3.1-flash-lite | 69,153 ms | 32,726 | 425 |
| `n:4` | critic | ✓ complete | gemini_lite_2 | gemini-3.1-flash-lite | 1,576 ms | 1,719 | 50 |
| `n:3` | formatter | ✓ complete | gemini_lite_2 | — | 1,528 ms | 0 | 0 |
| **TOTAL** | | | | | **79,249 ms** | **34,445** | **475** |

---
