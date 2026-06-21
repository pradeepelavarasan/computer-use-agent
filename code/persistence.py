"""Session 8 on-disk persistence for the growing graph.

Lives in its own file because flow.py needs to stay under 350 lines.
The two surfaces:

  - SessionStore: per-session directory under state/sessions/<sid>/.
    Owns reading and writing the graph pickle and the per-node JSON
    files. Atomic-write semantics (write to tmp, rename) so a SIGKILL
    mid-write does not corrupt the last successful snapshot.
  - rebuild_graph_state(): given a populated SessionStore, returns the
    list of NodeState records sorted by completion time so replay.py
    can walk them in order.

The Graph itself (the NetworkX wrapping) lives in flow.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import networkx as nx

from schemas import AgentResult, NodeState

SESSIONS_ROOT = Path(__file__).resolve().parent.parent / "logs"


class SessionLoadError(RuntimeError):
    """Raised when a persisted session cannot be safely loaded.

    Examples: a NodeState file that no longer matches the schema, a
    `_result_typed` payload that cannot round-trip back into an
    AgentResult. We fail loud here rather than silently degrade — the
    Executor's downstream code does `isinstance(..., AgentResult)`
    checks, and stashing a dict where it expects a Pydantic model is
    exactly the silent-degradation pattern review round-3 #4 flagged."""


def _atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode) as f:
        f.write(data)
    os.replace(tmp, path)


