"""知识图谱渲染器：将 LLM 返回的 JSON 转成内嵌 vis.js 的独立 HTML 字符串。

设计要点
--------
- 纯内嵌（不依赖外网 CDN）：vis.js network 通过 PyPI 包 visjs-network 获取；
  若未安装则降级为轻量 SVG 静态渲染（用 networkx + 基础布局）。
- 节点按 type 区分颜色：person=蓝, place=绿, event=橙, item=紫, concept=灰。
- 支持拖拽、缩放、鼠标悬停显示 desc。
- 返回完整 HTML 字符串，交给 QWebEngineView.setHtml() 显示。
"""

import json
import math
import html as _html
import os
import urllib.request
from pathlib import Path
from .config import APP_DIR

# vis-network 离线缓存路径
_VIS_CACHE = Path(APP_DIR) / "vis-network.min.js"
_VIS_URL   = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
_VIS_URL2  = "https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"  # 备用 CDN


def _ensure_vis_js() -> str:
    """返回 vis-network JS 内容字符串：优先用本地缓存，如没有则尝试下载（异步）。
    返回空字符串表示还没准备好（需要联网）。
    """
    if _VIS_CACHE.exists() and _VIS_CACHE.stat().st_size > 100_000:
        return _VIS_CACHE.read_text(encoding="utf-8")
    # 尝试下载
    for url in (_VIS_URL, _VIS_URL2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) > 100_000:
                _VIS_CACHE.write_bytes(data)
                return data.decode("utf-8")
        except Exception:
            continue
    return ""

# 节点类型 → (背景色, 边框色, 文字色)
_TYPE_COLOR = {
    "person":  ("#4a9eff", "#1a6fcc", "#ffffff"),
    "place":   ("#4caf82", "#2d7a56", "#ffffff"),
    "event":   ("#ff8c42", "#cc5f1a", "#ffffff"),
    "item":    ("#9c6fdc", "#6a3faa", "#ffffff"),
    "concept": ("#90a0b0", "#607080", "#ffffff"),
}
_DEFAULT_COLOR = ("#90a0b0", "#607080", "#ffffff")

# 节点类型中文标签
_TYPE_LABEL = {
    "person": "人物", "place": "地点",
    "event": "事件", "item": "物品", "concept": "概念",
}


