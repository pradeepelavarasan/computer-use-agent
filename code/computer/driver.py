"""A11yDriver and VisionDriver for the computer skill (Layers 2b and 3).

Both drivers implement the Scan-Act-Verify loop:

    SCAN  → cua-driver get_window_state  (builds fresh element-index cache)
    ACT   → cua-driver click / type_text / press_key
    VERIFY → re-scan; indices from previous turn are stale

Two invariants (violating either causes silent failures):
  1. Call get_window_state once per turn before any element-indexed action.
  2. Every new get_window_state replaces the previous index map.

Interaction goes through the cua-driver CLI (Unix socket), not the
cua-computer Python SDK.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .daemon import CuaDriver
from .highlight import DesktopElement, annotate, make_legend, to_data_url


# ── shared result types ───────────────────────────────────────────────────────

@dataclass
class StepRecord:
    turn: int
    actions: list[dict]
    outcome: str


@dataclass
class DriverResult:
    success: bool
    note: str
    steps: list[StepRecord] = field(default_factory=list)
    turns: int = 0
    actions: list[dict] = field(default_factory=list)
    content: str = ""
    error_code: Optional[str] = None


@dataclass
class DriverConfig:
    goal: str
    app_name: str = ""          # used by VisionDriver to activate the app before keystrokes
    electron_port: Optional[int] = None  # set for Electron apps; skips cua screenshot attempt
    max_steps: int = 12
    max_failures: int = 3
    artifacts_dir: Optional[str] = None
    provider: Optional[str] = None
    agent_tag: str = "computer"
    prior_context: list = field(default_factory=list)
    # One bullet per earlier layer that ran and escalated, e.g.:
    #   "Layer 2 (AppleScript): ran but produced no output — escalated"
    #   "Layer 3 (hotkeys [cmd+k, typed '<query>']): element count unchanged — UI may have changed"
    # VisionDriver injects this into the first SCAN prompt so the LLM
    # understands what has already been attempted and can continue from
    # the current state rather than starting from scratch.


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ax_tree(ax_response: dict) -> tuple[int, str, list[DesktopElement]]:
    """Parse get_window_state response into (element_count, markdown, elements).

    cua-driver returns:
      tree_markdown: str    (AX tree with [element_index N] tags)
      element_count: int    (number of actionable elements)
      screenshot: str       (base64 PNG, if capture_mode=som)
      elements: list        (structured element list, if available)
    """
    tree_md = ax_response.get("tree_markdown") or ax_response.get("tree", "") or ""
    elem_count = ax_response.get("element_count", 0)

    # If element_count is 0 but tree_markdown has content, try to count [element_index] tags
    if elem_count == 0 and tree_md:
        elem_count = tree_md.count("[element_index")

    # Build DesktopElement list from structured data if present
    elements: list[DesktopElement] = []
    raw_els = ax_response.get("elements") or []
    for i, el in enumerate(raw_els):
        elements.append(DesktopElement(
            id=el.get("element_index", el.get("id", i)),
            tag=el.get("role", "button").lower(),
            label=el.get("label") or el.get("title") or el.get("value") or "",
            x=float(el.get("x", 0) or 0),
            y=float(el.get("y", 0) or 0),
            w=float(el.get("width", el.get("w", 50)) or 50),
            h=float(el.get("height", el.get("h", 20)) or 20),
        ))

    return elem_count, tree_md, elements


def _save_png(png_bytes: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)


def _run_sync(cua: CuaDriver, method: str, *args, **kwargs) -> Any:
    """Run a synchronous CuaDriver method (blocking subprocess call)."""
    return getattr(cua, method)(*args, **kwargs)


async def _async_call(cua: CuaDriver, method: str, *args, **kwargs) -> Any:
    """Run a CuaDriver method in a thread executor so the event loop stays free."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: getattr(cua, method)(*args, **kwargs))


# ── Layer 2b: A11y tree + cheap LLM ──────────────────────────────────────────