class SessionStore:
    """One on-disk session. Layout:

        state/sessions/<sid>/
            graph.pkl              # NetworkX DiGraph pickle
            query.txt              # the user's verbatim query
            nodes/
                n_001.json         # NodeState for the n:1 node, etc.
                n_002.json
                ...
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.dir = SESSIONS_ROOT / session_id
        self.nodes_dir = self.dir / "nodes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    @property
    def query_path(self) -> Path:
        return self.dir / "query.txt"

    @property
    def graph_path(self) -> Path:
        # P1 #6: graph is persisted as JSON via nx.node_link_data so the file
        # is `cat`-able by students and the format survives a Python upgrade.
        return self.dir / "graph.json"

    @property
    def _legacy_graph_path(self) -> Path:
        # Older sessions wrote pickle; the loader tolerates that for resume
        # on pre-fix sessions but the writer always emits JSON now.
        return self.dir / "graph.pkl"

    def write_query(self, query: str) -> None:
        _atomic_write(self.query_path, query)

    def read_query(self) -> str:
        if not self.query_path.exists():
            return ""
        return self.query_path.read_text()

    def write_graph(self, graph_obj: nx.DiGraph) -> None:
        """Serialise the DiGraph to JSON via nx.node_link_data. Per-node
        `result` is an AgentResult (Pydantic) — dump it to a dict so the
        JSON encoder is happy. Reviving on read restores the Pydantic shape.
        """
        # node_link_data accepts arbitrary node-attr dicts; we just need
        # every value to be JSON-serialisable.
        h = nx.DiGraph()
        for n, d in graph_obj.nodes(data=True):
            attrs = dict(d)
            if isinstance(attrs.get("result"), AgentResult):
                attrs["result"] = attrs["result"].model_dump(mode="json")
                attrs["_result_typed"] = True
            h.add_node(n, **attrs)
        for u, v, d in graph_obj.edges(data=True):
            h.add_edge(u, v, **d)
        payload = nx.node_link_data(h, edges="edges")
        _atomic_write(self.graph_path, json.dumps(payload, indent=2, default=str))
        try:
            self._write_graph_html(payload)
        except Exception as e:
            import sys
            print(f"[persistence] WARNING: failed to write graph.html: {e}", file=sys.stderr)

    def _write_graph_html(self, payload: dict) -> None:
        query = self.read_query() or "Unknown Query"
        skill_colors = {
            "planner": {"background": "rgba(15, 23, 42, 0.8)", "border": "#3b82f6", "color": "#ffffff"},
            "researcher": {"background": "rgba(15, 23, 42, 0.8)", "border": "#10b981", "color": "#ffffff"},
            "formatter": {"background": "rgba(15, 23, 42, 0.8)", "border": "#8b5cf6", "color": "#ffffff"},
            "coder": {"background": "rgba(15, 23, 42, 0.8)", "border": "#f59e0b", "color": "#ffffff"},
            "sandbox_executor": {"background": "rgba(15, 23, 42, 0.8)", "border": "#6366f1", "color": "#ffffff"},
            "critic": {"background": "rgba(15, 23, 42, 0.8)", "border": "#ef4444", "color": "#ffffff"},
            "retriever": {"background": "rgba(15, 23, 42, 0.8)", "border": "#06b6d4", "color": "#ffffff"},
            "summariser": {"background": "rgba(15, 23, 42, 0.8)", "border": "#ec4899", "color": "#ffffff"},
            "distiller": {"background": "rgba(15, 23, 42, 0.8)", "border": "#14b8a6", "color": "#ffffff"},
            "browser": {"background": "rgba(15, 23, 42, 0.8)", "border": "#3b82f6", "color": "#ffffff"},
            # Shopping pipeline skills
            "product_shortlister":   {"background": "rgba(15, 23, 42, 0.8)", "border": "#f97316", "color": "#ffffff"},
            "product_analyst":       {"background": "rgba(15, 23, 42, 0.8)", "border": "#f59e0b", "color": "#ffffff"},
            "product_recommendation":{"background": "rgba(15, 23, 42, 0.8)", "border": "#10b981", "color": "#ffffff"},
        }
        default_colors = {"background": "rgba(15, 23, 42, 0.8)", "border": "#64748b", "color": "#ffffff"}

        vis_nodes = []
        for node in payload.get("nodes", []):
            nid = node.get("id")
            skill = node.get("skill")
            status = node.get("status")
            colors = skill_colors.get(skill, default_colors)
            label = f"ID: {nid}\nSkill: {skill}\nStatus: {status}"

            # Prepare structured details
            details = {
                "id": nid,
                "skill": skill,
                "status": status,
                "inputs": node.get("inputs") or [],
                "metadata": node.get("metadata") or {},
                "result": node.get("result") or {},
                "prompt_sent": node.get("prompt_sent") or ""
            }
            node_file = self._node_path(nid)
            if node_file.exists():
                try:
                    with open(node_file, "r", encoding="utf-8") as nf:
                        ndata = json.load(nf)
                        details["inputs"] = ndata.get("inputs") or details["inputs"]
                        details["metadata"] = ndata.get("metadata") or details["metadata"]
                        details["result"] = ndata.get("result") or details["result"]
                        details["prompt_sent"] = ndata.get("prompt_sent") or details["prompt_sent"]
                        if ndata.get("status"):
                            details["status"] = ndata["status"]
                except Exception:
                    pass

            vis_nodes.append({
                "id": nid,
                "label": label,
                "color": colors,
                "shape": "box",
                "details": details
            })

        vis_edges = []
        for edge in payload.get("edges", []):
            vis_edges.append({
                "from": edge.get("source"),
                "to": edge.get("target")
            })

        legend_html = ""
        for skill, colors in skill_colors.items():
            legend_html += f"""
            <div class="legend-item">
                <div class="legend-color" style="background-color: {colors['background']}; border-color: {colors['border']};"></div>
                <span>{skill}</span>
            </div>"""

        html_template = """<!DOCTYPE html>
