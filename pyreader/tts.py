import os
import io
import wave
import time
import threading
import asyncio
import urllib.request
from PySide6.QtCore import QObject, Signal
from .config import APP_DIR
from .ai import translate_to

# ============ 语音朗读（edge-tts） ============
TTS_VOICES = [
    ("晓晓（女·温柔，默认）", "zh-CN-XiaoxiaoNeural"),
    ("晓伊（女·活泼）", "zh-CN-XiaoyiNeural"),
    ("晓墨（女·可爱）", "zh-CN-XiaomoNeural"),
    ("云希（男·少年）", "zh-CN-YunxiNeural"),
    ("云扬（男·新闻）", "zh-CN-YunyangNeural"),
    ("云健（男）", "zh-CN-YunjianNeural"),
    ("晓北（东北女声）", "zh-CN-liaoning-XiaobeiNeural"),
    ("晓妮（陕西女声）", "zh-CN-shaanxi-XiaoniNeural"),
    ("晓臻（台湾女声）", "zh-TW-HsiaoChenNeural"),
    ("云哲（台湾男声）", "zh-TW-YunJheNeural"),
    ("Aria（英文·美式·女）", "en-US-AriaNeural"),
    ("Jenny（英文·美式·女）", "en-US-JennyNeural"),
    ("Guy（英文·美式·男）", "en-US-GuyNeural"),
    ("Sonia（英文·英式·女）", "en-GB-SoniaNeural"),
    ("Ryan（英文·英式·男）", "en-GB-RyanNeural"),
    ("Nanami（日文·女）", "ja-JP-NanamiNeural"),
    ("Keita（日文·男）", "ja-JP-KeitaNeural"),
    ("Huayan（中文·离线，无需联网）", "piper:zh_CN-huayan-medium"),
    ("Lessac（英文·离线，无需联网）", "piper:en_US-lessac-medium"),
]
TTS_RATES = ["-50%", "-25%", "-10%", "+0%", "+10%", "+25%", "+50%", "+100%"]
TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# Piper 离线语音：voice id -> Hugging Face piper-voices 仓库里的路径前缀
PIPER_VOICES = {
    "zh_CN-huayan-medium": "zh/zh_CN/huayan/medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium",
}
PIPER_MODEL_BASE_URL = "https://hf-mirror.com/rhasspy/piper-voices/resolve/main"
_piper_voice_cache = {}
_piper_voice_cache_lock = threading.Lock()

def _piper_model_dir():
    d = os.path.join(APP_DIR, "piper_models")
    os.makedirs(d, exist_ok=True)
    return d

