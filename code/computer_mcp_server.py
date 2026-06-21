"""MCP server for computer-use tools (Session 10).

Wraps cua-driver (via cua-computer Python SDK) and AppleScript into MCP tools.
This server is an optional complement to the computer bypass skill: it exposes
computer control as MCP tools so other skills (e.g. the planner) can optionally
call them directly, and so you can test computer tools from the CLI.

Transport: stdio (same as mcp_server.py).

Usage:
    uv run python computer_mcp_server.py

Tools:
    computer_launch_app         Launch an app by name or bundle_id
    computer_list_apps          List running apps
    computer_get_window_state   Get accessibility tree of the foreground app
    computer_screenshot         Take a screenshot, return base64 PNG
    computer_click              Click at (x, y) screen coordinates
    computer_type_text          Type text at the current cursor
    computer_press_key          Press a named key (Return, Tab, Escape, etc.)
    computer_hotkey             Send a keyboard shortcut (e.g. cmd+c)
    computer_kill_app           Kill an app by name
    computer_run_applescript    Run an AppleScript command against an app
"""
from __future__ import annotations

import asyncio
import base64
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("computer")

# ── lazy singleton computer instance ─────────────────────────────────────────
_computer = None


async def _get() :
    global _computer
    if _computer is None:
        from computer.daemon import ensure_daemon, get_computer
        await ensure_daemon()
        _computer = await get_computer()
    return _computer


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def computer_launch_app(
    app_name: str,
    bundle_id: Optional[str] = None,
) -> dict:
    """Launch a desktop app and activate it.

    Args:
        app_name: Display name for AppleScript activation (e.g. "Calculator")
        bundle_id: macOS bundle identifier (e.g. "com.apple.calculator")
    """
    comp = await _get()
    result = {"app_name": app_name}
    try:
        if bundle_id:
            await comp.interface.launch(bundle_id)
        from computer.daemon import activate_app
        activate_app(app_name)
        result["status"] = "launched_and_activated"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


@mcp.tool()
async def computer_list_apps() -> dict:
    """Return a list of currently running applications."""
    comp = await _get()
    try:
        apps = await comp.interface.run_command("ps -e -o comm= | sort -u")
        return {"apps": apps.stdout.splitlines() if hasattr(apps, "stdout") else str(apps)}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def computer_get_window_state() -> dict:
    """Get the accessibility tree of the current foreground window.

    Returns element_count and the AX tree as text.
    """
    comp = await _get()
    try:
        ax_tree = await comp.interface.get_accessibility_tree()
        from computer.driver import _element_count, _ax_to_markdown, _ax_to_elements
        elements = _ax_to_elements(ax_tree)
        count = _element_count(ax_tree)
        return {
            "element_count": count,
            "ax_markdown": _ax_to_markdown(ax_tree, elements),
            "raw": ax_tree,
        }
    except Exception as exc:
        return {"element_count": 0, "error": str(exc)}


@mcp.tool()
async def computer_screenshot() -> dict:
    """Take a screenshot of the current screen.

    Returns base64-encoded PNG so the caller can display or send to a vision model.
    """
    comp = await _get()
    try:
        png_bytes = await comp.interface.screenshot()
        return {"image_base64": base64.b64encode(png_bytes).decode(), "format": "png"}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def computer_click(x: float, y: float) -> dict:
    """Left-click at (x, y) in screen coordinates."""
    comp = await _get()
    try:
        await comp.interface.left_click(x, y)
        return {"status": "clicked", "x": x, "y": y}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def computer_type_text(text: str) -> dict:
    """Type text at the current cursor position."""
    comp = await _get()
    try:
        await comp.interface.type_text(text)
        return {"status": "typed", "text": text}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def computer_press_key(key: str) -> dict:
    """Press a named key.

    Common values: Return, Tab, Escape, space, BackSpace, Delete,
    Up, Down, Left, Right, F1-F12.
    """
    comp = await _get()
    try:
        await comp.interface.press_key(key)
        return {"status": "pressed", "key": key}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def computer_hotkey(keys: str) -> dict:
    """Send a keyboard shortcut.

    Pass keys as a plus-separated string: "cmd+c", "ctrl+shift+t", "cmd+option+space".
    """
    comp = await _get()
    try:
        parts = [k.strip() for k in keys.split("+")]
        await comp.interface.hotkey(*parts)
        return {"status": "sent", "keys": parts}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def computer_kill_app(app_name: str) -> dict:
    """Terminate an application by display name."""
    import subprocess
    try:
        subprocess.run(["osascript", "-e", f'quit app "{app_name}"'], check=False)
        return {"status": "killed", "app_name": app_name}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def computer_run_applescript(app_name: str, script_body: str) -> dict:
    """Run an AppleScript command against a named app.

    Args:
        app_name: The application to address (e.g. "Notes", "Mail")
        script_body: AppleScript statements inside 'tell application X ... end tell',
                     e.g. 'make new note with properties {name:"Hi", body:"World"}'

    Returns success status and any output or error text.
    """
    from computer.applescript import run_as_action
    ok, output = run_as_action(app_name, script_body)
    return {"success": ok, "output": output}


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
