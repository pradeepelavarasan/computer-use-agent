#!/usr/bin/env python3
"""Generate a generic session log (log.md) from a completed agent run.

Works for any query type: browser, computer, research, shopping, mixed.
Sections:
  1. User Goal
  2. Planner DAG
  3. Execution Journey  (one subsection per node, with screenshots)
  4. Final Result
  5. Performance Summary

Usage:
    python code/generate_custom_log.py --session s9-2026-06-19_12-40-33
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_label(node_id: str) -> str:
    return node_id.replace(":", "_")


def _extract_computer_layer_logs(log_dir: Path) -> dict[str, list[str]]:
    """Parse run.log and group computer layer messages by node ID.

    Returns {node_id: [lines]} where lines are [computer/...] and
    [vision/...] messages that appeared before each computer node's
    completion marker.  This captures AppleScript / hotkey / daemon
    activity from layers that escalated before Vision took over.
    """
    run_log = log_dir / "run.log"
    if not run_log.exists():
        return {}

    result: dict[str, list[str]] = {}
    pending: list[str] = []

    for line in run_log.read_text(encoding="utf-8", errors="replace").splitlines():
        # Node completion marker: "[n:2] computer           failed   (66.0s)"
        m = re.match(r"\[n:(\S+)\]\s+(\S+)\s+(complete|failed)", line)
        if m:
            node_id = f"n:{m.group(1)}"
            skill = m.group(2)
            if skill == "computer":
                result[node_id] = list(pending)
            # Clear pending for every node boundary
            pending = []
            continue

        # Collect any line that describes computer layer activity
        if (line.startswith("[computer/")
                or line.startswith("[vision/")
                or line.startswith("[vision] ")):
            pending.append(line)

    return result


def _truncate(text: str, max_chars: int = 1500) -> str:
    text = text.strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n*(truncated)*"
    return text


def _find_skill_run_dir(
    state_skill_dir: Path,
    started_at: float,
    skill_prefix: str,
) -> "Path | None":
    """Find the computer/browser run directory closest to started_at."""
    if not state_skill_dir.exists() or not started_at:
        return None
    run_dir = None
    best_diff = float("inf")
    for d in sorted(state_skill_dir.glob(f"{skill_prefix}_*")):
        try:
            ts = int(d.name.split("_")[1])
            diff = abs(ts - started_at)
            if diff < 30 and diff < best_diff:
                best_diff = diff
                run_dir = d
        except Exception:
            pass
    return run_dir


def _copy_screenshots(
    state_skill_dir: Path,
    started_at: float,
    layer_name: str,
    node_id: str,
    dest_dir: Path,
    skill_prefix: str,
) -> dict[int, str]:
    """Copy per-turn SCAN screenshots for browser/a11y layers.

    Returns {turn_number: relative_path} for embedding in markdown.
    Handles both old naming (turn_XX_raw.png) and new naming (turn_XX_scan.png).
    """
    mapping: dict[int, str] = {}
    run_dir = _find_skill_run_dir(state_skill_dir, started_at, skill_prefix)
    if not run_dir:
        return mapping

    layer_dir = run_dir / layer_name
    if not layer_dir.exists():
        return mapping

    # prefer annotated over raw; support both old and new naming conventions
    for glob_pat, turn_idx in [
        ("turn_*_scan_annotated.png", 1),  # new naming (VisionDriver rewrite)
        ("turn_*_annotated.png", 1),        # old naming
        ("turn_*_scan.png", 1),             # new naming raw
        ("turn_*_raw.png", 1),              # old naming raw
    ]:
        found = sorted(layer_dir.glob(glob_pat))
        if not found:
            continue
        for f in found:
            try:
                turn_num = int(f.name.split("_")[1])
                if turn_num in mapping:
                    continue  # already have a better one
                dest_name = f"{_safe_label(node_id)}_{layer_name}_turn_{turn_num:02d}_scan.png"
                shutil.copy(f, dest_dir / dest_name)
                mapping[turn_num] = f"screenshots/{dest_name}"
            except Exception as exc:
                print(f"Warning: could not copy screenshot {f}: {exc}")
        if mapping:
            break  # stop at first glob that matched

    return mapping


def _copy_vision_screenshots(
    vision_dir: Path,
    node_id: str,
    dest_dir: Path,
) -> dict[int, dict[str, "str | None"]]:
    """Copy both scan and verify screenshots for the Vision layer per turn.

    Returns {turn: {"scan": rel_path_or_None, "verify": rel_path_or_None}}.
    Handles both old naming (raw/annotated) and new naming (scan/verify_annotated).
    """
    if not vision_dir.exists():
        return {}

    result: dict[int, dict[str, "str | None"]] = {}

    # ── SCAN screenshots (before action) ─────────────────────────────────────
    for glob_pat in ("turn_*_scan_annotated.png", "turn_*_annotated.png",
                     "turn_*_scan.png", "turn_*_raw.png"):
        found = sorted(vision_dir.glob(glob_pat))
        if not found:
            continue
        for f in found:
            try:
                turn_num = int(f.name.split("_")[1])
                if turn_num in result and result[turn_num].get("scan"):
                    continue
                dest = f"{_safe_label(node_id)}_vision_t{turn_num:02d}_scan.png"
                shutil.copy(f, dest_dir / dest)
                result.setdefault(turn_num, {})["scan"] = f"screenshots/{dest}"
            except Exception as exc:
                print(f"Warning: {exc}")
        break  # use first glob that matched

    # ── VERIFY screenshots (after action) ────────────────────────────────────
    for glob_pat in ("turn_*_verify_annotated.png", "turn_*_verify.png"):
        found = sorted(vision_dir.glob(glob_pat))
        if not found:
            continue
        for f in found:
            try:
                turn_num = int(f.name.split("_")[1])
                if turn_num in result and result[turn_num].get("verify"):
                    continue
                dest = f"{_safe_label(node_id)}_vision_t{turn_num:02d}_verify.png"
                shutil.copy(f, dest_dir / dest)
                result.setdefault(turn_num, {})["verify"] = f"screenshots/{dest}"
            except Exception as exc:
                print(f"Warning: {exc}")
        break  # use first glob that matched

    # fill missing keys
    for turn_num in result:
        result[turn_num].setdefault("scan", None)
        result[turn_num].setdefault("verify", None)

    return result


def _render_turns(md: list, turns: list[dict], screenshots: dict[int, str]) -> None:
    """Render per-turn actions + screenshots into md."""
    for idx, turn in enumerate(turns):
        turn_num = turn.get("turn", idx + 1)
        actions_list = turn.get("actions", [])
        outcome = turn.get("outcome", "")

        md.append(f"##### Turn {turn_num}")
        if actions_list:
            md.append("**Actions:**")
            for action in actions_list:
                act_type = action.get("type") or action.get("tool") or "?"
                params = ", ".join(
                    f"{k}={v}" for k, v in action.items()
                    if k not in ("type", "tool", "thinking", "turn")
                )
                md.append(f"- `{act_type}({params})`")
            md.append("")
        if outcome:
            md.append(f"**Outcome:** `{outcome}`")
            md.append("")
        img = screenshots.get(turn_num)
        if img:
            md.append(f"![Turn {turn_num}]({img})")
            md.append("")


# ── node section renderers ─────────────────────────────────────────────────────

def _render_planner(md: list, node: dict) -> None:
    out = node.get("result", {}).get("output", {})
    rationale = out.get("rationale", "")
    planned_nodes = out.get("nodes", [])
    if rationale:
        md.append(f"**Rationale:** {rationale}")
        md.append("")
    if planned_nodes:
        md.append(f"**Planned {len(planned_nodes)} node(s):**")
        for n in planned_nodes:
            skill = n.get("skill", "?")
            label = n.get("metadata", {}).get("label", "")
            md.append(f"- `{skill}`" + (f" (`{label}`)" if label else ""))
        md.append("")


def _render_browser(
    md: list, node: dict,
    session_state_dir: Path, screenshots_dest: Path,
) -> None:
    out = node.get("result", {}).get("output", {})
    path = out.get("path", "unknown")
    url = out.get("url", "")
    goal = out.get("goal", "")
    content = out.get("content") or ""
    actions = out.get("actions") or []
    final_url = out.get("final_url") or url
    node_id = node.get("node_id", "browser")
    started_at = node.get("started_at", 0)

    if url:
        md.append(f"**URL:** {url}")
    if goal:
        md.append(f"**Goal:** {goal}")
    md.append(f"**Cascade path:** **{path.upper()}**")
    if final_url and final_url != url:
        md.append(f"**Final URL:** {final_url}")
    md.append("")

    screenshots = _copy_screenshots(
        session_state_dir / "browser", started_at, path, node_id,
        screenshots_dest, "browser",
    )

    if actions:
        md.append("**Journey:**")
        md.append("")
        _render_turns(md, actions, screenshots)
    elif screenshots:
        for turn_num, img_path in sorted(screenshots.items()):
            md.append(f"![Turn {turn_num}]({img_path})")
            md.append("")

    if content:
        md.append("**Extracted content:**")
        md.append("```")
        md.append(_truncate(content, 800))
        md.append("```")
        md.append("")


def _describe_vision_action(action_record: dict) -> str:
    """Human-readable one-liner for a Vision action record."""
    decision = action_record.get("decision", {})
    dtype = decision.get("type", "?")
    if dtype == "click":
        return f"click element {decision.get('element_index')}"
    if dtype == "click_bbox":
        return f"click bbox {decision.get('bbox')}"
    if dtype == "type_text":
        return f'type "{decision.get("value", "")}"'
    if dtype == "press_key":
        return f"press {decision.get('value', '')}"
    return dtype


def _render_computer(
    md: list, node: dict,
    session_state_dir: Path, screenshots_dest: Path,
    layer_logs: "dict[str, list[str]] | None" = None,
) -> None:
    out = node.get("result", {}).get("output", {})
    path = out.get("path", "unknown")
    app = out.get("app", "")
    goal = out.get("goal", "")
    content = out.get("content") or ""
    actions = out.get("actions") or []
    error_code = out.get("error_code") or (node.get("result") or {}).get("error_code")
    node_id = node.get("node_id", "computer")
    started_at = node.get("started_at", 0)
    status = node.get("status", "?")

    if app:
        md.append(f"**App:** {app}")
    if goal:
        md.append(f"**Goal:** {goal}")
    status_icon = "✓" if status == "complete" else "✗"
    md.append(f"**Cascade path:** **{path.upper()}** {status_icon}")
    if error_code:
        md.append(f"**Error:** `{error_code}`")
    md.append("")

    # ── Layer cascade trace (from run.log) ────────────────────────────────────
    # Show what every layer did — AppleScript, hotkeys, daemon messages — so
    # the reader understands why earlier layers escalated before Vision ran.
    node_layer_lines = (layer_logs or {}).get(node_id, [])
    if node_layer_lines:
        # Group into per-layer buckets for a cleaner presentation.
        # NOTE: [computer/daemon] lines are app launch / window resolution /
        # foregrounding — they apply to ALL layers (pre-flight), so they are
        # their own bucket, NOT Layer 2. Only [computer/Layer2] is AppleScript.
        daemon_lines = [l for l in node_layer_lines if "[computer/daemon]" in l]
        layer2 = [l for l in node_layer_lines if "[computer/Layer2]" in l]
        layer3 = [l for l in node_layer_lines if "[computer/Layer3]" in l]
        vision_trace = [l for l in node_layer_lines if "[vision/" in l or "[vision] " in l]

        if daemon_lines:
            md.append("**App launch / window resolution:**")
            md.append("```")
            for line in daemon_lines:
                md.append(line.split("] ", 1)[-1])  # strip the [computer/daemon] prefix
            md.append("```")
            md.append("")

        if layer2:
            md.append("**Layer 2 — AppleScript:**")
            md.append("```")
            for line in layer2:
                md.append(line.split("] ", 1)[-1])  # strip the [computer/Layer2] prefix
            md.append("```")
            md.append("")

        if layer3:
            md.append("**Layer 3 — Hotkeys:**")
            md.append("```")
            for line in layer3:
                md.append(line.split("] ", 1)[-1])
            md.append("```")
            md.append("")

        if vision_trace and path not in ("vision",):
            # Only show vision trace summary when vision isn't the primary path
            # (if vision IS the primary path, the screenshot section below covers it)
            md.append("**Layer 5 — Vision trace:**")
            md.append("```")
            for line in vision_trace:
                md.append(line.split("] ", 1)[-1] if "] " in line else line)
            md.append("```")
            md.append("")

    # ── Vision layer: full scan → act → verify chart ──────────────────────────
    if path == "vision":
        run_dir = _find_skill_run_dir(
            session_state_dir / "computer", started_at, "computer"
        )
        vision_dir = (run_dir / "vision") if run_dir else None
        turn_shots = (
            _copy_vision_screenshots(vision_dir, node_id, screenshots_dest)
            if vision_dir and vision_dir.exists()
            else {}
        )

        # `actions` from VisionDriver is a list of StepRecord.__dict__
        # Each: {"turn": N, "actions": [action_record], "outcome": "..."}
        step_records = [
            a for a in actions
            if isinstance(a, dict) and "outcome" in a
        ]

        if step_records or turn_shots:
            md.append("**Vision Session — Scan → Act → Verify per turn:**")
            md.append("")

            all_turns = sorted(
                set(list(turn_shots.keys()) + [s.get("turn", i) for i, s in enumerate(step_records)])
            )

            for turn_num in all_turns:
                shots = turn_shots.get(turn_num, {})
                scan_img = shots.get("scan")
                verify_img = shots.get("verify")

                # find matching step record
                step = next(
                    (s for s in step_records if s.get("turn") == turn_num), None
                )
                inner_actions = (step or {}).get("actions", [])
                outcome = (step or {}).get("outcome", "")

                md.append(f"##### Turn {turn_num + 1}")

                if scan_img:
                    md.append("**Before (Scan):**")
                    md.append(f"![Scan T{turn_num}]({scan_img})")
                    md.append("")

                if inner_actions:
                    descs = [_describe_vision_action(a) for a in inner_actions]
                    md.append(f"**Action:** {' | '.join(descs)}")
                    md.append("")

                if verify_img:
                    md.append("**After (Verify):**")
                    md.append(f"![Verify T{turn_num}]({verify_img})")
                    md.append("")

                if outcome:
                    if outcome.startswith("goal_complete") or outcome == "done":
                        icon = "✅"
                    elif "confirmed" in outcome:
                        icon = "✓"
                    elif "failed" in outcome or "error" in outcome or "not confirmed" in outcome:
                        icon = "✗"
                    else:
                        icon = "→"
                    md.append(f"**Verification:** {icon} `{outcome}`")
                    md.append("")

        if content:
            md.append(f"**Final result:** {_truncate(content, 400)}")
            md.append("")
        return

    # ── A11y layer: show AX-based actions ────────────────────────────────────
    if path == "a11y":
        step_records = [
            a for a in actions
            if isinstance(a, dict) and "outcome" in a
        ]
        if step_records:
            md.append("**AX Tree Session:**")
            md.append("")
            for step in step_records:
                turn_num = step.get("turn", 0)
                inner = step.get("actions", [])
                outcome = step.get("outcome", "")
                parts = [f"Turn {turn_num + 1}:"]
                if inner:
                    for a in inner:
                        tool = a.get("tool", "?")
                        idx = a.get("element_index")
                        val = a.get("value", "")
                        parts.append(
                            f"`{tool}`"
                            + (f" el={idx}" if idx is not None else "")
                            + (f' "{val}"' if val else "")
                        )
                if outcome:
                    parts.append(f"→ `{outcome}`")
                md.append("- " + " ".join(parts))
                md.append("")
        if content:
            md.append(f"**Result:** {_truncate(content, 400)}")
            md.append("")
        return

    # ── Hotkeys / AppleScript layers: flat keystroke list ────────────────────
    if actions:
        md.append("**Keystrokes / Actions:**")
        for a in actions:
            keys = a.get("keys")
            text = a.get("text")
            if keys:
                md.append(f"- `{'+'.join(keys)}`")
            elif text:
                md.append(f"- type `{text}`")
            else:
                # might be a StepRecord if generated by older code
                outcome = a.get("outcome")
                if outcome:
                    md.append(f"- `{outcome}`")
                else:
                    md.append(f"- `{json.dumps(a)}`")
        md.append("")

    # screenshots for non-vision layers (a11y raw saves .txt, hotkeys saves nothing)
    screenshots = _copy_screenshots(
        session_state_dir / "computer", started_at, path, node_id,
        screenshots_dest, "computer",
    )
    for turn_num, img_path in sorted(screenshots.items()):
        md.append(f"![Turn {turn_num} ({path})]({img_path})")
        md.append("")

    if content:
        md.append(f"**Result:** {_truncate(content, 400)}")
        md.append("")


def _render_generic(md: list, node: dict) -> None:
    """Fallback renderer for researcher, distiller, summariser, critic, etc."""
    out = node.get("result", {}).get("output", {})
    error = node.get("result", {}).get("error")

    if error:
        md.append(f"**Error:** {error}")
        md.append("")
        return

    # Pull the most informative text field
    for key in ("final_answer", "answer", "summary", "content", "text", "result"):
        val = out.get(key)
        if val and isinstance(val, str) and val.strip():
            md.append("**Output:**")
            md.append(_truncate(val, 600))
            md.append("")
            return

    # Fallback: dump the whole output dict (truncated)
    if out:
        md.append("**Output (raw):**")
        md.append("```json")
        md.append(_truncate(json.dumps(out, indent=2, ensure_ascii=False), 600))
        md.append("```")
        md.append("")


# ── performance summary (reused from original) ────────────────────────────────

def _render_performance(md: list, nodes: list[dict], log_dir: Path, session_id: str, section: int = 5) -> None:
    md.append(f"## {section}. Performance Summary")
    md.append("")
    md.append("| Node | Skill | Status | Provider | Model | Duration | Tokens In | Tokens Out |")
    md.append("|---|---|---|---|---|---|---|---|")

    # Parse WORKER lines from run.log for real token + latency data
    date_str = ""
    try:
        date_str = session_id.split("-", 1)[1].split("_")[0]  # "2026-06-19"
    except Exception:
        pass

    worker_calls: list[dict] = []
    run_log = log_dir / "run.log"
    if run_log.exists():
        for line in run_log.read_text(encoding="utf-8").splitlines():
            m = re.search(
                r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*WORKER\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|"
                r"\s*OK\s*\|\s*in=(\d+)\s*out=(\d+)\s*\((\d+)ms\)",
                line,
            )
            if m and date_str:
                try:
                    dt = datetime.datetime.strptime(
                        f"{date_str} {m.group(1)}:{m.group(2)}:{m.group(3)}",
                        "%Y-%m-%d %H:%M:%S",
                    )
                    worker_calls.append({
                        "epoch": dt.timestamp(),
                        "provider": m.group(4),
                        "model": m.group(5),
                        "tokens_in": int(m.group(6)),
                        "tokens_out": int(m.group(7)),
                        "latency_ms": int(m.group(8)),
                    })
                except Exception:
                    pass

    total_lat = total_in = total_out = 0

    for node in sorted(nodes, key=lambda n: n.get('started_at') or float('inf')):
        nid = node.get("node_id", "?")
        skill = node.get("skill", "?")
        status = node.get("status", "?")
        res = node.get("result") or {}
        started_at = node.get("started_at") or 0
        completed_at = node.get("completed_at") or 0
        lat_ms = (res.get("elapsed_s") or 0) * 1000

        node_calls = [
            c for c in worker_calls
            if started_at and completed_at
            and (started_at - 2) <= c["epoch"] <= (completed_at + 2)
        ]
        if node_calls:
            t_in = sum(c["tokens_in"] for c in node_calls)
            t_out = sum(c["tokens_out"] for c in node_calls)
            provider = ", ".join(sorted({c["provider"] for c in node_calls}))
            model = ", ".join(sorted({c["model"] for c in node_calls}))
            if lat_ms == 0:
                lat_ms = sum(c["latency_ms"] for c in node_calls)
        else:
            t_in = t_out = 0
            provider = res.get("provider", "—")
            model = "—"

        total_lat += lat_ms
        total_in += t_in
        total_out += t_out

        status_icon = "✓" if status == "complete" else ("✗" if status == "failed" else "⟳")
        md.append(
            f"| `{nid}` | {skill} | {status_icon} {status} | {provider} | {model} "
            f"| {lat_ms:,.0f} ms | {t_in:,} | {t_out:,} |"
        )

    md.append(
        f"| **TOTAL** | | | | | **{total_lat:,.0f} ms** | **{total_in:,}** | **{total_out:,}** |"
    )
    md.append("")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate log.md for a session run.")
    parser.add_argument("--session", "-s", required=True, help="Session ID")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    session_id = args.session
    log_dir = project_root / "logs" / session_id

    if not log_dir.exists():
        print(f"Error: log directory not found: {log_dir}")
        return

    # ── load data ─────────────────────────────────────────────────────────────
    user_query = (log_dir / "query.txt").read_text(encoding="utf-8").strip() \
        if (log_dir / "query.txt").exists() else "(unknown)"

    nodes: list[dict] = []
    nodes_dir = log_dir / "nodes"
    if nodes_dir.exists():
        for f in sorted(nodes_dir.glob("*.json")):
            try:
                nodes.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:
                print(f"Warning: could not parse {f.name}: {exc}")

    screenshots_dest = log_dir / "screenshots"
    screenshots_dest.mkdir(parents=True, exist_ok=True)

    session_state_dir = project_root / "code" / "state" / "sessions" / session_id

    # ── markdown ──────────────────────────────────────────────────────────────
    md: list[str] = []
    md.append(f"# Session Log — {session_id}")
    md.append("")

    # 1. User goal
    md.append("## 1. User Goal")
    md.append("")
    md.append(f"> {user_query}")
    md.append("")

    # 2. Planner DAG
    md.append("## 2. Planner DAG")
    md.append("")
    if (log_dir / "graph.html").exists():
        md.append("*[Open interactive DAG](graph.html)*")
    if (log_dir / "planner_dag.png").exists():
        md.append("![Planner DAG](planner_dag.png)")
    md.append("")

    # 3. Compact run overview — all nodes in one table
    md.append("## 3. Run Overview")
    md.append("")
    md.append("| Node | Skill | Status | Duration | Notes |")
    md.append("|---|---|---|---|---|")
    overview_nodes = sorted(nodes, key=lambda n: n.get('started_at') or float('inf'))
    for node in overview_nodes:
        nid = node.get("node_id", "?")
        skill = node.get("skill", "?")
        status = node.get("status", "?")
        res = node.get("result") or {}
        elapsed = res.get("elapsed_s") or 0
        status_icon = "✓" if status == "complete" else ("✗" if status == "failed" else "⟳")
        # One-line note per skill type
        out = res.get("output") or {}
        if skill == "browser":
            note = f"path={out.get('path','?')}"
        elif skill == "computer":
            note = f"app={out.get('app','?')}  path={out.get('path','?')}"
        elif skill == "planner":
            note = out.get("rationale", "")[:80]
        elif skill == "formatter":
            note = "→ final answer"
        else:
            note = ""
        md.append(f"| `{nid}` | {skill} | {status_icon} {status} | {elapsed:.1f}s | {note} |")
    md.append("")

    # 4. Computer node sections — one per computer node, with screenshots
    computer_nodes = [n for n in nodes if n.get("skill") == "computer"]
    if computer_nodes:
        md.append("## 4. Computer Sessions")
        md.append("")
        layer_logs = _extract_computer_layer_logs(log_dir)
        for node in computer_nodes:
            _render_computer(md, node, session_state_dir, screenshots_dest, layer_logs)

    # Final result + performance — section numbers shift if there were computer nodes
    next_sec = 5 if computer_nodes else 4

    md.append(f"## {next_sec}. Final Result")
    md.append("")
    final_answer = ""
    for node in reversed(nodes):
        if node.get("skill") == "formatter":
            final_answer = (node.get("result") or {}).get("output", {}).get("final_answer", "")
            break
    md.append(final_answer if final_answer else "*(no formatter output)*")
    md.append("")

    _render_performance(md, nodes, log_dir, session_id, section=next_sec + 1)

    # ── write ─────────────────────────────────────────────────────────────────
    output_path = log_dir / "log.md"
    output_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[log] Session log written to {output_path}")


if __name__ == "__main__":
    main()