def _ensure_piper_model(voice_id: str) -> str:
    """确保 voice_id 对应的 Piper 模型文件（.onnx + .onnx.json）已下载到本地，返回 onnx 路径。"""
    if voice_id not in PIPER_VOICES:
        raise RuntimeError(f"未知的离线语音：{voice_id}")
    d = _piper_model_dir()
    onnx_path = os.path.join(d, f"{voice_id}.onnx")
    cfg_path = onnx_path + ".json"
    prefix = PIPER_VOICES[voice_id]
    if not (os.path.isfile(onnx_path) and os.path.isfile(cfg_path)):
        _log_tts(f"[离线朗读] 首次使用，正在下载模型：{voice_id}（约 60MB，需联网一次）")
        for path, url in ((onnx_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx"),
                          (cfg_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx.json")):
            tmp = path + ".part"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                    out.write(resp.read())
                os.replace(tmp, path)
            except Exception as e:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise RuntimeError(f"离线语音模型下载失败：{e}") from e
    return onnx_path

def _piper_rate_to_length_scale(rate: str) -> float:
    """把 edge-tts 风格的 "+10%"/"-25%" 语速转成 Piper 的 length_scale（越大越慢）。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    pct = max(-90, min(200, pct))
    return 1.0 / (1.0 + pct / 100.0)

def _load_piper_voice(voice_id: str):
    with _piper_voice_cache_lock:
        voice = _piper_voice_cache.get(voice_id)
        if voice is not None:
            return voice
        try:
            from piper import PiperVoice
        except Exception as e:
            raise RuntimeError("未安装 piper-tts，请执行：pip install piper-tts") from e
        onnx_path = _ensure_piper_model(voice_id)
        voice = PiperVoice.load(onnx_path)
        _piper_voice_cache[voice_id] = voice
        return voice

def synthesize_tts_piper(text: str, voice_id: str, rate: str) -> bytes:
    """离线合成（Piper），返回 wav 字节。"""
    from piper import SynthesisConfig
    voice = _load_piper_voice(voice_id)
    syn_config = SynthesisConfig(length_scale=_piper_rate_to_length_scale(rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue()

def split_sentences(text: str, base: int = 0):
    """把文本切成句子：返回 [(起始偏移, 结束偏移, 句子文本)]，偏移为全局字符偏移。"""
    res = []
    seg_start = 0
    i = 0
    n = len(text)
    buf_len = 0
    while i < n:
        ch = text[i]
        buf_len += 1
        if ch in "。！？!?；;" or ch == "\n" or buf_len >= 100:
            end = i + 1
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            buf_len = 0
        i += 1
    if seg_start < n:
        seg = text[seg_start:n]
        if seg.strip():
            res.append((base + seg_start, base + n, seg))
    return res

def synthesize_tts(text: str, voice: str, rate: str) -> bytes:
    """把一段文本合成音频字节。voice 以 "piper:" 开头走本地离线合成（wav），
    否则走 edge-tts 云端合成（mp3，直连，带重试）。"""
    if voice.startswith("piper:"):
        return synthesize_tts_piper(text, voice[len("piper:"):], rate)
    try:
        import edge_tts
    except Exception as e:
        raise RuntimeError("未安装 edge-tts，请执行：pip install edge-tts") from e
    last_err = None
    for attempt in range(3):
        try:
            return _synth_once(edge_tts, text, voice, rate)
        except Exception as e:
            last_err = e
            _log_tts(f"[重试] 第{attempt + 1}次失败（原文前30字：{text[:30]!r}）：{e}")
            time.sleep(0.6 * (attempt + 1))   # 0.6s / 1.2s 退避后重试
    raise RuntimeError(f"语音合成失败（已重试3次）：{last_err}")

def _log_tts(msg):
    """把朗读相关事件写入 ~/.pyreader/tts.log。"""
    try:
        with open(os.path.join(APP_DIR, "tts.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def _synth_once(edge_tts, text, voice, rate):
    async def go():
        com = edge_tts.Communicate(text, voice, rate=rate)
        out = []
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                out.append(chunk["data"])
        if not out:
            raise RuntimeError("edge-tts 未返回音频")
        return b"".join(out)
    return asyncio.run(go())

class TtsWorker(QObject):
    ready = Signal(int, bytes)     # (句子序号, mp3 字节)
    failed = Signal(int, str)      # (句子序号, 错误信息)
    def __init__(self, seq, text, voice, rate):
        super().__init__()
        self.seq, self.text, self.voice, self.rate = seq, text, voice, rate
    def run(self):
        try:
            self.ready.emit(self.seq, synthesize_tts(self.text, self.voice, self.rate))
        except Exception as e:
            self.failed.emit(self.seq, str(e))

class TranslateWorker(QObject):
    done = Signal(int, str)        # (句子序号, 译文)
    failed = Signal(int, str)      # (句子序号, 错误信息)
    def __init__(self, seq, text, cfg, target):
        super().__init__()
        self.seq, self.text, self.cfg, self.target = seq, text, cfg, target
    def run(self):
        try:
            self.done.emit(self.seq, translate_to(self.text, self.cfg, self.target))
        except Exception as e:
            self.failed.emit(self.seq, str(e))