<html>
<head>
    <title>DAG Visualization for Browser Agent: {session_id}</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style type="text/css">
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
        
        body {{
            background: radial-gradient(circle at 50% 0%, #1a1c2d 0%, #08090f 100%);
            color: #f1f5f9;
            font-family: 'Outfit', sans-serif;
            margin: 0;
            padding: 24px;
            display: flex;
            flex-direction: column;
            height: 100vh;
            box-sizing: border-box;
            overflow: hidden;
            position: relative;
        }}
        
        /* Glowing background decorative blobs */
        body::before {{
            content: '';
            position: absolute;
            width: 400px;
            height: 400px;
            background: radial-gradient(circle, rgba(99, 102, 241, 0.15) 0%, rgba(99, 102, 241, 0) 70%);
            top: -100px;
            right: -100px;
            z-index: 0;
            pointer-events: none;
        }}
        body::after {{
            content: '';
            position: absolute;
            width: 500px;
            height: 500px;
            background: radial-gradient(circle, rgba(139, 92, 246, 0.1) 0%, rgba(139, 92, 246, 0) 70%);
            bottom: -150px;
            left: -150px;
            z-index: 0;
            pointer-events: none;
        }}
        
        h2 {{
            margin-top: 0;
            font-weight: 600;
            font-size: 24px;
            letter-spacing: -0.5px;
            color: #ffffff;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 16px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 12px;
            z-index: 1;
        }}
        h2::before {{
            content: '';
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #8b5cf6;
            box-shadow: 0 0 12px #8b5cf6;
        }}
        .main-container {{
            display: flex;
            flex: 1;
            gap: 0px;
            height: calc(100vh - 130px);
            min-height: 0;
            overflow: hidden;
            position: relative;
            z-index: 1;
        }}
        .left-panel {{
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 16px;
            min-height: 0;
            height: 100%;
            padding-right: 12px;
        }}
        .right-panel {{
            width: 400px;
            background: rgba(15, 17, 28, 0.7);
            backdrop-filter: blur(25px);
            -webkit-backdrop-filter: blur(25px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 24px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
            height: 100%;
            box-sizing: border-box;
            box-shadow: -10px 0 30px rgba(0, 0, 0, 0.5), inset 0 1px 1px rgba(255, 255, 255, 0.1);
            transition: width 0.3s cubic-bezier(0.16, 1, 0.3, 1), padding 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.2s ease;
        }}
        .resizer {{
            width: 16px;
            cursor: col-resize;
            background: transparent;
            height: 100%;
            position: relative;
            z-index: 10;
            margin: 0 -4px;
        }}
        .resizer::after {{
            content: '';
            position: absolute;
            left: 7px;
            width: 2px;
            height: 40px;
            top: calc(50% - 20px);
            background: rgba(255, 255, 255, 0.2);
            border-radius: 4px;
            transition: all 0.2s ease;
        }}
        .resizer:hover::after, .resizer.dragging::after {{
            background: #8b5cf6;
            box-shadow: 0 0 10px #8b5cf6;
            height: 60px;
            width: 4px;
            left: 6px;
        }}
        #mynetwork {{
            flex: 1;
            min-height: 400px;
            height: 100%;
            border: 1px solid rgba(255, 255, 255, 0.08);
            background-color: rgba(10, 11, 18, 0.5);
            border-radius: 16px;
            box-shadow: inset 0 4px 24px rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(5px);
        }}
        .legend {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            padding: 14px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            font-weight: 500;
            color: #94a3b8;
        }}
        .legend-color {{
            width: 12px;
            height: 12px;
            border-radius: 4px;
            border: 1px solid rgba(255, 255, 255, 0.15);
        }}
        .query-container {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.06);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 16px 20px;
            font-size: 15px;
            line-height: 1.6;
            border-left: 4px solid #8b5cf6;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
        }}
        .query-label {{
            font-weight: 600;
            color: #94a3b8;
            margin-bottom: 6px;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 1.5px;
        }}
        
        /* Details Panel Styling */
        .details-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 16px;
            margin-bottom: 8px;
        }}
        .details-title {{
            font-size: 20px;
            font-weight: 600;
            color: #ffffff;
            margin: 0;
            letter-spacing: -0.3px;
        }}
        .close-panel-btn {{
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #94a3b8;
            cursor: pointer;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
        }}
        .close-panel-btn:hover {{
            background: rgba(239, 68, 68, 0.15);
            border-color: rgba(239, 68, 68, 0.3);
            color: #f87171;
            transform: scale(1.05);
        }}
        .open-panel-btn {{
            position: absolute;
            right: 24px;
            bottom: 24px;
            background: rgba(139, 92, 246, 0.8);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            color: #ffffff;
            border: 1px solid rgba(255, 255, 255, 0.15);
            padding: 12px 20px;
            border-radius: 30px;
            cursor: pointer;
            z-index: 1000;
            font-size: 14px;
            font-weight: 600;
            letter-spacing: -0.2px;
            box-shadow: 0 8px 32px rgba(139, 92, 246, 0.3);
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }}
        .open-panel-btn:hover {{
            background: #8b5cf6;
            transform: translateY(-2px);
            box-shadow: 0 12px 36px rgba(139, 92, 246, 0.45);
        }}
        .details-section {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .details-sec-title {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: #94a3b8;
            font-weight: 700;
        }}
        .details-value {{
            font-size: 14px;
            background: rgba(0, 0, 0, 0.3);
            padding: 14px 18px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            color: #cbd5e1;
            line-height: 1.6;
            word-break: break-word;
        }}
        .pre-container {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 12.5px;
            white-space: pre-wrap;
            overflow-x: auto;
            max-height: 280px;
            box-shadow: inset 0 2px 8px rgba(0,0,0,0.4);
        }}
        .status-pill {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 30px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        .status-complete {{
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.25);
            box-shadow: 0 0 12px rgba(16, 185, 129, 0.1);
        }}
        .status-running {{
            background: rgba(245, 158, 11, 0.12);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.25);
            box-shadow: 0 0 12px rgba(245, 158, 11, 0.1);
        }}
        .status-pending {{
            background: rgba(148, 163, 184, 0.12);
            color: #94a3b8;
            border: 1px solid rgba(148, 163, 184, 0.25);
        }}
        .status-failed {{
            background: rgba(239, 68, 68, 0.12);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.25);
            box-shadow: 0 0 12px rgba(239, 68, 68, 0.1);
        }}
        
        /* Empty state */
        .empty-details {{
            display: flex;
            align-items: center;
            justify-content: center;
            flex: 1;
            color: #64748b;
            text-align: center;
            font-style: italic;
            font-size: 14px;
            line-height: 1.6;
            padding: 0 20px;
        }}
        
        /* Custom Scrollbar */
        ::-webkit-scrollbar {{
            width: 6px;
            height: 6px;
        }}
        ::-webkit-scrollbar-track {{
            background: rgba(0, 0, 0, 0.1);
            border-radius: 10px;
        }}
        ::-webkit-scrollbar-thumb {{
            background: rgba(255, 255, 255, 0.08);
            border-radius: 10px;
        }}
        ::-webkit-scrollbar-thumb:hover {{
            background: rgba(255, 255, 255, 0.16);
        }}
    </style>
