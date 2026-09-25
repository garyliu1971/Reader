import json
import urllib.request
from urllib.parse import urlparse
from PySide6.QtCore import QObject, Signal

def call_llm(prompt, cfg):
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("未配置 API Key（工具栏→设置）")
    base = (cfg.get("api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    payload = {"model": (cfg.get("model") or "").strip(), "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.2}
    # 本地 Ollama 的混合推理模型（如 Qwen3.5）默认会先生成一段隐藏思考再给答案，
    # 翻译这种简单任务用不上，加 think=false 能省一部分延迟（实测约减 15~20%）。
    # 这是 Ollama 专有字段，只在打到本地 Ollama 时加，避免传给云端 API 报错。
    host = urlparse(base).hostname or ""
    if host in ("localhost", "127.0.0.1"):
        payload["think"] = False
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def translate(text, cfg):
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def translate_to(text, cfg, target="中文"):
    if target == "英文":
        return call_llm(f"把下面这段小说文本翻译成地道的英文，只输出译文：\n{text}", cfg)
    return call_llm(f"把下面这段小说文本翻译成简体中文，只输出译文：\n{text}", cfg)

def lookup(word, cfg):
    return call_llm(f"解释词语“{word}”：给出词性、释义和一条例句。", cfg)

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
