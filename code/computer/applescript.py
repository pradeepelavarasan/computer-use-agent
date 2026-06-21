"""AppleScript utilities for macOS app scripting.

AppleScript is complementary to cua-driver:
- cua-driver reads / acts through the OS accessibility API (structure level)
- AppleScript talks to the app's own scripting dictionary (semantic level)

For scriptable apps (Mail, Notes, Calendar, Numbers, Finder, Safari, Pages,
Keynote, Contacts, Reminders) one AppleScript command can do what would take
5+ clicks through the AX tree, and it can't break on UI reflows because it
addresses the app's data model directly.

Layer 2a of the cascade tries AppleScript first for scriptable apps, then
falls back to hotkeys, then to the AX-tree-based Layer 2b.
"""
from __future__ import annotations

import subprocess


def _run_osascript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Execute an AppleScript; return (success, stdout_or_stderr)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run() already killed the child before raising.
        return False, (
            "osascript timed out — Accessibility permission not granted. "
            "Fix: System Settings → Privacy & Security → Accessibility → "
            "add your terminal app, then re-run."
        )
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip()


def is_scriptable(app_name: str) -> bool:
    """Return True if the app exposes an AppleScript dictionary.

    We probe by asking the app for its own name via the standard suite.
    Most scriptable apps respond; non-scriptable ones error out.
    """
    ok, _ = _run_osascript(f'tell application "{app_name}" to return name')
    return ok


def run_as_query(app_name: str, script_body: str) -> tuple[bool, str]:
    """Execute a read-only AppleScript against app_name.

    script_body is the content of 'tell application X ... end tell', e.g.:
        'get body of first note'

    Returns (success, result_text).
    """
    full = f'tell application "{app_name}"\n{script_body}\nend tell'
    return _run_osascript(full)


def run_as_action(app_name: str, script_body: str) -> tuple[bool, str]:
    """Execute an action AppleScript against app_name.

    script_body is the content of 'tell application X ... end tell', e.g.:
        'make new note with properties {name:"Hello", body:"World"}'

    Returns (success, output_or_error).
    """
    full = f'tell application "{app_name}"\n{script_body}\nend tell'
    return _run_osascript(full)


def run_raw(script: str) -> tuple[bool, str]:
    """Execute a raw AppleScript (no automatic app wrapper)."""
    return _run_osascript(script)


# ── System Events keystroke helpers (no cua-driver needed) ───────────────────
# These let Layer 2a hotkeys work on any Mac without installing cua-driver.
# System Events requires Accessibility permission (same TCC as cua-driver).

_KEY_CODES: dict[str, int] = {
    "Return": 36, "Enter": 36,
    "Tab": 48,
    "Escape": 53,
    "Space": 49,
    "Delete": 51, "BackSpace": 51,
    "Up": 126, "Down": 125, "Left": 123, "Right": 124,
    "Home": 115, "End": 119, "PageUp": 116, "PageDown": 121,
    "F1": 122, "F2": 120, "F3": 99,  "F4": 118,
    "F5": 96,  "F6": 97,  "F7": 98,  "F8": 100,
    "F9": 101, "F10": 109, "F11": 103, "F12": 111,
    "cmd": None, "shift": None, "alt": None, "ctrl": None,  # modifiers (handled below)
}

_MODIFIER_MAP = {
    "cmd": "command down", "command": "command down",
    "shift": "shift down",
    "alt": "option down", "option": "option down",
    "ctrl": "control down", "control": "control down",
}


def keystroke_sequence(
    steps: list[dict],
    app_name: str = "",
) -> tuple[bool, str]:
    """Send keystrokes via System Events (no cua-driver required).

    Each step dict:
      {"keys": ["1"]}                 → keystroke "1"
      {"keys": ["Return"]}            → key code 36  (special key)
      {"keys": ["cmd", "c"]}          → keystroke "c" using {command down}
      {"text": "hello world"}         → keystroke "hello world"

    app_name: when provided, the script first activates the app so keystrokes
    land on the correct window, not whatever happened to have focus.

    Returns (success, output_or_error).
    """
    lines: list[str] = []
    for step in steps:
        text = step.get("text")
        keys = step.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]

        if text:
            safe = text.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'keystroke "{safe}"')
            continue

        if not keys:
            continue

        # Separate modifiers from the final key
        modifiers = [k for k in keys if k.lower() in _MODIFIER_MAP]
        main_keys = [k for k in keys if k.lower() not in _MODIFIER_MAP]
        mod_str = (
            " using {" + ", ".join(_MODIFIER_MAP[m.lower()] for m in modifiers) + "}"
            if modifiers else ""
        )

        for key in main_keys:
            code = _KEY_CODES.get(key)
            if code is not None:
                lines.append(f"key code {code}{mod_str}")
            elif len(key) == 1:
                safe = key.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'keystroke "{safe}"{mod_str}')
            # Unknown named keys are skipped silently

    if not lines:
        return False, "no keystrokes to send"

    # Activate the target app first so keystrokes land on the right window,
    # not whatever happened to have focus at execution time.
    activate_block = (
        f'tell application "{app_name}" to activate\n'
        if app_name else ""
    )
    script = (
        activate_block
        + 'tell application "System Events"\n'
        + "\n".join(lines)
        + "\nend tell"
    )
    return _run_osascript(script)


def read_ax_value(process_name: str, path: str) -> tuple[bool, str]:
    """Read a UI element value via System Events accessibility (no cua-driver).

    path examples:
      'value of static text 1 of window 1'   ← Calculator display
      'value of text field 1 of window 1'

    Returns (success, value_text).
    """
    script = (
        f'tell application "System Events"\n'
        f'  tell process "{process_name}"\n'
        f'    return {path}\n'
        f'  end tell\n'
        f'end tell'
    )
    return _run_osascript(script)