class A11yDriver:
    LAYER_NAME = "a11y"

    def __init__(self, cua: CuaDriver, pid: int, window_id: int, client, cfg: DriverConfig):
        self.cua = cua
        self.pid = pid
        self.window_id = window_id
        self.client = client
        self.cfg = cfg
        self.steps: list[StepRecord] = []

    async def run(self) -> DriverResult:
        failures = 0
        for turn in range(self.cfg.max_steps):

            # ── SCAN ─────────────────────────────────────────────────────────
            try:
                ax_resp = await _async_call(
                    self.cua, "get_window_state",
                    self.pid, self.window_id, "ax",
                )
            except Exception as exc:
                return DriverResult(success=False, note=f"get_window_state failed: {exc}")

            elem_count, tree_md, elements = _parse_ax_tree(ax_resp)

            if elem_count == 0:
                return DriverResult(
                    success=False,
                    note=(
                        "Empty AX tree (element_count=0). "
                        "Check: (1) 'cua-driver permissions grant' accepted, "
                        "(2) app activated via AppleScript, "
                        "(3) QT_ACCESSIBILITY=1 if Qt app, "
                        "(4) electron_debugging_port if Electron app."
                    ),
                )

            # If the tree contains only the system menu bar and no AXWindow, the
            # app's window content is not exposed (typical for Electron apps).
            # Escalate immediately rather than burning all turns on menu bar items.
            # We do NOT escalate when AXWindow is also present — that is a native
            # app whose menu bar items are legitimately reachable via A11y.
            if "AXMenuBar" in tree_md and "AXWindow" not in tree_md:
                return DriverResult(
                    success=False,
                    note=(
                        "AX tree contains only the system menu bar (no AXWindow). "
                        "App window content is not exposed — likely an Electron or "
                        "GPU-rendered app. Escalating to Vision layer."
                    ),
                )

            # Save per-turn AX snapshot
            if self.cfg.artifacts_dir:
                snap = Path(self.cfg.artifacts_dir) / f"turn_{turn:02d}_ax.txt"
                snap.parent.mkdir(parents=True, exist_ok=True)
                snap.write_text(tree_md)

            # ── LLM decision ─────────────────────────────────────────────────
            prompt = (
                f"You are controlling a desktop application. "
                f"Goal: {self.cfg.goal}\n\n"
                f"Current window state:\n{tree_md}\n\n"
                "Reply with ONE JSON object (no markdown fences):\n"
                '{"type":"act","element_index":<N>,"tool":"click"|"type_text"|"press_key",'
                '"value":"<text or key name if needed>"}\n'
                'OR {"type":"done","result":"<what was accomplished>"}\n'
                'OR {"type":"escalate","reason":"<why AX tree cannot satisfy the goal>"}'
            )

            try:
                reply = await self.client.chat(
                    prompt, provider=self.cfg.provider, max_tokens=256,
                )
                raw = (reply.get("text", "") if isinstance(reply, dict) else reply.text).strip()
                raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                decision = json.loads(raw)
            except Exception as exc:
                failures += 1
                self.steps.append(StepRecord(turn=turn, actions=[], outcome=f"parse_error: {exc}"))
                if failures >= self.cfg.max_failures:
                    return DriverResult(success=False, note=f"LLM parse failed {failures}×: {exc}")
                continue

            dtype = decision.get("type")

            if dtype == "done":
                self.steps.append(StepRecord(turn=turn, actions=[], outcome="done"))
                return DriverResult(
                    success=True, note="done", steps=self.steps,
                    turns=turn + 1, actions=[s.__dict__ for s in self.steps],
                    content=decision.get("result", ""),
                )

            if dtype == "escalate":
                reason = decision.get("reason", "escalated by LLM")
                self.steps.append(StepRecord(turn=turn, actions=[], outcome=f"escalate: {reason}"))
                return DriverResult(success=False, note=reason, steps=self.steps)

            # ── ACT ──────────────────────────────────────────────────────────
            tool = decision.get("tool", "click")
            idx = decision.get("element_index")
            value = decision.get("value", "")
            action_record = {"turn": turn, "tool": tool, "element_index": idx, "value": value}

            try:
                if tool == "click":
                    await _async_call(self.cua, "click", self.pid, self.window_id, idx)
                elif tool == "type_text":
                    await _async_call(self.cua, "type_text", self.pid, str(value), idx, self.window_id)
                elif tool == "press_key":
                    await _async_call(self.cua, "press_key", self.pid, str(value), None, self.window_id)
                else:
                    return DriverResult(success=False, note=f"unknown tool: {tool}", steps=self.steps)

                await asyncio.sleep(0.3)

            except Exception as exc:
                failures += 1
                self.steps.append(StepRecord(turn=turn, actions=[action_record], outcome=f"act_error: {exc}"))
                if failures >= self.cfg.max_failures:
                    return DriverResult(success=False, note=f"action failed {failures}×: {exc}", steps=self.steps)
                continue

            # ── VERIFY ───────────────────────────────────────────────────────
            try:
                new_resp = await _async_call(self.cua, "get_window_state", self.pid, self.window_id, "ax")
                new_count, _, _ = _parse_ax_tree(new_resp)
                outcome = f"acted; new element_count={new_count}"
            except Exception as exc:
                outcome = f"verify_error: {exc}"

            self.steps.append(StepRecord(turn=turn, actions=[action_record], outcome=outcome))

        return DriverResult(
            success=False,
            note=f"max_steps ({self.cfg.max_steps}) reached without completing goal",
            steps=self.steps, turns=self.cfg.max_steps,
            actions=[s.__dict__ for s in self.steps],
        )


