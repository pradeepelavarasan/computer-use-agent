"""Session 10: the Computer skill — 5-layer cascade.

Layers (cheapest → most expensive, stops at first success):

    Pre-flight  AppleScript activate   — brings app to foreground; always runs
    Layer 1     Extract                — passive AX read, zero LLM (needs cua-driver)
    Layer 2     AppleScript            — scriptable apps; one LLM call generates script
    Layer 3     Deterministic hotkeys  — planner-supplied keystrokes; zero LLM
    Layer 4     AX tree + LLM          — Scan-Act-Verify loop; cheap text LLM (needs cua-driver)
    Layer 5     Vision / Set-of-Marks  — screenshot + VLM loop (needs cua-driver)

Layers 2 and 3 work on any Mac without cua-driver.
Layers 1, 4, and 5 require `cua-driver serve` to be running.

NodeSpec.metadata keys:
    app_name    str   display name for AppleScript ("Calculator", "Notes")
    bundle_id   str   macOS bundle id for cua-driver launch ("com.apple.calculator")
    goal        str   the sub-task to accomplish
    hotkeys     list  [{keys: ["cmd","n"]}, ...]  triggers Layer 3
    force_path  str   "extract"|"applescript"|"hotkeys"|"a11y"|"vision"  (testing only)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from schemas import AgentResult, ComputerOutput, NodeSpec

from .applescript import (
    is_scriptable, keystroke_sequence, read_ax_value,
    run_as_action, run_as_query,
)
from .daemon import (
    CuaDriver, CuaServerUnavailable,
    activate_app, ensure_daemon, get_cua_driver, _get_pid_and_window,
)
from .driver import A11yDriver, DriverConfig, DriverResult, VisionDriver


_INTERACTION_VERBS = frozenset({
    "click", "open", "type", "write", "fill", "send", "create", "make",
    "delete", "close", "save", "drag", "select", "choose", "press",
    "launch", "start", "quit", "resize", "move", "copy", "paste",
})


def _goal_is_read_only(goal: str) -> bool:
    return not any(v in goal.lower() for v in _INTERACTION_VERBS)


# ── App-specific knowledge registry ──────────────────────────────────────────
# The planner only needs to name the app. The computer skill resolves technical
# details (bundle IDs, whether it's Electron, debug port) from this registry.
# This keeps implementation concerns out of the planner prompt.
_ELECTRON_APP_REGISTRY: dict[str, dict] = {
    "whatsapp": {
        "bundle_ids": ["net.whatsapp.WhatsApp", "com.whatsapp.Desktop"],
        "electron_port": 9222,
    },
    "slack": {
        "bundle_ids": ["com.tinyspeck.slackmacgap"],
        "electron_port": 9222,
    },
    "vscode":       {"bundle_ids": ["com.microsoft.VSCode"], "electron_port": 9222},
    "visual studio code": {"bundle_ids": ["com.microsoft.VSCode"], "electron_port": 9222},
    "code":         {"bundle_ids": ["com.microsoft.VSCode"], "electron_port": 9222},
    "cursor":       {"bundle_ids": ["com.todesktop.230313mzl4w4u92"], "electron_port": 9222},
    "discord":      {"bundle_ids": ["com.hnc.Discord"], "electron_port": 9222},
    "notion":       {"bundle_ids": ["notion.id"], "electron_port": 9222},
    "telegram":     {"bundle_ids": ["ru.keepcoder.Telegram"], "electron_port": 9222},
    "obsidian":     {"bundle_ids": ["md.obsidian"], "electron_port": 9222},
    "linear":       {"bundle_ids": ["com.linear"], "electron_port": 9222},
    "figma":        {"bundle_ids": ["com.figma.Desktop"], "electron_port": 9222},
}


def _enrich_app_metadata(
    app_name: str,
    bundle_id: str,
    electron_port: Optional[int],
) -> tuple[str, Optional[int]]:
    """Resolve bundle_id and electron_port from the app registry.

    The planner only needs to supply app_name. The computer skill enriches the
    metadata with the correct technical details, keeping that knowledge here
    rather than polluting the planner prompt.
    """
    name_lower = app_name.lower().strip()
    for key, config in _ELECTRON_APP_REGISTRY.items():
        if key in name_lower or name_lower in key:
            if not electron_port:
                electron_port = config["electron_port"]
            # If no bundle_id provided, or the provided one isn't in our known list, use ours
            known_ids = config["bundle_ids"]
            if not bundle_id or bundle_id not in known_ids:
                bundle_id = known_ids[0]
                print(f"[computer/skill] enriched {app_name!r}: "
                      f"bundle_id={bundle_id!r}, electron_port={electron_port}")
            break
    return bundle_id, electron_port


def _wrap_browser_result(browser_result) -> DriverResult:
    """Convert a browser.driver.DriverResult into a computer DriverResult.

    The browser driver returns {success, note, steps}; the computer DriverResult
    also carries {turns, actions, content}. We extract what we can from steps.
    """
    steps = getattr(browser_result, "steps", [])
    content = ""
    if browser_result.success:
        # Try to extract the 'done' note from the last step as the content
        for step in reversed(steps):
            note = getattr(step, "outcome", "") or ""
            if "done" in note.lower() or "success" in note.lower():
                content = note
                break
        if not content:
            content = browser_result.note or "completed via CDP"
    return DriverResult(
        success=browser_result.success,
        note=browser_result.note,
        steps=steps,
        turns=len(steps),
        actions=[],
        content=content,
    )


class ComputerSkill:
    NAME = "computer"

    def __init__(
        self,
        *,
        gateway_url: str = "http://localhost:8110",
        agent_tag: str = "computer",
        a11y_provider_pin: Optional[str] = "gemini",
        vision_provider_pin: Optional[str] = None,
        artifacts_root: Optional[str] = None,
        max_steps_a11y: int = 12,
        max_steps_vision: int = 12,
        session: Optional[str] = None,
    ):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_a11y = max_steps_a11y
        self.max_steps_vision = max_steps_vision
        self.session = session

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self, node: NodeSpec) -> AgentResult:
        t0 = time.time()
        goal = node.metadata.get("goal") or (node.inputs[0] if node.inputs else "")
        app_name = node.metadata.get("app_name", "")
        force_path = node.metadata.get("force_path")

        if not goal:
            return self._pack_error(app_name, goal, "interaction_failed",
                                    "no goal given (metadata.goal or inputs[0])")

        from browser.client import V9Client
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)

        artifacts_dir: Optional[str] = None
        if self.artifacts_root:
            artifacts_dir = str(self.artifacts_root / f"computer_{int(t0)}")

        # Pre-flight: bring app to foreground via AppleScript (no cua-driver needed)
        if app_name:
            activate_app(app_name)

        try:
            return await self._cascade(
                client, app_name, goal, node, artifacts_dir, force_path, t0,
            )
        except Exception as exc:
            return self._pack_error(app_name, goal, "interaction_failed", str(exc),
                                    elapsed=time.time() - t0)

    # ── cascade ───────────────────────────────────────────────────────────────

    async def _cascade(
        self, client, app_name: str, goal: str,
        node: NodeSpec, artifacts_dir: Optional[str],
        force_path: Optional[str], t0: float,
    ) -> AgentResult:
        bundle_id = node.metadata.get("bundle_id", "")
        electron_port: Optional[int] = node.metadata.get("electron_debugging_port")

        # Enrich metadata from the skill's app registry — the planner only needs
        # to name the app, the skill resolves bundle_ids and electron ports.
        bundle_id, electron_port = _enrich_app_metadata(app_name, bundle_id, electron_port)

        # Probe cua-driver daemon ONCE — memoises the result so Layers 1, 4, 5
        # don't each wait 30 s independently when the daemon is absent.
        daemon_err: Optional[str] = None
        cua: Optional[CuaDriver] = None
        pid: Optional[int] = None
        window_id: Optional[int] = None

        try:
            await ensure_daemon()
            cua = get_cua_driver()
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            pid, window_id = await loop.run_in_executor(
                None, lambda: _get_pid_and_window(
                    cua, app_name, bundle_id,
                    electron_debugging_port=electron_port,
                )
            )
        except CuaServerUnavailable as exc:
            daemon_err = str(exc)
            print(f"[computer/skill] daemon unavailable — Layers 1, 4, 5 skipped: {daemon_err}")
        except Exception as exc:
            daemon_err = str(exc)
            print(f"[computer/skill] app launch failed — Layers 1, 4, 5 skipped: {daemon_err}")

        # prior_context accumulates one bullet per escalating layer so that
        # Vision's first SCAN prompt explains what has already been tried and
        # what state the UI is likely in — enabling a clean handoff.
        prior_context: list[str] = []

        # ── Layer 1: Extract ─────────────────────────────────────────────────
        # Passive AX tree read — zero LLM, zero clicks. Succeeds only for
        # read-only goals ("what is on screen?").
        if force_path not in ("applescript", "hotkeys", "a11y", "vision") and not daemon_err:
            result = await self._layer1_extract(cua, pid, window_id, app_name, goal, artifacts_dir)
            if result is not None:
                return result

        # ── Layer 2: AppleScript (with Scan-Act-Verify) ──────────────────────
        # For apps with a scripting dictionary. SAV: scan state → run script → verify.
        if force_path not in ("hotkeys", "a11y", "vision"):
            result = await self._layer2_applescript(
                cua, pid, window_id, client, app_name, goal, artifacts_dir
            )
            if result is not None:
                return result
            prior_context.append(
                "Layer 2 (AppleScript): ran but produced no verifiable output — escalated"
            )

        # ── Layer 3: Hotkeys (with Scan-Act-Verify) ─────────────────────────
        # The cascade ALWAYS attempts this layer — it does not depend on the
        # planner to provide hotkeys. Two sources for the keystroke sequence:
        #   A) Planner metadata — trusted, zero LLM cost (preferred)
        #   B) Skill-generated — one LLM call; returns [] if unsure (fallback)
        # Either way, SAV runs: scan → fire keys → verify state changed.
        # If verify shows no change → return None → Layer 4 handles it.
        if force_path not in ("a11y", "vision"):
            hotkeys = node.metadata.get("hotkeys") or []
            if not hotkeys:
                # Planner didn't supply hotkeys — generate them internally.
                hotkeys = await self._generate_hotkeys(client, app_name, goal)
            if hotkeys:
                result = await self._layer3_hotkeys(
                    cua, pid, window_id, app_name, goal, hotkeys, artifacts_dir
                )
                if result is not None:
                    return result
                # Describe what the hotkeys did and give Vision a clear next action
                typed_texts = [s["text"] for s in hotkeys if "text" in s]
                hotkey_desc = ", ".join(
                    (f'typed "{s.get("text","")}"' if "text" in s else "+".join(s["keys"]))
                    for s in hotkeys
                )
                if typed_texts:
                    # Layer 3 typed a search term — Vision must click the result
                    typed_str = ", ".join(f'"{t}"' for t in typed_texts)
                    prior_context.append(
                        f"Layer 3 typed {typed_str} into a field but could not verify the result. "
                        f"Look at the screenshot: if a search result list or contact is now visible, "
                        f"your FIRST action must be to CLICK on the correct result — do NOT type again."
                    )
                else:
                    prior_context.append(
                        f"Layer 3 (hotkeys [{hotkey_desc}]): fired but element count unchanged — "
                        "the UI may have partially changed. Check the screenshot and continue."
                    )
            else:
                prior_context.append(
                    "Layer 3 (hotkeys): skipped — no safe keystroke sequence for this goal"
                )

        # ── Layer 4: AX tree + LLM ───────────────────────────────────────────
        # Scan-Act-Verify loop driven by a cheap text LLM reading the AX tree.
        # Works for any app that exposes an accessibility tree. Needs cua-driver.
        if force_path != "vision" and not daemon_err:
            a11y_dir = str(Path(artifacts_dir) / "a11y") if artifacts_dir else None
            cfg = DriverConfig(
                goal=goal, app_name=app_name, max_steps=self.max_steps_a11y, max_failures=3,
                artifacts_dir=a11y_dir, provider=self.a11y_provider_pin,
                agent_tag=self.agent_tag, prior_context=list(prior_context),
                electron_port=electron_port,
            )
            a11y_result = await A11yDriver(cua, pid, window_id, client, cfg).run()
            if a11y_result.success:
                return self._pack_driver("a11y", app_name, goal, a11y_result,
                                         elapsed=time.time() - t0)
            prior_context.append(
                f"Layer 4 (A11y): {a11y_result.note or 'escalated'}"
            )

        # ── Layer 4b: Electron CDP via Playwright ─────────────────────────────
        # When the AX tree is empty AND an electron_debugging_port was specified,
        # connect Playwright to the app's CDP endpoint and run the full browser
        # driver pipeline (Set-of-Marks + Vision LLM). This reuses code/browser/
        # directly — no new infrastructure needed. Escalates to Layer 5 if CDP
        # connection fails or selectors don't complete the goal.
        if electron_port and not daemon_err and force_path != "vision":
            print(f"[computer/Layer4b] Electron CDP mode (port={electron_port})")
            try:
                from playwright.async_api import async_playwright
                # SetOfMarksDriver: screenshot + numbered marks + vision LLM picks element
                # Playwright clicks go via CDP Input.dispatchMouseEvent → reaches Electron DOM
                from browser.driver import SetOfMarksDriver as SoMDriver, DriverConfig as BrowserCfg
                async with async_playwright() as pw:
                    browser_conn = await pw.chromium.connect_over_cdp(
                        f"http://localhost:{electron_port}"
                    )
                    contexts = browser_conn.contexts
                    print(f"[computer/Layer4b] connected — contexts={len(contexts)}, "
                          f"pages={len(contexts[0].pages) if contexts else 0}")
                    if contexts and contexts[0].pages:
                        page = contexts[0].pages[0]
                        browser_cfg = BrowserCfg(
                            goal=goal,
                            max_steps=self.max_steps_a11y,
                            artifacts_dir=a11y_dir,
                            provider=self.vision_provider_pin,
                        )
                        cdp_result = await SoMDriver(page, client, browser_cfg).run()
                        if cdp_result.success:
                            return self._pack_driver("a11y", app_name, goal,
                                                     _wrap_browser_result(cdp_result),
                                                     elapsed=time.time() - t0)
                        prior_context.append(
                            f"Layer 4b (CDP/Playwright): {cdp_result.note or 'escalated'}"
                        )
                    else:
                        prior_context.append("Layer 4b (CDP/Playwright): no pages in context")
            except Exception as exc:
                print(f"[computer/Layer4b] CDP failed: {exc} — escalating to Vision")
                prior_context.append(f"Layer 4b (CDP/Playwright): failed ({exc})")

        # ── Layer 5: Vision / Set-of-Marks ───────────────────────────────────
        # Screenshot + vision LLM loop. Fallback for canvas apps, games, or
        # anything where the AX tree is empty. Needs cua-driver.
        if daemon_err:
            return self._pack_error(
                app_name, goal, "server_unavailable",
                f"Layers 1/4/5 require cua-driver (not available: {daemon_err}). "
                "Layers 2 (AppleScript) and 3 (hotkeys) were attempted.",
                elapsed=time.time() - t0,
            )

        vis_dir = str(Path(artifacts_dir) / "vision") if artifacts_dir else None
        cfg = DriverConfig(
            goal=goal, app_name=app_name, electron_port=electron_port,
            max_steps=self.max_steps_vision, max_failures=3,
            artifacts_dir=vis_dir, provider=self.vision_provider_pin,
            agent_tag=self.agent_tag, prior_context=prior_context,
        )
        vis_result = await VisionDriver(cua, pid, window_id, client, cfg).run()
        if vis_result.success:
            return self._pack_driver("vision", app_name, goal, vis_result,
                                     elapsed=time.time() - t0)

        # Pass the Vision driver's steps so the log shows what the LLM tried each turn.
        # Previously actions:[] on failure hid all 12 decisions from the node JSON.
        vis_actions = [s.__dict__ for s in vis_result.steps] if vis_result.steps else []
        return self._pack_error(app_name, goal, "interaction_failed",
                                f"all 5 layers exhausted; last: {vis_result.note}",
                                elapsed=time.time() - t0,
                                path="vision", actions=vis_actions)

    # ── Layer 1 ───────────────────────────────────────────────────────────────

    async def _layer1_extract(
        self, cua: CuaDriver, pid: int, window_id: int,
        app_name: str, goal: str, artifacts_dir: Optional[str],
    ) -> Optional[AgentResult]:
        """Passive AX tree read — zero LLM, zero clicks."""
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            ax_resp = await loop.run_in_executor(
                None, lambda: cua.get_window_state(pid, window_id, mode="ax")
            )
        except Exception:
            return None

        from .driver import _parse_ax_tree
        count, tree_md, _ = _parse_ax_tree(ax_resp)

        if count == 0:
            return self._pack_error(
                app_name, goal, "precondition_failed",
                "Empty AX tree (Layer 1). "
                "Check: (1) 'cua-driver permissions grant' accepted, "
                "(2) app activated (AppleScript), "
                "(3) QT_ACCESSIBILITY=1 if Qt, "
                "(4) electron_debugging_port if Electron.",
            )

        if not _goal_is_read_only(goal):
            return None   # interactive goal — continue to Layer 2

        if artifacts_dir:
            p = Path(artifacts_dir) / "extract" / "ax_tree.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(tree_md)

        return self._pack("extract", app_name, goal, turns=0, content=tree_md)

    # ── Layer 2 ───────────────────────────────────────────────────────────────

    async def _layer2_applescript(
        self,
        cua: Optional[CuaDriver], pid: Optional[int], window_id: Optional[int],
        client, app_name: str, goal: str, artifacts_dir: Optional[str],
    ) -> Optional[AgentResult]:
        """AppleScript with Scan → Act → Verify.

        SCAN:   Capture current window state before the script runs.
        ACT:    LLM generates an AppleScript; osascript executes it.
        VERIFY: Re-read window state; confirm it changed OR read script output.
                If state unchanged and no output → return None → Layer 3/4.

        Only fires for apps with a scripting dictionary.
        """
        if not app_name or not is_scriptable(app_name):
            return None

        import asyncio as _asyncio
        from .driver import _parse_ax_tree

        # ── SCAN ─────────────────────────────────────────────────────────────
        initial_count: Optional[int] = None
        initial_tree: Optional[str] = None

        if cua and pid and window_id:
            try:
                loop = _asyncio.get_event_loop()
                ax_before = await loop.run_in_executor(
                    None, lambda: cua.get_window_state(pid, window_id, mode="ax")
                )
                initial_count, initial_tree, _ = _parse_ax_tree(ax_before)
                print(f"[computer/Layer2] SCAN complete: element_count={initial_count}")
            except Exception as exc:
                print(f"[computer/Layer2] SCAN failed: {exc}")

        # ── ACT ──────────────────────────────────────────────────────────────
        prompt = (
            f"Write a minimal AppleScript body for the following task in {app_name}. "
            f"Task: {goal}\n\n"
            f"Output ONLY the statements that go inside "
            f"'tell application \"{app_name}\" ... end tell'. "
            "No explanations. No markdown fences. Just the AppleScript.\n\n"
            "IMPORTANT: The last statement MUST be a `return` statement that returns "
            "a short confirmation string describing what was done "
            "(e.g. 'return \"Created note titled Hello\"'). "
            "The return value is the only way the system can verify success."
        )
        try:
            reply = await client.chat(
                prompt, provider=self.a11y_provider_pin, max_tokens=300,
            )
            script_body = (reply.get("text", "") if isinstance(reply, dict) else reply.text).strip()
        except Exception:
            return None

        if not script_body:
            return None

        if artifacts_dir:
            p = Path(artifacts_dir) / "applescript" / "script.applescript"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"-- goal: {goal}\n-- app: {app_name}\n{script_body}")

        ok, script_output = run_as_action(app_name, script_body)

        if not ok:
            # Script errored — if app is still alive, fall through to Layer 3/4
            _, alive = run_as_query(app_name, "return name")
            if alive:
                return None
            return self._pack_error(app_name, goal, "interaction_failed",
                                    f"AppleScript failed and app unresponsive: {script_output}")

        # Idempotency guard: verify state changed AND avoid counting a retry as
        # new work. If the scan and verify show identical state AND the script
        # produced no output, the script was a no-op (maybe ran twice on retry).
        # In that case we still return success since the goal was already achieved.

        # ── VERIFY ───────────────────────────────────────────────────────────
        content: Optional[str] = script_output.strip() if script_output else None
        state_changed = False

        if cua and pid and window_id:
            try:
                loop = _asyncio.get_event_loop()
                ax_after = await loop.run_in_executor(
                    None, lambda: cua.get_window_state(pid, window_id, mode="ax")
                )
                after_count, after_tree, _ = _parse_ax_tree(ax_after)
                print(f"[computer/Layer2] VERIFY: element_count={after_count}")

                state_changed = (
                    after_count != initial_count
                    or after_tree[:400] != (initial_tree or "")[:400]
                )

            except Exception as exc:
                print(f"[computer/Layer2] VERIFY failed: {exc}")

        # Strict physical verification for interactive goals
        if not _goal_is_read_only(goal) and not state_changed:
            print("[computer/Layer2] VERIFY: state physically unchanged for interactive goal — "
                  "escalating to Layer 3/4")
            return None

        # Only claim success when we have content that actually confirms the
        # goal was achieved — either the script returned a value (script_output)
        # or VERIFY confirmed the UI changed in a meaningful way.
        if not content:
            print("[computer/Layer2] no verifiable result from AppleScript "
                  "— escalating to Layer 3/4")
            _, alive = run_as_query(app_name, "return name")
            if alive:
                return None   # app OK; Layer 3/4 will verify properly
            return self._pack_error(app_name, goal, "interaction_failed",
                                    "AppleScript produced no output and app appears unresponsive")

        return self._pack("applescript", app_name, goal, turns=1, content=content)

    # ── Layer 3 ───────────────────────────────────────────────────────────────

    # Apps that hold computation state and need an Escape/Clear before new input.
    _STATEFUL_APPS = {"calculator", "grapher"}

    async def _generate_hotkeys(
        self, client, app_name: str, goal: str
    ) -> list[dict]:
        """Ask the LLM to generate a keystroke sequence for this goal (one call).

        This keeps Layer 3 self-contained — the cascade doesn't depend on the
        planner knowing which hotkeys to use. Returns [] if the LLM is unsure,
        which causes Layer 3 to be skipped and the cascade to continue to Layer 4.
        """
        import json as _json

        prompt = (
            f"You are automating the macOS app \"{app_name}\" using System Events keystrokes.\n"
            f"Goal: {goal}\n\n"
            "Output ONLY a JSON array of keystroke steps. Each step:\n"
            '  {"keys": ["cmd", "n"]}   → keyboard shortcut\n'
            '  {"keys": ["Return"]}     → press Return\n'
            '  {"keys": ["Escape"]}     → press Escape\n'
            '  {"text": "hello"}        → type a string\n\n'
            "Key names: Return, Tab, Escape, Space, Delete, Up, Down, Left, Right, F1-F12.\n"
            "Modifiers: cmd, shift, alt, ctrl.\n\n"
            "Rules:\n"
            "- Use standard macOS shortcuts for this specific app.\n"
            "- KEY TEST before returning any non-empty array: ask yourself — "
            "can I write down the COMPLETE keystroke sequence right now, "
            "without needing to see what appears on screen between any two steps? "
            "If NO (e.g. you need to see search results, a list highlight, a dialog "
            "response, or any intermediate UI state before you know the next key), "
            "return []. Blind keystroke sequences are only safe when the full "
            "sequence is known in advance and requires zero visual confirmation.\n"
            "- Good candidates: arithmetic shortcuts, fixed menu shortcuts (cmd+n, cmd+s), "
            "fixed document shortcuts — sequences where every key is predetermined.\n"
            "- Bad candidates: any goal requiring search → result selection → further action, "
            "navigation that depends on what is currently highlighted or focused, "
            "or multi-step flows where step N determines step N+1.\n"
            "- If you are NOT 100% certain the full sequence works without visual "
            "confirmation, return [] so the smarter visual layer handles it.\n"
            "- Do NOT include app-launch steps — the app is already open.\n\n"
            "Return ONLY the JSON array. No explanation. No markdown fences."
        )

        try:
            reply = await client.chat(
                prompt, provider=self.a11y_provider_pin, max_tokens=300,
            )
            raw = (reply.get("text", "") if isinstance(reply, dict) else reply.text).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            start, end = raw.find("["), raw.rfind("]")
            if start == -1 or end == -1:
                return []
            hotkeys = _json.loads(raw[start:end + 1])
            if hotkeys:
                print(f"[computer/Layer3] generated {len(hotkeys)} keystrokes for goal")
            return hotkeys if isinstance(hotkeys, list) else []
        except Exception as exc:
            print(f"[computer/Layer3] hotkey generation failed: {exc}")
            return []

    async def _layer3_hotkeys(
        self,
        cua: Optional[CuaDriver], pid: Optional[int], window_id: Optional[int],
        app_name: str, goal: str,
        hotkeys: list[dict], artifacts_dir: Optional[str],
    ) -> Optional[AgentResult]:
        """Planner-supplied keystrokes with Scan → Act → Verify.

        SCAN:   Capture the window state before acting (element count + tree snapshot).
        ACT:    Fire the planner-supplied keystroke sequence via System Events.
        VERIFY: Re-read the window state and check whether it changed.
                If no change is detected AND no readable result value is found,
                return None so the cascade escalates to Layer 4 (AX tree + LLM)
                which can diagnose the failure with proper Scan-Act-Verify per turn.

        The verify step has two complementary probes:
          1. Read a concrete value via System Events AX (instant, no daemon needed).
             Example: Calculator's display value, a text field's current text.
          2. Re-scan via cua-driver and diff element count / tree text against the
             pre-action snapshot (works for any app when cua-driver is running).
        """
        import asyncio as _asyncio
        from .driver import _parse_ax_tree

        # ── SCAN ─────────────────────────────────────────────────────────────
        initial_count: Optional[int] = None
        initial_tree: Optional[str] = None

        if cua and pid and window_id:
            try:
                loop = _asyncio.get_event_loop()
                ax_before = await loop.run_in_executor(
                    None, lambda: cua.get_window_state(pid, window_id, mode="ax")
                )
                initial_count, initial_tree, _ = _parse_ax_tree(ax_before)
                print(f"[computer/Layer3] SCAN complete: element_count={initial_count}")
            except Exception as exc:
                print(f"[computer/Layer3] SCAN via cua-driver failed: {exc}")

        if initial_tree is None:
            # Fallback scan: read current display via System Events AX
            for ax_path in ("value of static text 1 of window 1",
                            "value of text field 1 of window 1"):
                ok_v, val = read_ax_value(app_name, ax_path)
                if ok_v:
                    initial_tree = val
                    break

        # ── ACT ──────────────────────────────────────────────────────────────
        if app_name.lower() in self._STATEFUL_APPS:
            hotkeys = [{"keys": ["Escape"]}] + list(hotkeys)

        ok, output = keystroke_sequence(hotkeys, app_name=app_name)

        if not ok:
            return self._pack_error(app_name, goal, "interaction_failed",
                                    f"System Events keystroke failed: {output}")

        # ── VERIFY ───────────────────────────────────────────────────────────
        content: Optional[str] = None

        # Probe 1: read a concrete value (Calculator display, text fields, etc.)
        for ax_path in (
            "value of static text 1 of window 1",
            "value of text field 1 of window 1",
            "value of static text 1 of group 1 of window 1",
        ):
            ok_v, val = read_ax_value(app_name, ax_path)
            if ok_v and val and val != initial_tree:
                content = val   # value changed → confirmed success
                break

        # Probe 2: cua-driver re-scan and diff
        state_changed = False   # initialised here; updated inside try block below
        if cua and pid and window_id:
            try:
                loop = _asyncio.get_event_loop()
                ax_after = await loop.run_in_executor(
                    None, lambda: cua.get_window_state(pid, window_id, mode="ax")
                )
                after_count, after_tree, _ = _parse_ax_tree(ax_after)
                print(f"[computer/Layer3] VERIFY: element_count={after_count}")

                state_changed = (
                    after_count != initial_count
                    or after_tree[:400] != (initial_tree or "")[:400]
                )

                if state_changed and not content:
                    # Use typed text as the content when available — far more
                    # meaningful than an element-count delta, and lets the critic pass.
                    typed_texts = [s["text"] for s in hotkeys if "text" in s]
                    if typed_texts:
                        content = "Typed via keystrokes: " + " ".join(typed_texts)
                    else:
                        keys_summary = ", ".join(
                            "+".join(s["keys"]) for s in hotkeys if "keys" in s
                        )
                        content = (
                            f"Executed keystrokes ({keys_summary}); "
                            f"UI state changed (elements: {initial_count} → {after_count})"
                        )

                if not state_changed and not content:
                    print("[computer/Layer3] VERIFY: state unchanged — "
                          "hotkeys had no visible effect; escalating to Layer 4")
                    return None   # fall through; Layer 4 will diagnose properly

            except Exception as exc:
                print(f"[computer/Layer3] VERIFY via cua-driver failed: {exc}")

        # Strict physical verification for interactive goals
        physically_verified = state_changed or bool(content)
        if not _goal_is_read_only(goal) and not physically_verified:
            print("[computer/Layer3] VERIFY: state physically unchanged for interactive goal — "
                  "escalating to Layer 4")
            return None

        if not content:
            # Neither probe returned a verifiable result.
            # Without confirmation, do not report success — fall through to Layer 4.
            print("[computer/Layer3] VERIFY: no verifiable result — escalating to Layer 4")
            return None

        actions_taken = [{"step": i, **step} for i, step in enumerate(hotkeys)]

        if artifacts_dir:
            p = Path(artifacts_dir) / "hotkeys" / "actions.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(str(a) for a in actions_taken))

        return self._pack("hotkeys", app_name, goal,
                          turns=len(hotkeys), content=content, actions=actions_taken)

    # ── packers ───────────────────────────────────────────────────────────────

    def _pack(
        self, path: str, app_name: str, goal: str, *,
        turns: int, content: Optional[str] = None,
        actions: Optional[list] = None, elapsed: float = 0.0,
    ) -> AgentResult:
        out = ComputerOutput(
            app=app_name, goal=goal, path=path, turns=turns,
            content=content, actions=actions or [],
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_driver(
        self, path: str, app_name: str, goal: str,
        drv_result: DriverResult, *, elapsed: float,
    ) -> AgentResult:
        out = ComputerOutput(
            app=app_name, goal=goal, path=path,
            turns=drv_result.turns,
            content=drv_result.content or None,
            actions=drv_result.actions or [],
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_error(
        self, app_name: str, goal: str, code: str, msg: str, *,
        elapsed: float = 0.0, path: str = "extract",
        turns: int = 0, actions: Optional[list] = None,
    ) -> AgentResult:
        out = ComputerOutput(
            app=app_name, goal=goal, path=path, turns=turns,
            content=None, actions=actions or [], error_code=code,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code=code,  # type: ignore[arg-type]
            elapsed_s=elapsed,
        )