</head>
<body>
    <h2>DAG Visualization for Browser Agent: {session_id}</h2>
    
    <div class="main-container">
        <div class="left-panel">
            <div class="query-container">
                <div class="query-label">Session Query</div>
                <div>{query}</div>
            </div>
            <div class="legend">
                {legend_html}
            </div>
            <div id="mynetwork"></div>
        </div>
        <div class="resizer" id="drag-resizer"></div>
        <div class="right-panel" id="details-panel">
            <!-- Details will be rendered here dynamically -->
        </div>
        <button class="open-panel-btn" id="open-panel-btn" style="display: none;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline></svg>
            Show Details
        </button>
    </div>

    <script type="text/javascript">
        const nodes = new vis.DataSet({nodes_json});
        const edges = new vis.DataSet({edges_json});

        const container = document.getElementById('mynetwork');
        const data = {{ nodes: nodes, edges: edges }};
        const options = {{
            layout: {{
                hierarchical: {{
                    enabled: true,
                    direction: 'LR',
                    sortMethod: 'directed',
                    nodeSpacing: 160,
                    levelSeparation: 260
                }}
            }},
            nodes: {{
                shape: 'box',
                margin: 12,
                font: {{
                    face: 'Outfit',
                    size: 14,
                    color: '#ffffff'
                }},
                borderWidth: 2,
                shadow: {{
                    enabled: true,
                    color: 'rgba(0,0,0,0.5)',
                    size: 10,
                    x: 0,
                    y: 4
                }}
            }},
            edges: {{
                arrows: {{
                    to: {{ enabled: true, scaleFactor: 1 }}
                }},
                color: {{
                    color: 'rgba(255, 255, 255, 0.15)',
                    highlight: '#8b5cf6',
                    hover: '#8b5cf6'
                }},
                width: 2,
                shadow: {{
                    enabled: true,
                    color: 'rgba(0,0,0,0.3)',
                    size: 4,
                    x: 0,
                    y: 2
                }}
            }}
        }};
        
        const network = new vis.Network(container, data, options);
        const detailsPanel = document.getElementById('details-panel');
        const resizer = document.getElementById('drag-resizer');
        const openBtn = document.getElementById('open-panel-btn');
        
        network.on("selectNode", function (params) {{
            if (params.nodes.length > 0) {{
                const nodeId = params.nodes[0];
                const nodeData = nodes.get(nodeId);
                if (nodeData && nodeData.details) {{
                    renderDetails(nodeData.details);
                }}
            }}
        }});
        
        network.on("deselectNode", function (params) {{
            renderEmptyDetails();
        }});
        
        function renderEmptyDetails() {{
            detailsPanel.innerHTML = 
                '<div class="details-header">' +
                '<span class="details-title">Node details</span>' +
                '<button class="close-panel-btn" onclick="collapsePanel()">' +
                '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
                '</button>' +
                '</div>' +
                '<div class="empty-details">Select a node in the graph to view details, prompts, and outputs.</div>';
        }}
        
        function renderDetails(d) {{
            const statusClass = 'status-' + (d.status || 'pending');
            let resultHTML = '';
            let metadataHTML = '';
            
            if (d.result && Object.keys(d.result).length > 0) {{
                const resText = JSON.stringify(d.result, null, 2);
                resultHTML = '<div class="details-section">' +
                             '<div class="details-sec-title">Result Output</div>' +
                             '<pre class="details-value pre-container">' + escapeHtml(resText) + '</pre>' +
                             '</div>';
            }}
            
            if (d.metadata && Object.keys(d.metadata).length > 0) {{
                const metaText = JSON.stringify(d.metadata, null, 2);
                metadataHTML = '<div class="details-section">' +
                               '<div class="details-sec-title">Metadata</div>' +
                               '<pre class="details-value pre-container">' + escapeHtml(metaText) + '</pre>' +
                               '</div>';
            }}
            
            let promptHTML = '';
            if (d.prompt_sent) {{
                promptHTML = '<div class="details-section">' +
                             '<div class="details-sec-title">Prompt Sent</div>' +
                             '<pre class="details-value pre-container">' + escapeHtml(d.prompt_sent) + '</pre>' +
                             '</div>';
            }}
            
            detailsPanel.innerHTML = 
                '<div class="details-header">' +
                '<span class="details-title">Node details: ' + escapeHtml(d.id) + '</span>' +
                '<button class="close-panel-btn" onclick="collapsePanel()">' +
                '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
                '</button>' +
                '</div>' +
                '<div class="details-section">' +
                '<div class="details-sec-title">Skill / Agent</div>' +
                '<div class="details-value">' + escapeHtml(d.skill) + '</div>' +
                '</div>' +
                '<div class="details-section">' +
                '<div class="details-sec-title">Status</div>' +
                '<div><span class="status-pill ' + statusClass + '">' + escapeHtml(d.status || 'unknown') + '</span></div>' +
                '</div>' +
                '<div class="details-section">' +
                '<div class="details-sec-title">Inputs</div>' +
                '<div class="details-value">' + escapeHtml(JSON.stringify(d.inputs)) + '</div>' +
                '</div>' +
                resultHTML +
                promptHTML +
                metadataHTML;
        }}
        
        function escapeHtml(text) {{
            if (!text) return '';
            return text.toString()
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }}

        // Resizer drag-and-drop logic
        let isDragging = false;

        resizer.addEventListener('mousedown', function(e) {{
            isDragging = true;
            resizer.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
        }});

        document.addEventListener('mousemove', function(e) {{
            if (!isDragging) return;
            const containerWidth = document.querySelector('.main-container').clientWidth;
            const newWidth = containerWidth - e.clientX;
            
            if (newWidth < 50) {{
                collapsePanel();
            }} else if (newWidth > containerWidth * 0.7) {{
                detailsPanel.style.width = (containerWidth * 0.7) + 'px';
            }} else {{
                detailsPanel.style.width = newWidth + 'px';
                detailsPanel.style.display = 'flex';
                // Reset styles changed during transition
                detailsPanel.style.paddingLeft = '24px';
                detailsPanel.style.paddingRight = '24px';
                detailsPanel.style.borderLeftWidth = '1px';
                detailsPanel.style.opacity = '1';
                resizer.style.display = 'block';
                openBtn.style.display = 'none';
            }}
        }});

        document.addEventListener('mouseup', function() {{
            if (isDragging) {{
                isDragging = false;
                resizer.classList.remove('dragging');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }}
        }});

        function collapsePanel() {{
            detailsPanel.style.width = '0px';
            detailsPanel.style.paddingLeft = '0px';
            detailsPanel.style.paddingRight = '0px';
            detailsPanel.style.borderLeftWidth = '0px';
            detailsPanel.style.opacity = '0';
            setTimeout(() => {{
                if (detailsPanel.style.width === '0px') {{
                    detailsPanel.style.display = 'none';
                }}
            }}, 300);
            resizer.style.display = 'none';
            openBtn.style.display = 'flex';
        }}

        function expandPanel() {{
            detailsPanel.style.display = 'flex';
            // Force reflow
            detailsPanel.offsetHeight;
            detailsPanel.style.width = '400px';
            detailsPanel.style.paddingLeft = '24px';
            detailsPanel.style.paddingRight = '24px';
            detailsPanel.style.borderLeftWidth = '1px';
            detailsPanel.style.opacity = '1';
            resizer.style.display = 'block';
            openBtn.style.display = 'none';
        }}

        openBtn.addEventListener('click', expandPanel);
        
        renderEmptyDetails();
    </script>