# ── Layer 5: Vision / Set-of-Marks ───────────────────────────────────────────

class VisionDriver:
    """Per-turn Scan → Act → Verify loop using a vision LLM.

    Each turn has two LLM calls:
      1. SCAN call   — screenshot sent with "what should I do?" prompt
      2. VERIFY call — fresh screenshot sent with "did that work?" prompt

    The VERIFY call is what makes the loop safe: if the action landed on the
    wrong element (e.g., chat search instead of contact search) the LLM sees
    the new state and reports `confirmed: false`, incrementing the failure
    counter and preventing the driver from blindly continuing.
    """

    LAYER_NAME = "vision"

    def __init__(self, cua: CuaDriver, pid: int, window_id: int, client, cfg: DriverConfig):
        self.cua = cua
        self.pid = pid
        self.window_id = window_id
        self.client = client
        self.cfg = cfg
        self.steps: list[StepRecord] = []

    # ── screenshot helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_blank(png_bytes: bytes, min_real_size: int = 8_000) -> bool:
        """Return True if the PNG is suspiciously small (blank / GPU-rendering miss).

        Apps that use GPU-accelerated or Electron-based rendering can return
        a tiny 1 KB placeholder instead of the real window pixels when captured
        via the AX window path.  A real screenshot of any visible UI content
        will be larger than the threshold.
        """
        if len(png_bytes) < min_real_size:
            return True
        # Secondary check: nearly all pixels white or black → blank frame
        try:
            from PIL import Image, ImageStat
            from io import BytesIO
            img = Image.open(BytesIO(png_bytes)).convert("L")
            stat = ImageStat.Stat(img)
            # stddev < 8 means extremely little variation → solid-color image
            return stat.stddev[0] < 8
        except Exception:
            return False

    async def _screencapture_fallback(self) -> bytes:
        """Capture the screen via macOS screencapture.

        Used when cua-driver returns a blank PNG for GPU-rendered apps
        (Electron, Metal, etc.).  screencapture reads the composited framebuffer
        so it always captures what is actually on screen.

        Raises RuntimeError if the target process cannot be confirmed as the
        frontmost window — we never capture a wrong window and feed garbage to
        the Vision LLM.
        """
        import tempfile, os, subprocess as sp
        loop = asyncio.get_event_loop()

        def _run(cmd, timeout=3):
            return sp.run(cmd, timeout=timeout, capture_output=True, text=True)

        # Step 1: activate the target process by PID (generic — no app name).
        result = await loop.run_in_executor(
            None,
            lambda: _run([
                "osascript", "-e",
                f'tell application "System Events" to set frontmost of '
                f'first process whose unix id is {self.pid} to true',
            ]),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"[vision] could not activate pid={self.pid}: {result.stderr.strip()}"
            )

        await asyncio.sleep(0.4)  # let the window manager finish raising the window

        # Step 2: verify the target is actually frontmost before we capture.
        verify = await loop.run_in_executor(
            None,
            lambda: _run([
                "osascript", "-e",
                'tell application "System Events" to unix id of '
                'first process whose frontmost is true',
            ]),
        )
        frontmost_pid_str = verify.stdout.strip()
        if not frontmost_pid_str.isdigit() or int(frontmost_pid_str) != self.pid:
            raise RuntimeError(
                f"[vision] target app (pid={self.pid}) is not frontmost "
                f"(frontmost pid={frontmost_pid_str!r}) — aborting screencapture "
                f"to avoid capturing the wrong window"
            )

        # Step 3: safe to capture — target app is confirmed frontmost.
        tmp = tempfile.mktemp(suffix=".png")
        try:
            await loop.run_in_executor(
                None,
                lambda: _run(["screencapture", "-x", "-o", tmp], timeout=5),
            )
            return Path(tmp).read_bytes()
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    async def _screenshot_annotated(
        self,
    ) -> tuple[bytes, bytes, list[DesktopElement], str, int, int]:
        """Take a screenshot, falling back to screencapture for blank frames.

        Returns (visual_png, annotated_png, elements, legend, logical_w, logical_h).

        IMPORTANT — two separate concerns:
        • visual_png  → what the Vision LLM sees. cua-driver for native apps;
                        screencapture fallback for Electron / GPU-rendered apps.
        • logical_w/h → cua-driver's coordinate space (always logical pixels,
                        regardless of Retina scale). We get these from the
                        cua-driver screenshot even when it's blank, because
                        cua-driver always returns its own pixel space.
                        screencapture returns Retina pixels (2×) on HiDPI
                        displays, so we must NOT use its dimensions for clicks.
        """
        import tempfile, os, subprocess as _sp, json as _json
        from PIL import Image as _PILImage
        from io import BytesIO as _BytesIO

        def _logical_size_for_screenshot(png_bytes: bytes) -> tuple[int, int]:
            """Convert screencapture physical dims → logical dims (DPR-aware).

            Mirrors browser/highlight.py's devicePixelRatio approach: screencapture
            returns physical pixels (2× on Retina), cua-driver click needs logical pixels.
            We match the PNG dimensions against system_profiler display data to find the
            display's logical resolution, exactly as the browser uses devicePixelRatio.
            """
            try:
                img = _PILImage.open(_BytesIO(png_bytes))
                phys_w, phys_h = img.size
            except Exception:
                return 1920, 1080

            try:
                out = _sp.run(
                    ["system_profiler", "SPDisplaysDataType", "-json"],
                    capture_output=True, text=True, timeout=8,
                ).stdout
                data = _json.loads(out)
                for gpu in data.get("SPDisplaysDataType", []):
                    for display in gpu.get("spdisplays_ndrvs", []):
                        pixels_str = display.get("_spdisplays_pixels", "")
                        res_str = display.get("_spdisplays_resolution", "")
                        if not pixels_str or not res_str:
                            continue
                        try:
                            pw, ph = [int(x.strip()) for x in pixels_str.split("x")]
                            lw, lh = [int(x.split("@")[0].strip()) for x in res_str.split("x")]
                            if pw == phys_w and ph == phys_h:
                                return lw, lh
                        except Exception:
                            continue
            except Exception:
                pass

            # Fallback: if PNG > 2048px wide, assume Retina 2× scale
            if phys_w > 2048:
                return phys_w // 2, phys_h // 2
            return phys_w, phys_h

        # For Electron apps, cua-driver get_screenshot ALWAYS returns blank —
        # confirmed across all session logs. Skip that call entirely and go
        # straight to screencapture to save one wasted round-trip per turn.
        if self.cfg.electron_port:
            print("[vision] Electron app — skipping cua screenshot, using screencapture directly")
            visual_png = await self._screencapture_fallback()
            logical_w, logical_h = _logical_size_for_screenshot(visual_png)
        else:
            tmp = tempfile.mktemp(suffix=".png")
            try:
                await _async_call(self.cua, "get_screenshot", self.pid, self.window_id, tmp)
                cua_png = Path(tmp).read_bytes()
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

            if self._is_blank(cua_png):
                print("[vision] cua-driver screenshot is blank — falling back to screencapture")
                visual_png = await self._screencapture_fallback()
                logical_w, logical_h = _logical_size_for_screenshot(visual_png)
            else:
                visual_png = cua_png
                try:
                    _img = _PILImage.open(_BytesIO(visual_png))
                    logical_w, logical_h = _img.size   # cua-driver is already logical pixels
                except Exception:
                    logical_w, logical_h = 1920, 1080

        elements: list[DesktopElement] = []
        legend = ""
        try:
            ax_resp = await _async_call(
                self.cua, "get_window_state", self.pid, self.window_id, "ax"
            )
            _, _, elements = _parse_ax_tree(ax_resp)
            if elements:
                legend = make_legend(elements)
        except Exception:
            pass

        annotated = annotate(visual_png, elements) if elements else visual_png
        return visual_png, annotated, elements, legend, logical_w, logical_h

    # ── duplicate-action fingerprint ─────────────────────────────────────────

    @staticmethod
    def _action_fingerprint(decision: dict) -> str:
        """Stable key for an action, used to detect loops."""
        dtype = decision.get("type", "")
        if dtype == "type_text":
            return f"type_text:{decision.get('value', '').strip()}"
        if dtype == "click":
            return f"click:{decision.get('element_index', '')}"
        if dtype == "click_bbox":
            # Round bbox coords to nearest 50 px so near-identical clicks match
            bbox = decision.get("bbox") or [0, 0, 0, 0]
            rounded = [round(v / 50) * 50 for v in bbox]
            return f"click_bbox:{rounded}"
        if dtype == "press_key":
            return f"press_key:{decision.get('value', '')}"
        return dtype

    # ── parse helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_vlm(reply_obj) -> dict:
        raw = (reply_obj.get("text", "") if isinstance(reply_obj, dict) else reply_obj.text).strip()
        if not raw:
            raise ValueError("Vision LLM returned empty response")
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)

    # ── action description (for the verify prompt) ────────────────────────────

    @staticmethod
    def _describe(decision: dict) -> str:
        dtype = decision.get("type", "unknown")
        if dtype == "click":
            return f"clicked element {decision.get('element_index')}"
        if dtype == "click_bbox":
            return f"clicked at bbox {decision.get('bbox')}"
        if dtype == "type_text":
            return f"typed text: \"{decision.get('value', '')}\""
        if dtype == "press_key":
            return f"pressed key: {decision.get('value', '')}"
        return dtype

    # ── app activation helpers ────────────────────────────────────────────────

    async def _activate_app_for_action(self) -> None:
        """Bring the target app to the foreground immediately before a cua-driver action.

        cua-driver's click uses CGEventPost which delivers to whichever app is frontmost
        at the moment of posting — not a specific PID. The LLM call between SCAN and ACT
        takes 9-15 seconds, during which the terminal/browser reclaims focus. Without this
        call, clicks land in the wrong window.
        """
        from .applescript import _run_osascript
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: _run_osascript(
                f'tell application "{self.cfg.app_name}" to activate'
            ),
        )
        await asyncio.sleep(0.2)   # let the window manager finish raising the window

    # ── keyboard via System Events ────────────────────────────────────────────

    async def _keystroke_via_system_events(self, steps: list[dict]) -> None:
        """Send keystrokes via System Events (works for Electron/WebView apps).

        Activates the target app by name before sending so keystrokes land in
        the correct window, not whatever happened to be focused during the
        async LLM call that preceded this action.
        """
        from .applescript import keystroke_sequence
        loop = asyncio.get_event_loop()
        ok, out = await loop.run_in_executor(
            None,
            lambda: keystroke_sequence(steps, app_name=self.cfg.app_name),
        )
        if not ok:
            raise RuntimeError(f"System Events keystroke failed: {out}")

    # ── execute one action ────────────────────────────────────────────────────

    async def _execute(
        self,
        decision: dict,
        elements: list[DesktopElement],
        orig_w: int,
        orig_h: int,
    ) -> dict:
        """Execute the action. Returns action_record dict. Raises on failure.

        orig_w / orig_h are the pixel dimensions of the ORIGINAL unresized
        screenshot.  to_data_url() shrinks the image before sending it to the
        Vision LLM, but the LLM returns click_bbox in 0-1000 NORMALISED
        coordinates that are fraction-of-image-width / height — so mapping
        those fractions back to the ORIGINAL dimensions gives the correct
        physical screen pixel, provided the resize preserved the aspect ratio
        (which to_data_url guarantees).
        """
        dtype = decision.get("type")
        record: dict[str, Any] = {"decision": decision}

        if dtype == "click":
            await self._activate_app_for_action()
            idx = decision.get("element_index")
            target = next((el for el in elements if el.id == idx), None)
            if not target:
                raise ValueError(f"element_index {idx} not found in current AX tree")
            cx, cy = target.x + target.w / 2, target.y + target.h / 2
            await _async_call(self.cua, "click", self.pid, self.window_id, None, cx, cy)
            record["coords"] = (cx, cy)

        elif dtype == "click_bbox":
            await self._activate_app_for_action()
            bbox = decision.get("bbox", [0, 0, 0, 0])
            ymin, xmin, ymax, xmax = bbox
            # Map 0-1000 normalised LLM coordinates → logical screen pixels.
            # orig_w/orig_h are the screen's logical dimensions (DPR-corrected),
            # so (coord/1000) × logical_dim gives the correct cua-driver position.
            cx = ((xmin + xmax) / 2) / 1000.0 * orig_w
            cy = ((ymin + ymax) / 2) / 1000.0 * orig_h
            print(f"[vision/_execute] click_bbox → ({cx:.0f}, {cy:.0f}) | logical screen {orig_w}×{orig_h}")
            await _async_call(self.cua, "click", self.pid, self.window_id, None, cx, cy)
            record["coords"] = (cx, cy)

        elif dtype == "type_text":
            # cua-driver type_text does NOT work for Electron/WebView apps because
            # keyboard events sent to the PID are not routed into the DOM text input.
            # System Events keystroke activates the app first then sends OS-level key
            # events through the window manager — the correct path for all apps.
            await self._keystroke_via_system_events([{"text": str(decision.get("value", ""))}])

        elif dtype == "press_key":
            # LLM sends "Return", "Escape", or "cmd+a", "cmd+shift+k" etc.
            # Split on "+" so keystroke_sequence sees separate modifier / key parts.
            raw_key = str(decision.get("value", ""))
            parts = [p.strip() for p in raw_key.split("+") if p.strip()]
            await self._keystroke_via_system_events([{"keys": parts}])

        else:
            raise ValueError(f"unknown action type: {dtype}")

        return record

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> DriverResult:
        failures = 0
        last_next_hint: str = ""   # carries VERIFY's suggested next step into the next SCAN
        typed_values: set[str] = set()   # every text value typed this session — never shrinks

        for turn in range(self.cfg.max_steps):
            art = Path(self.cfg.artifacts_dir) if self.cfg.artifacts_dir else None

            # ── SCAN ─────────────────────────────────────────────────────────
            try:
                raw_png, ann_png, elements, legend, orig_w, orig_h = \
                    await self._screenshot_annotated()
            except Exception as exc:
                return DriverResult(success=False, note=f"screenshot failed: {exc}")
            # orig_w / orig_h are cua-driver's logical pixel dimensions — correct
            # for click coordinate mapping regardless of display Retina scale.

            if art:
                _save_png(raw_png, art / f"turn_{turn:02d}_scan.png")
                _save_png(ann_png, art / f"turn_{turn:02d}_scan_annotated.png")

            scan_url = to_data_url(ann_png)   # resizes internally; raw_png unchanged

            # Build history context (last 4 outcomes) so LLM doesn't repeat actions
            history_lines = [
                f"  Turn {s.turn + 1}: {s.outcome}"
                for s in self.steps[-4:]
            ]
            history_ctx = (
                "Recent action history (do NOT repeat these):\n"
                + "\n".join(history_lines) + "\n\n"
            ) if history_lines else ""

            # Priority order for guidance injected into the SCAN prompt:
            #   1. last_next_hint (from previous VERIFY — most current, turn-level)
            #   2. prior_context  (from earlier cascade layers — session-level, first turn only)
            # We show prior_context only when there's no active hint so the
            # two don't compete. Once Vision takes its first action and VERIFY
            # responds, last_next_hint takes over and prior_context is no longer needed.
            if last_next_hint:
                hint_ctx = (
                    f"IMPORTANT — the last verification step says the next action should be:\n"
                    f"  {last_next_hint}\n\n"
                )
            elif self.cfg.prior_context:
                hint_ctx = (
                    "HANDOFF CONTEXT — earlier layers already attempted parts of this task:\n"
                    + "\n".join(f"  • {c}" for c in self.cfg.prior_context)
                    + "\n"
                    "Look at the screenshot carefully: some steps may already be done. "
                    "Continue from the current state — do NOT start over from scratch.\n\n"
                )
            else:
                hint_ctx = ""

            # ── LLM: what should I do? ────────────────────────────────────────
            action_prompt = (
                f"You are controlling a macOS desktop application.\n"
                f"GOAL: {self.cfg.goal}\n\n"
                f"{history_ctx}"
                f"{hint_ctx}"
                f"Numbered element legend (elements visible in the screenshot):\n"
                f"{legend or '(no numbered elements — use click_bbox for coordinates)'}\n\n"
                "Look at the screenshot carefully. Choose ONE action to take toward the goal.\n\n"
                "RULES:\n"
                "- If the goal is already complete in this screenshot, reply with {\"type\":\"done\"}.\n"
                "- Click a NUMBERED element using its element_index from the legend.\n"
                "- Click an UN-numbered element using click_bbox with 0-1000 normalized [ymin,xmin,ymax,xmax].\n"
                "- type_text types into the CURRENTLY FOCUSED field. It does NOT press Enter.\n"
                "- Use press_key with 'Return' to submit, 'Escape' to cancel, 'Tab' to move focus.\n"
                "- If a search field already contains the correct text, do NOT type it again.\n"
                "- When search results are grouped into labelled sections (e.g. 'Contacts', 'Chats',\n"
                "  'Recent', 'Files', 'Messages', 'Media'): target items in the FIRST / TOPMOST section\n"
                "  that directly represents the item you need to open, NOT lower sections that show\n"
                "  content WITHIN that item. The first section is always the most direct path.\n"
                "- When search results appear in a list, prefer KEYBOARD navigation over click_bbox:\n"
                "    press_key 'Down' → moves focus to the very first result at the TOP of the list\n"
                "    press_key 'Return' → opens / selects the highlighted result\n"
                "  This avoids misidentifying which row or section a coordinate belongs to.\n"
                "- Use click_bbox for buttons, icons, toolbar items, and UI chrome. For list items,\n"
                "  prefer keyboard nav. If click_bbox on a list item fails (VERIFY=false), switch to keyboard.\n"
                "- NEVER use press_key 'cmd+a' inside a search bar — it corrupts the search input.\n"
                "  To clear a search: press_key 'Escape' to exit search mode, then reopen it.\n"
                "- To REPLACE text in a non-search text field: press_key 'cmd+a' then type_text.\n\n"
                "Reply with ONLY one JSON object — no markdown fences:\n"
                "{\"type\":\"click\",\"element_index\":<N>}\n"
                "{\"type\":\"click_bbox\",\"bbox\":[ymin,xmin,ymax,xmax]}\n"
                "{\"type\":\"type_text\",\"value\":\"<text>\"}\n"
                "{\"type\":\"press_key\",\"value\":\"<key>\"}\n"
                "{\"type\":\"done\",\"result\":\"<what was accomplished>\"}"
            )

            try:
                reply = await self.client.vision(
                    scan_url, action_prompt,
                    provider=self.cfg.provider, max_tokens=300,
                )
                decision = self._parse_vlm(reply)
                print(f"[vision/turn{turn}] SCAN decision: {decision}")
            except Exception as exc:
                failures += 1
                self.steps.append(StepRecord(
                    turn=turn, actions=[], outcome=f"scan_parse_error: {exc}",
                ))
                if failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False,
                        note=f"VLM action parse failed {failures}×: {exc}",
                        steps=self.steps,
                    )
                continue

            # goal already complete per SCAN observation
            if decision.get("type") == "done":
                self.steps.append(StepRecord(turn=turn, actions=[], outcome="done"))
                return DriverResult(
                    success=True, note="done", steps=self.steps,
                    turns=turn + 1, actions=[s.__dict__ for s in self.steps],
                    content=decision.get("result", ""),
                )

            action_desc = self._describe(decision)

            # ── DUPLICATE type_text GUARD ─────────────────────────────────────
            # typed_values is a set that grows for the entire session (never
            # shrinks), so the same text typed at turn 1 is still blocked at
            # turn 10 even if many clicks happened in between.
            # Key presses and clicks are intentionally excluded — Return is
            # legitimately pressed twice ("navigate to contact" then "send"),
            # and clicks at slightly different coordinates are normal.
            if decision.get("type") == "type_text":
                value = decision.get("value", "").strip()
                if value in typed_values:
                    failures += 1
                    reason = (
                        f"duplicate type_text blocked: already typed '{value}' and it was "
                        "verified as appearing in the correct field. "
                        "Do not type it again — click the search result instead, "
                        "or press Escape to clear and refocus."
                    )
                    last_next_hint = ""
                    self.steps.append(StepRecord(
                        turn=turn, actions=[], outcome=f"loop_detected: {reason}",
                    ))
                    print(f"[vision/turn{turn}] LOOP DETECTED — {reason}")
                    if failures >= self.cfg.max_failures:
                        return DriverResult(
                            success=False,
                            note=f"type_text loop after {failures} repeats. "
                                 f"Last blocked: '{value}'",
                            steps=self.steps,
                        )
                    continue
                # Do NOT add to typed_values yet — only add after VERIFY confirms
                # the text actually appeared in the right field. If typing fails
                # silently (field not focused, wrong target), the LLM must be
                # allowed to retry rather than being permanently blocked.

            # ── CLICK-LOOP SAFETY NET ─────────────────────────────────────────
            # If the LLM has tried click_bbox 2+ times in a row and VERIFY
            # confirmed each click (confirmed=True) but the goal is still not
            # complete, the click is firing but not opening anything.
            # Override with keyboard Down+Return, which is more reliable for
            # selecting items from any list/search result in any macOS app.
            recent_confirmed_clicks = sum(
                1 for s in self.steps[-2:]
                if ("click_bbox" in str(s.actions) or "click" in str(s.actions))
                and "confirmed:" in s.outcome and "False" not in s.outcome
            )
            if (recent_confirmed_clicks >= 2
                    and decision.get("type") in ("click", "click_bbox")):
                print(f"[vision/turn{turn}] overriding click with Down+Return "
                      f"after {recent_confirmed_clicks} confirmed-but-incomplete clicks")
                decision = {"type": "press_key", "value": "Down"}
                action_desc = "pressed key: Down (keyboard-nav override after repeated click failures)"
                # We'll press Down this turn, Return next turn via the loop
            # ─────────────────────────────────────────────────────────────────

            print(f"[vision/turn{turn}] action: {action_desc}")

            # ── ACT ──────────────────────────────────────────────────────────
            try:
                action_record = await self._execute(decision, elements, orig_w, orig_h)
                action_record["turn"] = turn
                await asyncio.sleep(0.6)   # let UI settle before verify screenshot
            except Exception as exc:
                failures += 1
                self.steps.append(StepRecord(
                    turn=turn,
                    actions=[{"turn": turn, "decision": decision}],
                    outcome=f"act_error: {exc}",
                ))
                if failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False,
                        note=f"action failed {failures}×: {exc}",
                        steps=self.steps,
                    )
                continue

            # ── VERIFY — ask the LLM "did that work?" ────────────────────────
            try:
                v_raw, v_ann, _, _, _, _ = await self._screenshot_annotated()
            except Exception as exc:
                # Can't take verify screenshot — count as unconfirmed
                failures += 1
                self.steps.append(StepRecord(
                    turn=turn, actions=[action_record], outcome=f"verify_screenshot_failed: {exc}",
                ))
                if failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False, note=f"verify screenshot failed {failures}×",
                        steps=self.steps,
                    )
                continue

            if art:
                _save_png(v_raw, art / f"turn_{turn:02d}_verify.png")
                _save_png(v_ann, art / f"turn_{turn:02d}_verify_annotated.png")

            verify_url = to_data_url(v_ann)

            verify_prompt = (
                f"You are a STRICT verifier for desktop automation.\n"
                f"FINAL GOAL: {self.cfg.goal}\n\n"
                f"The last action was: {action_desc}\n\n"
                "Look at this screenshot taken immediately AFTER the action.\n\n"
                "ABSOLUTE RULE: If this screenshot looks IDENTICAL to the screenshot before "
                "the action — same layout, same content, same right panel — then confirmed=false. "
                "A real action on a real UI always produces some visible change. "
                "Do NOT reason about what 'should have happened'. Only report what you can SEE changed.\n\n"
                "Answer these two questions using ONLY what you can see:\n"
                "1. Did the screenshot change in a way that shows genuine progress toward the goal? "
                "(confirmed=true requires visible change relevant to the goal, not clock/animation)\n"
                "2. Is the FINAL GOAL now fully achieved? "
                "(goal_complete=true only when you can clearly see the outcome the goal asked for)\n\n"
                "confirmed=false if: the screen looks unchanged, the action landed on the wrong element, "
                "or the right/main panel did not transition to a new view when a navigation action was taken.\n\n"
                "CRITICAL — clock ticks, cursor blinks, and menu-bar animations are NOT confirmation.\n"
                "For any click or key meant to open a contact/chat/file/document:\n"
                "  confirmed=true ONLY if the main content area visibly changed to show that item.\n"
                "  If the same search results, same chat list, or same empty panel is still visible: confirmed=false.\n\n"
                "next_hint (when confirmed=true but not complete) must be ONE specific UI action, "
                "not a description — e.g. 'press Down then Return to open the first search result'.\n\n"
                "Reply with ONLY one JSON object — no markdown fences:\n\n"
                "Goal fully achieved:\n"
                "{\"confirmed\":true,\"goal_complete\":true,\"result\":\"<describe what you see that proves the goal is done>\"}\n\n"
                "Action correct, more steps needed:\n"
                "{\"confirmed\":true,\"goal_complete\":false,\"next_hint\":\"<single specific next UI action>\"}\n\n"
                "Action was wrong or ineffective:\n"
                "{\"confirmed\":false,\"reason\":\"<what you see in the screenshot that shows it went wrong>\"}"
            )

            try:
                v_reply = await self.client.vision(
                    verify_url, verify_prompt,
                    provider=self.cfg.provider, max_tokens=256,
                )
                verdict = self._parse_vlm(v_reply)
            except Exception as exc:
                # Parse failure counts as unconfirmed
                failures += 1
                self.steps.append(StepRecord(
                    turn=turn, actions=[action_record], outcome=f"verify_parse_error: {exc}",
                ))
                if failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False, note=f"verify parse failed {failures}×: {exc}",
                        steps=self.steps,
                    )
                continue

            print(f"[vision/turn{turn}] verify: confirmed={verdict.get('confirmed')}, "
                  f"complete={verdict.get('goal_complete')}")

            # Goal complete — return success with the result
            if verdict.get("goal_complete"):
                result_text = verdict.get("result", action_desc)
                if decision.get("type") == "type_text":
                    typed_values.add(decision.get("value", "").strip())
                self.steps.append(StepRecord(
                    turn=turn, actions=[action_record], outcome=f"goal_complete: {result_text}",
                ))
                return DriverResult(
                    success=True, note="done", steps=self.steps,
                    turns=turn + 1, actions=[s.__dict__ for s in self.steps],
                    content=result_text,
                )

            # Action not confirmed — typing may have failed silently; do NOT
            # add to typed_values so the LLM can retry after refocusing.
            if not verdict.get("confirmed"):
                reason = verdict.get("reason", "action not confirmed by verify screenshot")
                last_next_hint = ""   # stale hint is invalid after a failed action
                failures += 1
                self.steps.append(StepRecord(
                    turn=turn, actions=[action_record], outcome=f"verify_failed: {reason}",
                ))
                print(f"[vision/turn{turn}] action not confirmed: {reason}")
                if failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False,
                        note=f"too many unconfirmed actions ({failures}×). "
                             f"Last failure: {reason}",
                        steps=self.steps,
                    )
                continue

            # Action confirmed, goal not yet complete — carry hint into next SCAN.
            # Only now do we record the typed value: VERIFY saw the text appear
            # in the correct field, so retrying the same text IS a loop.
            if decision.get("type") == "type_text":
                typed_values.add(decision.get("value", "").strip())

            next_hint = verdict.get("next_hint", "continue toward goal")
            last_next_hint = next_hint   # injected into the next turn's action_prompt
            self.steps.append(StepRecord(
                turn=turn, actions=[action_record],
                outcome=f"confirmed: {next_hint}",
            ))

        return DriverResult(
            success=False,
            note=f"max_steps ({self.cfg.max_steps}) reached without completing goal",
            steps=self.steps, turns=self.cfg.max_steps,
            actions=[s.__dict__ for s in self.steps],
        )