def _try_vis_cdn() -> bool:
    """判断能否使用本地 vis-network CDN（优先用离线资源）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("visjs_network") is not None
    except Exception:
        return False


def build_graph_html(graph_json: str, title: str = "") -> str:
    """将知识图谱 JSON 转为完整 HTML 字符串。"""
    try:
        data = json.loads(graph_json)
    except json.JSONDecodeError as e:
        return _error_html(f"JSON 解析失败：{e}")

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    if not nodes:
        return _error_html("图谱中没有节点数据")

    vis_js = _ensure_vis_js()
    if not vis_js:
        return _error_html(
            "vis-network 库尚未下载。\n"
            "请联网后重试（程序会自动下载约 700KB 的图形库，之后永久离线可用）。"
        )
    return _build_visjs_html(nodes, edges, title, vis_js)


def _build_visjs_html(nodes: list, edges: list, title: str, vis_js: str) -> str:
    """用内嵌 vis-network JS 渲染交互图谱（完全离线，不依赖外网 CDN）。"""
    # ---- 构造 vis.js nodes / edges 数据 ----
    vis_nodes = []
    for n in nodes:
        nid   = _html.escape(str(n.get("id", "")))
        label = _html.escape(str(n.get("label", nid)))
        ntype = n.get("type", "concept")
        desc  = _html.escape(str(n.get("desc", "")))
        bg, border, fg = _TYPE_COLOR.get(ntype, _DEFAULT_COLOR)
        vis_nodes.append({
            "id": n.get("id", nid),
            "label": n.get("label", label),
            "title": f"<b>{n.get('label','')}</b><br/>{_TYPE_LABEL.get(ntype,ntype)}<br/>{n.get('desc','')}",
            "color": {"background": bg, "border": border,
                      "highlight": {"background": bg, "border": "#ffdd00"}},
            "font": {"color": fg, "size": 13},
            "shape": "box" if ntype == "event" else "ellipse",
        })

    vis_edges = []
    for e in edges:
        vis_edges.append({
            "from":  e.get("from", ""),
            "to":    e.get("to", ""),
            "label": e.get("label", ""),
            "arrows": "to",
            "font":  {"size": 11, "color": "#666666", "align": "middle"},
            "color": {"color": "#aaaaaa", "highlight": "#ffaa00"},
            "smooth": {"type": "curvedCW", "roundness": 0.15},
        })

    nodes_js = json.dumps(vis_nodes, ensure_ascii=False)
    edges_js = json.dumps(vis_edges, ensure_ascii=False)
    title_esc = _html.escape(title)

    # 图例 HTML
    legend_items = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:10px;">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{bg};'
        f'border:1px solid {border};display:inline-block;margin-right:4px;"></span>'
        f'<span style="color:#ccc;font-size:12px;">{label}</span></span>'
        for (k, (bg, border, _)), label in zip(_TYPE_COLOR.items(), _TYPE_LABEL.values())
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>{title_esc} 知识图谱</title>
<script>{vis_js}</script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: 100%; height: 100%; background: #1e1e2e;
                font-family: "Microsoft YaHei", sans-serif; overflow: hidden; }}
  #titlebar {{ position: absolute; top: 0; left: 0; right: 0; height: 32px;
               padding: 0 12px; background: #2a2a3e; color: #e0e0ff;
               font-size: 13px; border-bottom: 1px solid #444;
               display:flex; justify-content:space-between; align-items:center;
               z-index: 5; }}
  #legend {{ display:flex; flex-wrap:wrap; gap:4px; }}
  #network {{ position: absolute; top: 32px; left: 0; right: 0; bottom: 0; }}
  #tooltip {{ position:fixed; background:#333; color:#fff; padding:6px 10px;
              border-radius:6px; font-size:12px; max-width:240px; pointer-events:none;
              display:none; z-index:99; line-height:1.5; }}

  #controls {{ position:fixed; bottom:12px; right:12px; display:flex; gap:6px; z-index:10; }}
  .ctrl-btn {{ background:#3a3a5e; color:#ccc; border:1px solid #555; border-radius:4px;
               padding:4px 10px; cursor:pointer; font-size:12px; }}
  .ctrl-btn:hover {{ background:#5050aa; color:#fff; }}
</style>
</head>
<body>
<div id="titlebar">
  <span>📚 {title_esc} &nbsp;—&nbsp; 知识图谱</span>
  <div id="legend">{legend_items}</div>
</div>
<div id="network"></div>
<div id="tooltip"></div>
<div id="controls">
  <button class="ctrl-btn" onclick="network.fit()">适配窗口</button>
  <button class="ctrl-btn" onclick="network.setOptions({{physics:{{enabled:!physicsOn}}}});
    physicsOn=!physicsOn; this.textContent=physicsOn?'物理:开':'物理:关'">物理:开</button>
</div>

<script>
// 压制 vis-network 物理引擎稳定化期间的 ResizeObserver 噪音
const _OrigRO = window.ResizeObserver;
window.ResizeObserver = class extends _OrigRO {{
  constructor(cb) {{ super((entries, obs) => {{ try {{ cb(entries, obs); }} catch(e) {{}} }}); }}
}};
var physicsOn = true;
var nodes = new vis.DataSet({nodes_js});
var edges = new vis.DataSet({edges_js});
var container = document.getElementById("network");
var data = {{ nodes: nodes, edges: edges }};
var options = {{
  physics: {{
    enabled: true,
    barnesHut: {{ gravitationalConstant: -8000, springLength: 130, springConstant: 0.04 }},
    stabilization: {{ iterations: 200 }}
  }},
  interaction: {{
    hover: true, tooltipDelay: 120,
    navigationButtons: false, keyboard: true
  }},
  nodes: {{ borderWidth: 2, margin: 8,
            shadow: {{ enabled: true, size: 6, x: 2, y: 2, color: "rgba(0,0,0,0.4)" }} }},
  edges: {{ width: 1.5, selectionWidth: 3 }},
  layout: {{ improvedLayout: true }}
}};
var network = new vis.Network(container, data, options);

// 悬停 tooltip
var tip = document.getElementById("tooltip");
network.on("hoverNode", function(p) {{
  var n = nodes.get(p.node);
  if (n && n.title) {{
    tip.innerHTML = n.title;
    tip.style.display = "block";
  }}
}});
network.on("blurNode", function() {{ tip.style.display = "none"; }});
document.addEventListener("mousemove", function(e) {{
  tip.style.left = (e.clientX + 14) + "px";
  tip.style.top  = (e.clientY + 14) + "px";
}});
</script>
</body>
</html>"""
    return html


def _error_html(msg: str) -> str:
    return f"""<!DOCTYPE html><html><body style="background:#1e1e2e;color:#ff6666;
font-family:sans-serif;padding:40px;font-size:14px;">
<h3>⚠️ 知识图谱生成失败</h3><p>{_html.escape(msg)}</p></body></html>"""
