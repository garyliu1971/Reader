import json
import urllib.request
from urllib.parse import urlparse
from PySide6.QtCore import QObject, Signal

# ---- 超时（秒）：摘要/问答/续写允许更长推理时间 ----
_SHORT_TIMEOUT = 30
_LONG_TIMEOUT  = 90

def _build_chat_url(base: str, api_version: str) -> str:
    """构造 chat/completions URL，Azure 旧式部署路径自动附加 api-version。

    支持两种 Azure 路径形式：
      新式 v1（推荐）: .../openai/v1          → 直接拼 /chat/completions
      旧式部署路径:    .../openai/deployments/xxx → 附加 ?api-version=...
    其余（OpenAI / DeepSeek / Ollama 等）不做处理。
    """
    url = base + "/chat/completions"
    # 旧式 Azure 部署路径：包含 /deployments/ 且不含 /v1
    if "/deployments/" in base and "/v1" not in base:
        ver = (api_version or "2024-10-21").strip()
        url += f"?api-version={ver}"
    return url


def call_llm(prompt, cfg, timeout=_SHORT_TIMEOUT, messages=None):
    """单轮或多轮调用。
    messages: 若传入则作为完整 messages 列表（多轮对话），忽略 prompt。

    Azure AI Foundry 两种用法：
      1. 新式 v1（推荐）
         api_base  = https://<resource>.openai.azure.com/openai/v1
         model     = <Deployment Name>（如 gpt-4o-mini）
         api_key   = <Azure API Key>
         无需填 api_version
      2. 旧式部署路径
         api_base  = https://<resource>.openai.azure.com/openai/deployments/<deployment>
         api_version = 2024-10-21（或其他版本）
         model 可留空（路径已含部署名）
    """
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("未配置 API Key（工具栏→设置）")
    base = (cfg.get("api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    api_version = (cfg.get("api_version") or "").strip()
    msgs = messages if messages is not None else [{"role": "user", "content": prompt}]
    model_name = (cfg.get("model") or "").strip()
    payload = {"model": model_name, "messages": msgs}
    # temperature: gpt-5 系列会拒绝该参数，需略去。
    # cfg["temperature"] == "none" （来自 .env CODING_AGENT_TEMPERATURE=none）或
    # model 名含 "gpt-5" 时自动略去。
    t = cfg.get("temperature", 0.2)
    omit_temp = (str(t).lower() == "none") or ("gpt-5" in model_name.lower())
    if not omit_temp:
        payload["temperature"] = float(t) if t != "none" else 0.2
    host = urlparse(base).hostname or ""
    if host in ("localhost", "127.0.0.1"):
        payload["think"] = False
    url = _build_chat_url(base, api_version)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def translate(text, cfg):
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def translate_to(text, cfg, target="中文"):
    if target == "英文":
        return call_llm(f"把下面这段小说文本翻译成地道的英文，只输出译文：\n{text}", cfg)
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def lookup(word, cfg):
    return call_llm(f"解释词语「{word}」：给出词性、释义和一条例句。", cfg)

# ================================================================
# 新增 AI 辅助功能
# ================================================================

def summarize_chapter(text, title, cfg):
    """📝 章节摘要：提炼本章核心内容（300字以内）。"""
    snippet = text[:4000]   # 避免超出 token 限制
    prompt = (
        f"以下是小说章节《{title}》的正文内容（节选）：\n\n{snippet}\n\n"
        "请用简体中文，300字以内，提炼本章的核心剧情、关键事件和重要转折，"
        "分段列出，不要复述原文，直接输出摘要。"
    )
    return call_llm(prompt, cfg, timeout=_LONG_TIMEOUT)

def extract_mindmap(text, title, cfg):
    """🗺️ 大纲/思维导图：提取本章人物、事件、关键词层级结构。"""
    snippet = text[:4000]
    prompt = (
        f"以下是小说章节《{title}》的正文（节选）：\n\n{snippet}\n\n"
        "请用简体中文输出本章的结构化大纲，格式如下（用缩进表示层级）：\n"
        "【人物】\n  - 主要人物及其行动\n"
        "【主线事件】\n  1. 事件一\n  2. 事件二\n"
        "【关键细节】\n  - 重要物品/地点/伏笔\n"
        "【情感基调】\n  - 一句话概括\n"
        "只输出大纲内容，不要解释。"
    )
    return call_llm(prompt, cfg, timeout=_LONG_TIMEOUT)

def ai_qa(question, context_text, title, cfg, history=None):
    """❓ AI 问答（支持多轮对话）。
    history: [(role, content), ...] 本次之前的对话记录。
    """
    snippet = context_text[:5000]
    system_msg = {
        "role": "system",
        "content": (
            f"你是一位博学的读书助手，正在帮读者阅读小说章节《{title}》。"
            f"以下是本章节的部分内容供你参考：\n\n{snippet}\n\n"
            "请始终基于上文内容回答，如正文中没有明确信息请如实说明，用简体中文作答。"
        )
    }
    messages = [system_msg]
    for role, content in (history or []):
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return call_llm("", cfg, timeout=_LONG_TIMEOUT, messages=messages)

def continue_story(text, cfg):
    """✍️ 续写建议：根据当前段落预测/续写下一段情节（200字以内）。"""
    # 取最近 1500 字作为上文
    snippet = text[-1500:] if len(text) > 1500 else text
    prompt = (
        f"以下是小说的一段正文：\n\n{snippet}\n\n"
        "请根据上文的风格、节奏和伏笔，用简体中文续写下一段情节，"
        "200字以内，保持原文的语气和人称，只输出续写内容。"
    )
    return call_llm(prompt, cfg, timeout=_LONG_TIMEOUT)

def analyze_sentiment(text, cfg):
    """😊 情感分析：分析所选段落的情感色彩与人物心理。"""
    snippet = text[:2000]
    prompt = (
        f"请对以下小说段落进行情感分析：\n\n{snippet}\n\n"
        "输出格式：\n"
        "【整体情感】正面 / 负面 / 中性 / 复杂（一词概括）\n"
        "【情感强度】弱 / 中 / 强\n"
        "【主要情绪】列出 2~4 种情绪词（如：紧张、悲伤、希望……）\n"
        "【人物心理】简述主要人物当前的内心状态（1~2句）\n"
        "【叙事氛围】一句话描述段落整体氛围\n"
        "只输出以上内容，不要多余解释。"
    )
    return call_llm(prompt, cfg, timeout=_SHORT_TIMEOUT)

def explain_background(text, cfg):
    """🔍 背景解读：解释段落中涉及的历史/文化/地理背景知识。"""
    snippet = text[:2000]
    prompt = (
        f"以下是一段小说文本：\n\n{snippet}\n\n"
        "请识别其中可能需要背景知识才能理解的内容（历史事件、地名、文化习俗、专有名词等），"
        "用简体中文逐条解释，格式：\n"
        "· 【词条】：解释\n"
        "若无需特别解释的内容，请回复「本段落无需特别背景知识」。"
    )
    return call_llm(prompt, cfg, timeout=_SHORT_TIMEOUT)

def extract_knowledge_graph(text, title, cfg):
    """🕸️ 知识图谱：从章节文本中提取人物/地点/事件节点及其关系，返回 JSON 字符串。"""
    snippet = text[:6000]
    prompt = (
        f"以下是小说章节《{title}》的正文（节选）：\n\n{snippet}\n\n"
        "请从中提取知识图谱，严格按照以下 JSON 格式输出，不要输出任何其他内容：\n"
        "{\n"
        '  "nodes": [\n'
        '    {"id": "唯一ID", "label": "显示名称", "type": "person|place|event|item|concept", "desc": "一句话描述"}\n'
        "  ],\n"
        '  "edges": [\n'
        '    {"from": "源节点ID", "to": "目标节点ID", "label": "关系描述"}\n'
        "  ]\n"
        "}\n\n"
        "type 说明：person=人物, place=地点, event=事件, item=物品/道具, concept=概念/势力\n"
        "要求：节点 10~25 个，边 10~30 条，只提取对理解本章有意义的内容，JSON 必须合法。"
    )
    raw = call_llm(prompt, cfg, timeout=_LONG_TIMEOUT)
    # 提取 JSON 块（LLM 有时会在前后加 markdown 代码块标记）
    import re as _re
    m = _re.search(r'\{[\s\S]*\}', raw)
    if not m:
        raise RuntimeError(f"LLM 未返回合法 JSON：{raw[:200]}")
    return m.group(0)

# ================================================================

class AiWorker(QObject):
    done = Signal(str); failed = Signal(str)
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:
            self.failed.emit(str(e))