</body>
</html>
"""
        html_content = html_template.format(
            session_id=self.session_id,
            query=query,
            nodes_json=json.dumps(vis_nodes),
            edges_json=json.dumps(vis_edges),
            legend_html=legend_html
        )
        html_path = self.dir / "graph.html"
        _atomic_write(html_path, html_content)


    def read_graph(self) -> nx.DiGraph | None:
        if self.graph_path.exists():
            payload = json.loads(self.graph_path.read_text())
            g = nx.node_link_graph(payload, edges="edges", directed=True)
            # NOTES_RUNS round-3 review #4: a write tagged a node's `result`
            # as a typed AgentResult via `_result_typed`. If the dict no
            # longer round-trips through AgentResult.model_validate, that
            # is silent data corruption — the previous "keep the dict, let
            # downstream isinstance checks handle it" was exactly the
            # silent-degradation pattern we just fixed in P0 #2.
            # Raise instead; the SessionLoadError surfaces the bad file path
            # and the validation message so the operator can act on it.
            for nid, d in g.nodes(data=True):
                if d.pop("_result_typed", False) and isinstance(d.get("result"), dict):
                    try:
                        d["result"] = AgentResult.model_validate(d["result"])
                    except (ValueError, TypeError) as e:
                        raise SessionLoadError(
                            f"node {nid} in {self.graph_path}: persisted "
                            f"AgentResult failed model_validate. The graph "
                            f"is unsafe to resume — inspect the file and "
                            f"either repair it or delete the session. "
                            f"validation error: {type(e).__name__}: {e}"
                        ) from e
            return g
        # Backwards-compat: tolerate sessions written by the pre-P1 pickle
        # path. We import pickle lazily so the dependency is only paid when
        # someone resumes a legacy session.
        if self._legacy_graph_path.exists():
            import pickle, sys
            print(f"[persistence] reading legacy pickle graph from "
                  f"{self._legacy_graph_path}", file=sys.stderr)
            return pickle.loads(self._legacy_graph_path.read_bytes())
        return None

    def _node_path(self, node_id: str) -> Path:
        # node_id is like "n:1" — turn that into n_001.json so directory
        # listings sort sensibly.
        try:
            i = int(node_id.split(":", 1)[1])
            return self.nodes_dir / f"n_{i:03d}.json"
        except (IndexError, ValueError):
            safe = node_id.replace(":", "_").replace("/", "_")
            return self.nodes_dir / f"{safe}.json"

    def write_node(self, state: NodeState) -> None:
        _atomic_write(self._node_path(state.node_id), state.model_dump_json(indent=2))

    def read_node(self, node_id: str) -> NodeState | None:
        p = self._node_path(node_id)
        if not p.exists():
            return None
        return NodeState.model_validate_json(p.read_text())

    def read_all_nodes(self) -> list[NodeState]:
        """Load every persisted NodeState in this session. Corrupt or
        partially-written files (the typical cause is a process kill between
        the temp-file write and the atomic rename) are skipped with a clear
        warning to stderr — never silently dropped. NOTES_RUNS feedback
        P0 #2: a bare `except Exception: continue` here was killing resume
        invisibly when one node file was bad."""
        import sys
        states: list[NodeState] = []
        for p in sorted(self.nodes_dir.glob("n_*.json")):
            try:
                states.append(NodeState.model_validate_json(p.read_text()))
            except (OSError, ValueError) as e:
                # OSError = unreadable; ValueError covers JSON decode +
                # Pydantic ValidationError (which inherits ValueError).
                print(f"[persistence] WARNING: skipped corrupt node file "
                      f"{p}: {type(e).__name__}: {e}", file=sys.stderr)
        return states


def list_sessions() -> list[str]:
    if not SESSIONS_ROOT.exists():
        return []
    return sorted(p.name for p in SESSIONS_ROOT.iterdir() if p.is_dir())
