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
        args: dict[str, Any] = {"pid": pid, "key": key, "modifiers": modifiers}
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("hotkey", args)


def _get_pid_and_window(
    cua: CuaDriver,
    app_name: str,
    bundle_id: str,
    wait_s: float = 1.0,
    electron_debugging_port: Optional[int] = None,
) -> tuple[int, int]:
    """Launch (or find) the app and return (pid, window_id).

    Waits briefly for the window to appear if launch_app returns no windows.
    Pass electron_debugging_port for Electron apps to enable CDP access.
    """
    try:
        resp = cua.launch_app(
            bundle_id=bundle_id or None,
            name=app_name or None,
            electron_debugging_port=electron_debugging_port,
        )
    except RuntimeError as exc:
        if bundle_id and app_name:
            print(f"[computer/daemon] launch_app failed with bundle_id {bundle_id}, falling back to name={app_name}")
            resp = cua.launch_app(
                bundle_id=None, name=app_name,
                electron_debugging_port=electron_debugging_port,   # preserve port in fallback
            )
        else:
            raise exc

    pid = resp["pid"]
    windows = resp.get("windows") or []

    if not windows:
        # App may need a moment to open its first window
        time.sleep(wait_s)
        windows = cua.list_windows(pid)

    if not windows:
        raise CuaServerUnavailable(
            f"No windows found for {app_name or bundle_id} (pid={pid}). "
            "The app may still be launching — retry in a moment."
        )

    return pid, windows[0]["window_id"]


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
