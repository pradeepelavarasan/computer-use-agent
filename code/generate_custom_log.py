#!/usr/bin/env python3
"""
Generate a customized Markdown log for a Session ID.
Usage:
  python code/generate_custom_log.py --session s9-2026-06-13_00-29-52
"""

import argparse
import json
import os
import shutil
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Compile a customized log markdown from a session run.")
    parser.add_argument("--session", "-s", required=True, help="Session ID (e.g. s9-2026-06-13_00-29-52)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    session_id = args.session

    log_dir = project_root / "logs" / session_id
    if not log_dir.exists():
        print(f"Error: Log directory for session {session_id} does not exist at {log_dir}")
        return

    # 1. Load Original User Goal
    query_file = log_dir / "query.txt"
    user_query = ""
    if query_file.exists():
        user_query = query_file.read_text(encoding="utf-8").strip()
    else:
        print(f"Warning: query.txt not found in {log_dir}")

    # Load node files
    nodes_dir = log_dir / "nodes"
    nodes = []
    if nodes_dir.exists():
        for f in sorted(nodes_dir.glob("*.json")):
            try:
                nodes.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                print(f"Warning: Could not parse node file {f.name}: {e}")

    # Extract relevant nodes
    bsearch_node = None
    shortlister_node = None
    rec_node = None
    detail_browser_nodes = []

    for node in nodes:
        skill = node.get("skill")
        result_out = node.get("result", {}).get("output", {})
        
        # Check for browser search node
        if skill == "browser":
            if "s?k=" in result_out.get("url", "") or "s?k=" in node.get("prompt_sent", ""):
                bsearch_node = node
            else:
                detail_browser_nodes.append(node)
        elif skill == "product_shortlister":
            shortlister_node = node
        elif skill == "product_recommendation":
            rec_node = node

    # Helper function to copy screenshots for a specific browser run matching started_at timestamp
    def copy_node_screenshots(session_browser_dir, started_at, path_name, node_id, dest_dir):
        screenshot_mappings = {}
        if not session_browser_dir.exists() or not started_at or not path_name:
            return screenshot_mappings
        
        # Find matching browser_* dir
        run_dir = None
        best_diff = float('inf')
        for d in sorted(session_browser_dir.glob("browser_*")):
            try:
                ts = int(d.name.split("_")[1])
                diff = abs(ts - started_at)
                if diff < 15 and diff < best_diff:
                    best_diff = diff
                    run_dir = d
            except Exception:
                pass
                
        if run_dir:
            path_dir = run_dir / path_name
            if path_dir.exists():
                for f in sorted(path_dir.glob("turn_*_raw.png")):
                    try:
                        turn_num = int(f.name.split("_")[1])
                        dest_name = f"{node_id.replace(':', '_')}_turn_{turn_num:02d}_raw.png"
                        shutil.copy(f, dest_dir / dest_name)
                        screenshot_mappings[turn_num] = f"screenshots/{dest_name}"
                    except Exception as e:
                        print(f"Warning: Failed to copy screenshot {f.name}: {e}")
        return screenshot_mappings

    # 2. Browser path chosen
    browser_path = "Unknown"
    browser_turns = []
    if bsearch_node:
        b_output = bsearch_node.get("result", {}).get("output", {})
        browser_path = b_output.get("path", "Unknown")
        browser_turns = bsearch_node.get("result", {}).get("actions", []) or b_output.get("actions", [])

    # 3. Copy Screenshots to public directory
    screenshots_dest = log_dir / "screenshots"
    screenshots_dest.mkdir(parents=True, exist_ok=True)

    session_state_browser_dir = project_root / "code" / "state" / "sessions" / session_id / "browser"
    screenshot_mappings = {}
    if bsearch_node:
        b_started = bsearch_node.get("started_at", 0)
        b_id = bsearch_node.get("node_id", "bsearch")
        screenshot_mappings = copy_node_screenshots(
            session_state_browser_dir, b_started, browser_path, b_id, screenshots_dest
        )

    # Build Markdown Content
    md = []
    md.append(f"# Session Log: {session_id}")
    md.append("")
    md.append("## 1. Original User Goal")
    md.append(f"> {user_query}")
    md.append("")
    md.append("## 2. Planner DAG")
    md.append("![Planner DAG](planner_dag.png)")
    md.append("")
    md.append("## 3. Browser Path Chosen")
    md.append(f"The Browser cascade chose the **{browser_path.upper()}** interaction path.")
    md.append("")
    md.append("## 4. Browser Actions & Screenshots")
    md.append("")

    if not browser_turns:
        md.append("*No browser actions logged.*")
    else:
        for idx, turn in enumerate(browser_turns):
            turn_num = turn.get("turn", idx + 1)
            thinking = turn.get("thinking", "")
            actions_list = turn.get("actions", [])
            outcome = turn.get("outcome", "")

            md.append(f"### Turn {turn_num}")
            md.append(f"**Thinking:** {thinking}")
            md.append("")
            md.append("**Actions:**")
            for action in actions_list:
                act_type = action.get("type")
                params = ", ".join(f"{k}={v}" for k, v in action.items() if k not in ("type", "thinking"))
                md.append(f"- `{act_type}({params})`")
            md.append("")
            md.append(f"**Outcome:** `{outcome}`")
            md.append("")

            # Embed turn screenshot if exists
            img_path = screenshot_mappings.get(turn_num)
            if img_path:
                md.append(f"![Turn {turn_num} Page State]({img_path})")
                md.append("")

    # 5. Extracted Data
    md.append("## 5. Extracted Shortlist Data")
    md.append("")
    if shortlister_node:
        s_output = shortlister_node.get("result", {}).get("output", {})
        md.append("```json")
        md.append(json.dumps(s_output, indent=2, ensure_ascii=False))
        md.append("```")
    else:
        md.append("*No shortlister node output found.*")
    md.append("")

    # 6. Browser path chosen for product analyst
    md.append("## 6. Browser path chosen for product analyst")
    md.append("")
    if not detail_browser_nodes:
        md.append("*No product analyst browser runs logged.*")
    else:
        for db_node in sorted(detail_browser_nodes, key=lambda n: n.get("node_id", "")):
            db_id = db_node.get("node_id")
            db_output = db_node.get("result", {}).get("output", {})
            db_url = db_output.get("url", "")
            db_path = db_output.get("path", "Unknown")
            db_turns = db_node.get("result", {}).get("actions", []) or db_output.get("actions", [])
            db_started = db_node.get("started_at", 0)
            
            # Find ASIN from URL
            import re
            asin_match = re.search(r"/dp/([A-Z0-9]{10})", db_url)
            asin_str = f" (ASIN: {asin_match.group(1)})" if asin_match else ""
            
            md.append(f"### Product detail lookup for [{db_url}]({db_url}){asin_str}")
            md.append(f"- **Node ID:** `{db_id}`")
            md.append(f"- **Interaction Path:** **{db_path.upper()}**")
            md.append("")
            
            # Copy and map screenshots
            db_screenshot_mappings = copy_node_screenshots(
                session_state_browser_dir, db_started, db_path, db_id, screenshots_dest
            )
            
            if db_turns:
                md.append("#### Actions taken:")
                for idx, turn in enumerate(db_turns):
                    turn_num = turn.get("turn", idx + 1)
                    thinking = turn.get("thinking", "")
                    actions_list = turn.get("actions", [])
                    outcome = turn.get("outcome", "")

                    md.append(f"##### Turn {turn_num}")
                    md.append(f"**Thinking:** {thinking}")
                    md.append("")
                    md.append("**Actions:**")
                    for action in actions_list:
                        act_type = action.get("type")
                        params = ", ".join(f"{k}={v}" for k, v in action.items() if k not in ("type", "thinking"))
                        md.append(f"- `{act_type}({params})`")
                    md.append("")
                    md.append(f"**Outcome:** `{outcome}`")
                    md.append("")

                    # Embed turn screenshot if exists
                    img_path = db_screenshot_mappings.get(turn_num)
                    if img_path:
                        md.append(f"![Turn {turn_num} Page State]({img_path})")
                        md.append("")
            
            # Extracted content summary
            content = db_output.get("content", "")
            if content:
                # Clean up content for markdown representation (e.g. truncate long text)
                truncated_content = content[:1500].strip()
                if len(content) > 1500:
                    truncated_content += "\n\n... (truncated for length)"
                
                md.append("#### Extracted Content:")
                md.append("```text")
                md.append(truncated_content)
                md.append("```")
                md.append("")
            else:
                md.append("*No content extracted or extraction failed.*")
                md.append("")
    md.append("")

    # 7. Final Comparison Table
    md.append("## 7. Final Recommendation Matrix")
    md.append("")
    md.append("![Final Matrix](final_matrix.png)")
    md.append("")

    if rec_node:
        rec_data = rec_node.get("result", {}).get("output", {})
        products = rec_data.get("products", [])
        analysis = rec_data.get("analysis", {})
        priorities = rec_data.get("task", {}).get("priorities", [])

        # Build table headers
        headers = ["Product", "Price", "Rating"] + priorities
        col_aligns = ["---"] * len(headers)
        
        table_rows = []
        for prod in products:
            p_id = prod.get("id")
            p_title = prod.get("title", "")
            # Truncate title for markdown table readability
            short_title = p_title[:50] + "..." if len(p_title) > 50 else p_title
            
            p_price = prod.get("price", "N/A")
            p_rating = f"{prod.get('rating', 'N/A')} ({prod.get('reviews_count', 0):,} reviews)"

            # Get evaluations matching each priority
            prod_analysis = next((p for p in analysis.get("products", []) if p.get("product_id") == p_id), {})
            evals = prod_analysis.get("evaluations", {})

            row = [
                f"**[{short_title}]({prod.get('url', '#')})**",
                p_price,
                p_rating
            ]

            for priority in priorities:
                p_eval = evals.get(priority, {})
                if isinstance(p_eval, dict):
                    score = (p_eval.get("score") or "neutral").upper()
                    text = p_eval.get("analysis") or ""
                    row.append(f"**[{score}]** {text}")
                else:
                    row.append(str(p_eval))

            table_rows.append(" | ".join(row))

        md.append(" | ".join(headers))
        md.append(" | ".join(col_aligns))
        for row in table_rows:
            md.append(row)
        md.append("")
        
        md.append(f"**Overall Agent Recommendation Summary:**")
        md.append(f"> {analysis.get('overall_agent_summary', '')}")
    else:
        md.append("*No recommendation node output found.*")
    md.append("")

    # 8. Turn Count and Cost Summary
    md.append("## 8. Cost & Performance Summary")
    md.append("")
    md.append("| Node ID | Skill | Provider | Model | Latency | Tokens In | Tokens Out |")
    md.append("|---|---|---|---|---|---|---|")
    
    # Parse run.log for actual database calls and token usage using timestamps
    import datetime
    import re
    date_str = session_id.split("-", 1)[1].split("_")[0] # "2026-06-13"
    worker_calls = []
    run_log_file = log_dir / "run.log"
    if run_log_file.exists():
        for line in run_log_file.read_text(encoding="utf-8").splitlines():
            # Match lines like: [01:33:26] WORKER       | gemini_lite_2  | gemini-3.1-flash-lite          | OK    | in=4765 out=743 (2279ms)
            m = re.search(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*WORKER\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*OK\s*\|\s*in=(\d+)\s*out=(\d+)\s*\((\d+)ms\)", line)
            if m:
                hh, mm, ss = m.group(1), m.group(2), m.group(3)
                provider = m.group(4)
                model = m.group(5)
                t_in = int(m.group(6))
                t_out = int(m.group(7))
                lat = int(m.group(8))
                
                try:
                    dt = datetime.datetime.strptime(f"{date_str} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
                    epoch = dt.timestamp()
                    worker_calls.append({
                        "epoch": epoch,
                        "provider": provider,
                        "model": model,
                        "tokens_in": t_in,
                        "tokens_out": t_out,
                        "latency": lat
                    })
                except Exception:
                    pass

    total_latency_ms = 0
    total_tokens_in = 0
    total_tokens_out = 0

    for node in nodes:
        nid = node.get("node_id")
        skill = node.get("skill")
        res = node.get("result", {})
        
        provider = res.get("provider", "N/A")
        model = res.get("model", "N/A")
        
        latency = res.get("elapsed_s", 0) * 1000
        if latency == 0:
            latency = res.get("latency_ms", 0)
        
        # Match worker calls to this node by timestamp (with a 2-second buffer)
        started_at = node.get("started_at", 0)
        completed_at = node.get("completed_at", 0)
        
        node_calls = []
        if started_at and completed_at:
            node_calls = [
                c for c in worker_calls 
                if (started_at - 2) <= c["epoch"] <= (completed_at + 2)
            ]
            
        if node_calls:
            t_in = sum(c["tokens_in"] for c in node_calls)
            t_out = sum(c["tokens_out"] for c in node_calls)
            # Use real provider/model if available
            provider = ", ".join(sorted(list(set(c["provider"] for c in node_calls))))
            model = ", ".join(sorted(list(set(c["model"] for c in node_calls))))
        else:
            t_in = res.get("tokens_in", 0) or res.get("input_tokens", 0) or 0
            t_out = res.get("tokens_out", 0) or res.get("output_tokens", 0) or 0

        total_latency_ms += latency
        total_tokens_in += t_in
        total_tokens_out += t_out

        md.append(f"| {nid} | {skill} | {provider} | {model} | {latency:,.0f}ms | {t_in:,} | {t_out:,} |")

    md.append(f"| **TOTAL** | | | | **{total_latency_ms:,.0f}ms** | **{total_tokens_in:,}** | **{total_tokens_out:,}** |")
    md.append("")

    # Save Markdown file
    output_path = log_dir / "customized_log.md"
    output_path.write_text("\n".join(md), encoding="utf-8")
    print(f"Success: Customized log compiled and saved to {output_path}")

if __name__ == "__main__":
    main()
