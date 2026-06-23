"""cua-driver daemon management and thin CLI wrapper.

cua-driver communicates via a Unix socket:
    /Users/<user>/Library/Caches/cua-driver/cua-driver.sock

The correct integration pattern is:
    cua-driver call <tool> '<json>'    ← dispatches a tool call
    cua-driver status                  ← checks if daemon is running
    cua-driver serve                   ← starts the daemon

The cua-computer Python SDK (use_host_computer_server=True) talks TCP and
does NOT work with cua-driver's Unix socket.  Use this module instead.

One-time macOS setup:
    cua-driver permissions grant   # accept both TCC dialogs
"""
from __future__ import annotations

import json
import subprocess
import asyncio
import time
from typing import Any, Optional


class CuaServerUnavailable(RuntimeError):
    """Raised when cua-driver daemon is not running or cannot be started."""


# ── probe & lifecycle ─────────────────────────────────────────────────────────

def _probe_sync() -> bool:
    """Return True if the cua-driver daemon is running (synchronous)."""
    try:
        r = subprocess.run(
            ["cua-driver", "status"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


async def _probe_server() -> bool:
    """Async wrapper for the daemon probe (runs in executor to avoid blocking)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _probe_sync)


async def ensure_daemon() -> None:
    """Start cua-driver serve if not already running. Idempotent."""
    if await _probe_server():
        return

    try:
        subprocess.Popen(
            ["cua-driver", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CuaServerUnavailable(
            "cua-driver binary not found on PATH. "
            "Install it from https://github.com/trycua/cua and run "
            "'cua-driver serve' before starting the agent. "
            "See CLAUDE.md for setup instructions."
        ) from exc

    print("[computer/daemon] waiting for cua-driver daemon to start …")
    for attempt in range(60):   # up to 30 s
        await asyncio.sleep(0.5)
        if await _probe_server():
            print(f"[computer/daemon] daemon is up (after {(attempt + 1) * 0.5:.1f}s)")
            return

    raise CuaServerUnavailable(
        "cua-driver daemon failed to start within 30 s. "
        "Try running 'cua-driver serve' manually in a terminal and check for errors."
    )


# ── CuaDriver: thin subprocess wrapper ───────────────────────────────────────

class CuaDriver:
    """Thin wrapper around 'cua-driver call <tool> <json>'.

    All tool calls go through the Unix socket the daemon listens on.
    Methods are synchronous; the async cascade in skill.py runs them in
    an executor so the event loop is not blocked.
    """

    def __init__(self, binary: str = "cua-driver"):
        self.binary = binary

    def call(self, tool: str, args: dict[str, Any], timeout: float = 30.0) -> dict:
        """Dispatch one tool call and return the parsed JSON response."""
        result = subprocess.run(
            [self.binary, "call", tool, json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cua-driver {tool} failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:300]}"
            )
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"raw_output": stdout}

    # ── app lifecycle ─────────────────────────────────────────────────────────

    def launch_app(
        self,
        bundle_id: Optional[str] = None,
        name: Optional[str] = None,
        electron_debugging_port: Optional[int] = None,
    ) -> dict:
        """Launch (or locate) an app; return {pid, windows: [{window_id, ...}]}.

        If the app is already running, returns the existing pid and windows.
        Pass electron_debugging_port for Electron apps (WhatsApp, Slack, VS Code, etc.)
        to enable CDP access — without it, Electron returns element_count=0.
        """
        args: dict[str, Any] = {}
        if bundle_id:
            args["bundle_id"] = bundle_id
        elif name:
            args["name"] = name
        else:
            raise ValueError("launch_app requires bundle_id or name")
        if electron_debugging_port:
            args["electron_debugging_port"] = electron_debugging_port
        return self.call("launch_app", args)

    def list_windows(self, pid: int) -> list[dict]:
        """Return all top-level windows for a pid."""
        resp = self.call("list_windows", {"pid": pid})
        return resp.get("windows") or resp if isinstance(resp, list) else []

    # ── perception ────────────────────────────────────────────────────────────

    def get_window_state(
        self,
        pid: int,
        window_id: int,
        mode: str = "ax",
        query: Optional[str] = None,
        screenshot_out: Optional[str] = None,
    ) -> dict:
        """Read AX tree (and optionally screenshot) for a window.

        mode:
          "ax"     → AX tree only (tree_markdown + element_count)
          "vision" → screenshot only (screenshot_file_path or base64)
          "som"    → both (default for cua-driver)

        Returns dict with at least tree_markdown and element_count.
        """
        args: dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "capture_mode": mode,
        }
        if query:
            args["query"] = query
        if screenshot_out:
            args["screenshot_out_file"] = screenshot_out
        return self.call("get_window_state", args)

    def get_screenshot(self, pid: int, window_id: int, out_file: str) -> None:
        """Write a PNG screenshot of window to out_file."""
        self.get_window_state(pid, window_id, mode="vision", screenshot_out=out_file)

    def page(
        self,
        pid: int,
        action: str,
        selector: Optional[str] = None,
        value: Optional[str] = None,
        script: Optional[str] = None,
    ) -> dict:
        """Interact with an Electron/Chromium app via Chrome DevTools Protocol.

        Requires the app to have been launched with electron_debugging_port.
        Actions: 'click', 'type', 'evaluate', 'wait_for_selector', etc.
        """
        args: dict[str, Any] = {"pid": pid, "action": action}
        if selector is not None:
            args["selector"] = selector
        if value is not None:
            args["value"] = value
        if script is not None:
            args["script"] = script
        return self.call("page", args)

    # ── action ────────────────────────────────────────────────────────────────

    def click(
        self,
        pid: int,
        window_id: int,
        element_index: Optional[int] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> dict:
        args: dict[str, Any] = {"pid": pid, "window_id": window_id}
        if element_index is not None:
            args["element_index"] = element_index
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self.call("click", args)

    def type_text(
        self,
        pid: int,
        text: str,
        element_index: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> dict:
        args: dict[str, Any] = {"pid": pid, "text": text}
        if element_index is not None and window_id is not None:
            args["element_index"] = element_index
            args["window_id"] = window_id
        return self.call("type_text", args)

    def press_key(
        self,
        pid: int,
        key: str,
        modifiers: Optional[list[str]] = None,
        window_id: Optional[int] = None,
    ) -> dict:
        args: dict[str, Any] = {"pid": pid, "key": key}
        if modifiers:
            args["modifiers"] = modifiers
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("press_key", args)

    def hotkey(
        self,
        pid: int,
        key: str,
        modifiers: list[str],
        window_id: Optional[int] = None,
    ) -> dict:
        # cua-driver's `hotkey` expects a single `keys` array of [*modifiers, key]
        # (e.g. ["cmd","c"]) — NOT separate key/modifiers fields.
        args: dict[str, Any] = {"pid": pid, "keys": [*modifiers, key]}
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("hotkey", args)


def resolve_window(cua: CuaDriver, app_name: str) -> Optional[dict]:
    """Resolve the target app's frontmost on-screen window BY APP NAME.

    This is the single source of truth for (pid, window_id, bounds) — never the
    raw pid returned by launch_app (which can be wrong/background) and never the
    agent's own browser. Matches against every window's app_name, filters to
    on-screen windows on the current Space, and picks the frontmost (highest z).

    Returns {pid, window_id, x, y, w, h} in logical points, or None if not found.
    """
    name_lower = app_name.lower().strip()
    try:
        resp = cua.call("list_windows", {})
        wins = resp.get("windows") if isinstance(resp, dict) else resp
        wins = wins or []
    except Exception as exc:
        print(f"[computer/daemon] resolve_window list_windows failed: {exc}")
        return None

    cands = []
    for w in wins:
        wname = (w.get("app_name") or "").lower()
        if not wname or (name_lower not in wname and wname not in name_lower):
            continue
        # is_on_screen=True already implies the window is visible on the current
        # Space. We do NOT filter on on_current_space — cua-driver leaves it None
        # for many windows, and `not None` would wrongly drop a valid window.
        if not w.get("is_on_screen"):
            continue
        b = w.get("bounds") or {}
        if b.get("width", 0) < 200 or b.get("height", 0) < 150:
            continue   # skip tiny helper/popover windows (menu-bar items, badges)
        cands.append(w)

    if not cands:
        return None

    win = max(cands, key=lambda w: w.get("z_index", 0))
    b = win["bounds"]
    return {
        "pid": win["pid"], "window_id": win["window_id"],
        "x": int(b["x"]), "y": int(b["y"]),
        "w": int(b["width"]), "h": int(b["height"]),
    }


def _frontmost_window_app(cua: CuaDriver) -> Optional[str]:
    """Return the app_name of the frontmost on-screen window (highest z), or None."""
    try:
        resp = cua.call("list_windows", {})
        wins = resp.get("windows") if isinstance(resp, dict) else resp
        on = [w for w in (wins or []) if w.get("is_on_screen")
              and (w.get("bounds") or {}).get("width", 0) > 200]
        if not on:
            return None
        return (max(on, key=lambda w: w.get("z_index", 0)).get("app_name") or "") or None
    except Exception:
        return None


def _get_pid_and_window(
    cua: CuaDriver,
    app_name: str,
    bundle_id: str,
    wait_s: float = 0.5,
) -> tuple[int, int]:
    """Launch the app, bring it to the foreground, and return its resolved
    (pid, window_id) — both from resolve_window (BY APP NAME), never the raw
    launch pid.

    NOTE: we never pass electron_debugging_port. That would force launch_app to
    relaunch the app in the BACKGROUND (to inject --remote-debugging-port),
    defeating the pre-flight foreground activation.
    """
    # 1. Launch (background) by bundle_id, falling back to name. This registers
    #    the app with cua-driver but does NOT guarantee a window is open.
    try:
        cua.launch_app(bundle_id=bundle_id or None, name=app_name or None)
    except RuntimeError as exc:
        if bundle_id and app_name:
            print(f"[computer/daemon] launch_app failed with bundle_id {bundle_id}, "
                  f"falling back to name={app_name}")
            try:
                cua.launch_app(bundle_id=None, name=app_name)
            except RuntimeError:
                pass   # may already be running; resolve_window below decides
        else:
            raise exc

    # 2. SURFACE A WINDOW. Many apps (Telegram, Slack, WhatsApp, Spotify…) keep
    #    running in the menu bar with NO window after you close it. `tell app to
    #    activate` only foregrounds a windowless app — it does NOT reopen the
    #    window. The macOS `open` command sends the "reopenApplication" Apple
    #    event (same as clicking the Dock icon), which reliably reopens the main
    #    window AND launches the app if it isn't running.
    def _surface_window():
        cmd = ["open", "-b", bundle_id] if bundle_id else ["open", "-a", app_name]
        subprocess.run(cmd, check=False, capture_output=True)

    _surface_window()
    time.sleep(wait_s)

    # 3. Poll resolve_window (by app name) until the window appears (up to ~10s).
    #    Re-nudge with `open` periodically in case a cold start was slow.
    win = None
    for i in range(20):
        win = resolve_window(cua, app_name)
        if win:
            break
        if i in (4, 10):          # re-trigger reopen if still no window
            _surface_window()
        time.sleep(0.5)

    if not win:
        raise CuaServerUnavailable(
            f"{app_name or bundle_id} window never appeared on screen — "
            "is the app installed and able to open a window?"
        )

    # 4. Re-assert foreground and verify the frontmost window is the target
    #    (best-effort; capture-by-window-id is occlusion-proof anyway).
    for _ in range(3):
        front = _frontmost_window_app(cua)
        if front and app_name.lower() in front.lower():
            break
        activate_app(app_name, wait=0.3)
    else:
        print(f"[computer/daemon] warning: {app_name} not confirmed frontmost "
              f"(front={_frontmost_window_app(cua)!r}) — proceeding (capture is by window id)")

    print(f"[computer/daemon] resolved {app_name}: pid={win['pid']} "
          f"window_id={win['window_id']} bounds=({win['x']},{win['y']},{win['w']},{win['h']})")
    return win["pid"], win["window_id"]


def get_cua_driver() -> CuaDriver:
    """Return a CuaDriver instance (daemon must already be running)."""
    return CuaDriver()


# ── AppleScript-based app activation ─────────────────────────────────────────

def activate_app(app_name: str, wait: float = 0.5) -> None:
    """Bring the named app to the foreground using AppleScript.

    cua-driver's launch_app does NOT steal focus (self_activation_suppressed).
    This AppleScript call ensures the window is realized in the AX hierarchy
    before get_window_state is called.
    """
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        check=False,
        capture_output=True,
    )
    time.sleep(wait)
