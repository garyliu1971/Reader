import os
import io
import wave
import time
import asyncio
import threading
import bz2
import tarfile
import http.client
import urllib.request
from PySide6.QtCore import QObject, Signal
from .config import APP_DIR, bundled_models_dir, net_urlopen
from .ai import translate_to

# ============ 语音朗读（Melo/Kokoro 离线 / Azure Speech / Piper 离线英文） ============
TTS_VOICES = [
    ("Melo（中英混读·女·离线，默认）", "sherpa:melo-zh_en"),
    ("Lessac（英文·离线，无需联网）", "piper:en_US-lessac-medium"),
    # Kokoro-82M（sherpa-onnx 走 espeak-ng 做中文音素化，实测中文自然度/耐听度明显低于 Melo，
    # 仅作备选不作默认，容易听出机器感）
    ("Kokoro-晓晓（中英混读·女·离线，响度更稳但机械感明显）", "kokoro:zf_xiaoxiao"),
    ("Kokoro-晓伊（中英混读·女·离线）", "kokoro:zf_xiaoyi"),
    ("Kokoro-云希（中英混读·男·离线）", "kokoro:zm_yunxi"),
    ("Kokoro-云健（中英混读·男·离线）", "kokoro:zm_yunjian"),
    # Azure Speech（官方接口，需在设置里填 Key 和区域；F0 免费层每月 50 万字符，中日韩每字按 2 计）
    ("Nanami（日文·女·Azure）", "azure:ja-JP-NanamiNeural"),
    ("Keita（日文·男·Azure）", "azure:ja-JP-KeitaNeural"),
    ("SunHi（韩文·女·Azure）", "azure:ko-KR-SunHiNeural"),
    ("InJoon（韩文·男·Azure）", "azure:ko-KR-InJoonNeural"),
    ("Denise（法文·女·Azure）", "azure:fr-FR-DeniseNeural"),
    ("Henri（法文·男·Azure）", "azure:fr-FR-HenriNeural"),
    ("Katja（德文·女·Azure）", "azure:de-DE-KatjaNeural"),
    ("Conrad（德文·男·Azure）", "azure:de-DE-ConradNeural"),
    ("Elvira（西班牙文·女·Azure）", "azure:es-ES-ElviraNeural"),
    ("Alvaro（西班牙文·男·Azure）", "azure:es-ES-AlvaroNeural"),
    ("晓晓（中文·女·温柔·Azure）", "azure:zh-CN-XiaoxiaoNeural"),
    ("晓伊（中文·女·活泼·Azure）", "azure:zh-CN-XiaoyiNeural"),
    ("云希（中文·男·少年·Azure）", "azure:zh-CN-YunxiNeural"),
    ("云扬（中文·男·新闻·Azure）", "azure:zh-CN-YunyangNeural"),
    ("云健（中文·男·Azure）", "azure:zh-CN-YunjianNeural"),
    ("Jenny（英文·美式·女·Azure）", "azure:en-US-JennyNeural"),
    ("Aria（英文·美式·女·Azure）", "azure:en-US-AriaNeural"),
    ("Guy（英文·美式·男·Azure）", "azure:en-US-GuyNeural"),
    ("Sonia（英文·英式·女·Azure）", "azure:en-GB-SoniaNeural"),
    ("Ryan（英文·英式·男·Azure）", "azure:en-GB-RyanNeural"),
]
TTS_RATES = ["-50%", "-25%", "-10%", "+0%", "+10%", "+25%", "+50%", "+100%"]
TTS_DEFAULT_VOICE = "sherpa:melo-zh_en"

# Sherpa-ONNX 离线语音（vits-melo-tts-zh_en，MIT，中英混读）。
# 模型文件从 hf-mirror.com 逐个下载到 ~/.pyreader/sherpa_models/（huggingface.co 在本机会被重置连接）。
SHERPA_MELO_ID = "melo-zh_en"
SHERPA_MELO_DIR = "vits-melo-tts-zh_en"
SHERPA_MELO_BASE_URL = "https://hf-mirror.com/csukuangfj/vits-melo-tts-zh_en/resolve/main"
SHERPA_MELO_FILES = [
    "model.onnx", "lexicon.txt", "tokens.txt",
    "date.fst", "number.fst", "phone.fst", "new_heteronym.fst",
    "dict/jieba.dict.utf8", "dict/hmm_model.utf8", "dict/idf.utf8",
    "dict/stop_words.utf8", "dict/user.dict.utf8",
    "dict/pos_dict/char_state_tab.utf8", "dict/pos_dict/prob_emit.utf8",
    "dict/pos_dict/prob_start.utf8", "dict/pos_dict/prob_trans.utf8",
]
_sherpa_tts = None
_sherpa_lock = threading.Lock()   # 同时保护模型加载和合成调用

def _sherpa_model_dirs() -> list:
    """候选 Melo 模型目录：用户目录优先（可增量更新），其次随程序附带的离线模型目录。"""
    dirs = [os.path.join(APP_DIR, "sherpa_models", SHERPA_MELO_DIR)]
    b = bundled_models_dir()
    if b:
        dirs.append(os.path.join(b, "sherpa_models", SHERPA_MELO_DIR))
    return dirs

def sherpa_model_ready() -> bool:
    return any(os.path.isfile(os.path.join(d, ".complete")) for d in _sherpa_model_dirs())

def _ensure_sherpa_model() -> str:
    """确保 Melo 模型文件齐全（约 190MB，首次需联网），返回模型目录。镜像单文件偶发失败，逐个重试。"""
    d = os.path.join(APP_DIR, "sherpa_models", SHERPA_MELO_DIR)
    ok_flag = os.path.join(d, ".complete")
    if os.path.isfile(ok_flag):
        return d
    b = bundled_models_dir()
    if b:
        bundled = os.path.join(b, "sherpa_models", SHERPA_MELO_DIR)
        if os.path.isfile(os.path.join(bundled, ".complete")):
            _log_tts("[离线朗读] 使用随程序附带的 Melo 模型（无需联网）")
            return bundled
    _log_tts("[离线朗读] 首次使用，正在下载 Melo 模型（约 190MB，需联网一次）")
    for rel in SHERPA_MELO_FILES:
        path = os.path.join(d, *rel.split("/"))
        if os.path.isfile(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        last_err = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(f"{SHERPA_MELO_BASE_URL}/{rel}", headers={"User-Agent": "Mozilla/5.0"})
                with net_urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                os.replace(tmp, path)
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                time.sleep(1.0 * (attempt + 1))
        if last_err is not None:
            raise RuntimeError(f"离线语音模型下载失败（{rel}）：{last_err}") from last_err
    with open(ok_flag, "w") as f:
        f.write("ok")
    return d

def _load_sherpa_tts():
    """调用方须已持有 _sherpa_lock。"""
    global _sherpa_tts
    if _sherpa_tts is not None:
        return _sherpa_tts
    try:
        import sherpa_onnx
    except Exception as e:
        raise RuntimeError("未安装 sherpa-onnx，请执行：pip install sherpa-onnx") from e
    d = _ensure_sherpa_model().replace("\\", "/")   # 路径用正斜杠，避免被当成转义字符
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=f"{d}/model.onnx", lexicon=f"{d}/lexicon.txt",
                tokens=f"{d}/tokens.txt", dict_dir=f"{d}/dict"),
            provider="cpu", num_threads=4, debug=False),
        rule_fsts=",".join(f"{d}/{f}" for f in ("date.fst", "phone.fst", "number.fst", "new_heteronym.fst")),
        max_num_sentences=1)
    _sherpa_tts = sherpa_onnx.OfflineTts(cfg)
    return _sherpa_tts

# Melo 模型原生语速（speed=1.0）偏快——VITS 类模型训练语料本身偏快是通病，
# 商用 TTS 通常会把默认语速调慢一点。这里给 "+0%" 档位乘上一个 <1 的基准系数，
# 让默认听感更接近正常朗读节奏，用户手动调的档位仍是在这个基准上等比例缩放。
MELO_BASE_SPEED = 0.85

def _rate_to_speed(rate: str) -> float:
    """把 "+10%"/"-25%" 语速档位转成 Sherpa 的 speed（越大越快），在 Melo 偏慢的基准上再缩放。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    return max(0.4, min(3.0, MELO_BASE_SPEED * (1.0 + pct / 100.0)))

# 跨句增益平滑：记录上一句的增益，用于限制相邻句子间的增益跳变幅度。
# 按句序列播放时近似有效（TtsWorker 按 seq 顺序合成），多线程并发合成时仅影响平滑精度，不影响正确性。
_prev_gain_lock = threading.Lock()
_prev_gain = [None]

def _normalize_loudness(samples, target_rms=0.15, max_gain=8.0, peak_ceiling=0.95, max_step_db=3.0, sample_rate=44100):
    """句级响度归一化 + 跨句最终响度平滑，消除句与句之间的忽高忽低。

    Melo/Piper 逐句合成时，每句响度差异很大（实测可达 20~30 dB，短句尤其轻）。
    注意：这里限制的是最终输出 RMS 的跨句跳变幅度，而不是增益本身的跳变幅度——
    因为原始句子间响度差异很大，即使增益本身跳变被限制住，最终听到的音量（=原始RMS×增益）
    仍可能跳变很大；另外 max_gain 封顶会在过强放大时把响度拉不到目标，让该句比前后都轻。
    改为直接限制最终 RMS 相对上一句最终 RMS 的跳变幅度，语义上更直接对应听感。
      target_rms   ：目标 RMS（float 幅度，0.15 ≈ -16.5 dBFS，正常语音响度）
      max_gain     ：放大上限，避免近乎静音的句子被放大成噪音
      peak_ceiling ：峰值上限，放大后峰值不超过该值，避免削波失真
      max_step_db  ：相邻两句最终 RMS 每次最大变化幅度（dB），越小过渡越平滑但越慢追上目标响度
    """
    import numpy as np
    s = np.asarray(samples, dtype=np.float32)
    rms = float(np.sqrt(np.mean(s.astype(np.float64) ** 2)))
    if rms < 1e-4:
        return s                      # 近乎静音，不动（不更新 _prev_gain，避免静音段拉坏后续平滑基准）
    peak = float(np.max(np.abs(s))) if s.size else 0.0

    gain = target_rms / rms
    if peak > 0 and gain * peak > peak_ceiling:
        gain = peak_ceiling / peak    # 峰值保护：优先于 RMS，避免削波
    gain = min(gain, max_gain)        # 静音段不要被无限放大
    target = rms * gain               # 这句自身能达到的最终响度（受 peak_ceiling/max_gain 限制后的实际值）

    with _prev_gain_lock:
        prev_rms = _prev_gain[0]
        if prev_rms is not None and prev_rms > 1e-6:
            step_limit = 10 ** (max_step_db / 20.0)          # dB -> 线性倍数
            ratio = target / prev_rms
            if ratio > step_limit:
                target = prev_rms * step_limit               # 最终响度涨得太快→截住
            elif ratio < 1.0 / step_limit:
                target = prev_rms / step_limit               # 最终响度跌得太快→截住
        _prev_gain[0] = target
    gain = target / rms
    if peak > 0 and gain * peak > peak_ceiling:                 # 平滑后的增益也不能切波
        gain = peak_ceiling / peak
        with _prev_gain_lock:
            _prev_gain[0] = rms * gain                           # 削波保护生效时，同步更新平滑基准，避免下一句按未生效的目标值续跟
    return _apply_edge_fade(s * gain, sample_rate=sample_rate)

def _apply_edge_fade(samples, fade_ms=15, sample_rate=44100):
    """对音频首尾做短淡入淡出，消除每句切换播放源（QMediaPlayer 每句重新 setSourceDevice）
    带来的启动/停止爆音（“嗟”声）。fade_ms 按 44.1kHz 设计，实际调用时会按真实采样率换算。
    15ms 足够消除冲击声，又短到不会影响语音可听度。"""
    import numpy as np
    n = int(sample_rate * fade_ms / 1000)
    if n <= 0 or samples.size < 2 * n:
        return samples
    s = samples.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    s[:n] *= ramp
    s[-n:] *= ramp[::-1]
    return s

def reset_loudness_state():
    """新一次朗读开始前清空跨句增益平滑的状态，避免上一次朗读/上一本书的增益残留影响这次开头几句。"""
    with _prev_gain_lock:
        _prev_gain[0] = None

# Melo 有两种独立的概率性解码缺陷，都会让某段音频幅度异常低，听感上就是那几个字不发音：
#   1. 整句失败：整句 RMS 都极低（尤其是单字句如“不。”，实测失败率可达 ~67%）。
#   2. 句首吸字：整句平均 RMS 正常，但句首前 ~0.25s 异常安静（实测如长句开头是
#      “谢谢，部长先生，那么……”这种，句首——对应“谢谢”两字——被模型吸掉，实测发现有些文本
#      在按 speed 重试时甚至 100% 固定失败，说明这不是随机种子问题，而是文本本身触发的确定性 bug。
# 对策略分两步：
#   第 1 次重试：只扰动 speed（±15%，人耳难以察觉），对「真随机」失败有效且不改变听感。
#   第 2 次重试：若仍失败（说明是文本触发的固定性 bug），改在文本前加一个有实际发音内容的
#      语气词前缀（实测“这个，”最有效，多个测试用例均能从 100%/67% 失败降到 0%），
#      改变音素上下文来规避这个确定性缺陷。代价是听到一个多余的语气词开头，但远比关键内容完全丢失好。
#      注意：纯标点（逗号/句号）完全无效，必须是实词，因为标点本身不产生音素。
# 整句失败样本 rms 在 1e-6~1e-5 量级，正常样本（即使是轻音字）在 1e-3 以上，阈值 1e-4 足够区分。
#
# 句首吞字的判据【2026-10-21 修改】：从绝对阈值 head_rms < 0.015 改为相对占比。
# 原因：Melo 正常发音带 ~50~250ms 起音淡入，加上擦音/鼻音起头（“世上/掍着/满/将军/谢谢”），
# 实测正常句子的 head_rms 落在 0.005~0.017，与真·吞字（0.0027）区间重叠 → 大量误判。
# 误判的代价是每次白跑 3~8 次重合成（实测单句 10~40 秒），听感上就是“读着读着长时间静音”。
# 改判「前 250ms 能量占整句能量的比例」：
#   正常句实测 22 个样本：0.147~0.565
#   真·吞字（“谢谢，部长先生…” 那次提交的复现用例）：0.084
# 阈值 0.12 能把两者分开，且不依赖录音电平（换音色/音量都成立）。
SHERPA_SILENT_RMS_THRESHOLD = 0.0001
SHERPA_HEAD_WINDOW_SEC = 0.25               # 检测句首吞字的窗口长度（秒）
SHERPA_HEAD_RATIO_THRESHOLD = 0.12          # 句首 250ms 能量 / 整句能量 低于此值视为吞噬
SHERPA_SILENT_RETRY = 8                     # 整句伪静音的预算：多是单字/短句（单次 1~3s），多试几次代价小
SHERPA_HEAD_RETRY = 2                       # 句首吞字的预算：多是长句（单次 9~12s），多试一次就是十几秒静音
SHERPA_SILENT_SPEED_JITTER = 0.15           # 每次重试对 speed 的最大随机扰动幅度（按比例）
SHERPA_SPEED_ONLY_RETRY = 3                 # 整句伪静音：前 3 次只扰动 speed，再失败才加前缀
SHERPA_HEAD_PREFIX_AT = 1                   # 句首吞字：第 2 次直接用前缀（speed 扰动对它无效）
SHERPA_FALLBACK_PREFIX = "这个，"            # 触发文本本身的确定性失败时，用这个语气词前缀规避

def _sherpa_detect_bad(g, text):
    """检测一次 Melo 生成结果是否命中已知缺陷。返回 (is_bad, 诊断信息, 类型)。
    类型为 "silent"（整句伪静音）或 "head"（句首吞字），两种缺陷的重试预算不同。"""
    import numpy as np
    samples = np.asarray(g.samples, dtype=np.float64)
    if not samples.size:
        return True, "empty", "silent"
    overall_rms = float(np.sqrt(np.mean(samples ** 2)))
    if overall_rms < SHERPA_SILENT_RMS_THRESHOLD and len(text) > 1:
        return True, f"整句伪静音 rms={overall_rms:.6f}", "silent"
    head_n = int(g.sample_rate * SHERPA_HEAD_WINDOW_SEC)
    if len(samples) > head_n * 2:   # 句子至少要比检测窗口长很多，避免短句被误判
        head_rms = float(np.sqrt(np.mean(samples[:head_n] ** 2)))
        ratio = head_rms / max(overall_rms, 1e-9)
        if ratio < SHERPA_HEAD_RATIO_THRESHOLD:
            return True, f"句首能量占比过低 head/整句={ratio:.3f}（head_rms={head_rms:.5f}）", "head"
    return False, "", ""

def synthesize_tts_sherpa(text: str, rate: str) -> bytes:
    """离线合成（Sherpa-ONNX Melo，中英混读），返回 wav 字节。

    【2026-11 修改】只合成一次，不做任何重试。
    原因：Melo 在本机合成速度约 0.6× 实时（rtf≈1.6），本来就追不上播放；旧逻辑对
    「伪静音/句首吞字」做最多 8 次重合成，每次都卡住唯一的合成线程，把整条流水线
    冻住十几秒，听感就是「两个句子中间长时间停顿（合成）」。按要求：连续优先，
    丢字/吞音可直接忽略，所以这里直接接受单次结果，不再扰动 speed/加前缀/重试。"""
    import numpy as np
    with _sherpa_lock:
        tts = _load_sherpa_tts()
        g = tts.generate(text, sid=0, speed=_rate_to_speed(rate))
    if not len(g.samples):
        raise RuntimeError("Melo 未返回音频")
    samples = _normalize_loudness(g.samples, sample_rate=g.sample_rate)
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(g.sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()

# ---- Kokoro-82M 离线语音（sherpa-onnx 支持，中英混读，103 个说话人）----
# 实测：句间 RMS 波动比 Melo 小（~1~2dB vs Melo 的 ~5~8dB），但中文发音自然度/耐听度
# 明显低于 Melo（sherpa-onnx 绑定走 espeak-ng 做中文音素化，非 Kokoro 官方的 misaki G2P，
# 机械感明显），仅作备选音色，不作默认。模型不是逐文件下载，而是单个 tar.bz2（GitHub Releases），下载后解压。
KOKORO_DIR = "kokoro-int8-multi-lang-v1_1"
KOKORO_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2"
# 中文说话人名 -> sid（完整映射见 sherpa-onnx 文档，zf_ 女声/zm_ 男声），这里只登记项目实际用到的几个
KOKORO_SPEAKERS = {"zf_xiaoxiao": 47, "zf_xiaoyi": 48, "zm_yunxi": 50, "zm_yunjian": 49}
_kokoro_tts = None
_kokoro_lock = threading.Lock()   # 同时保护模型加载和合成调用

def kokoro_model_ready() -> bool:
    dirs = [os.path.join(APP_DIR, "sherpa_models", KOKORO_DIR)]
    b = bundled_models_dir()
    if b:
        dirs.append(os.path.join(b, "sherpa_models", KOKORO_DIR))
    return any(os.path.isfile(os.path.join(d, ".complete")) for d in dirs)

def _ensure_kokoro_model() -> str:
    """确保 Kokoro 模型文件齐全（int8 量化版约 140MB，首次需联网），返回模型目录。"""
    base = os.path.join(APP_DIR, "sherpa_models")
    d = os.path.join(base, KOKORO_DIR)
    ok_flag = os.path.join(d, ".complete")
    if os.path.isfile(ok_flag):
        return d
    b = bundled_models_dir()
    if b:
        bundled = os.path.join(b, "sherpa_models", KOKORO_DIR)
        if os.path.isfile(os.path.join(bundled, ".complete")):
            _log_tts("[离线朗读] 使用随程序附带的 Kokoro 模型（无需联网）")
            return bundled
    _log_tts("[离线朗读] 首次使用，正在下载 Kokoro 模型（约 140MB，需联网一次）")
    os.makedirs(base, exist_ok=True)
    tmp = os.path.join(base, KOKORO_DIR + ".tar.bz2.part")
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(KOKORO_URL, headers={"User-Agent": "Mozilla/5.0"})
            with net_urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            with bz2.BZ2File(tmp) as bz, tarfile.open(fileobj=bz) as tf:
                tf.extractall(base)   # 归档内部已包含 KOKORO_DIR 前缀目录
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    if last_err is not None:
        raise RuntimeError(f"离线语音模型（Kokoro）下载失败：{last_err}") from last_err
    with open(ok_flag, "w") as f:
        f.write("ok")
    return d

def _load_kokoro_tts():
    """调用方须已持有 _kokoro_lock。"""
    global _kokoro_tts
    if _kokoro_tts is not None:
        return _kokoro_tts
    try:
        import sherpa_onnx
    except Exception as e:
        raise RuntimeError("未安装 sherpa-onnx，请执行：pip install sherpa-onnx") from e
    d = _ensure_kokoro_model().replace("\\", "/")
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=f"{d}/model.int8.onnx", voices=f"{d}/voices.bin",
                tokens=f"{d}/tokens.txt", data_dir=f"{d}/espeak-ng-data",
                lexicon=f"{d}/lexicon-us-en.txt,{d}/lexicon-zh.txt"),
            provider="cpu", num_threads=4, debug=False),
        rule_fsts=",".join(f"{d}/{f}" for f in ("date-zh.fst", "phone-zh.fst", "number-zh.fst")),
        max_num_sentences=1)
    _kokoro_tts = sherpa_onnx.OfflineTts(cfg)
    return _kokoro_tts

def _kokoro_rate_to_speed(rate: str) -> float:
    """把 "+10%"/"-25%" 语速档位转成 Kokoro 的 speed（越大越快）。Kokoro 默认语速本身已比
    Melo 更慢、更自然，不套用 Melo 专属的 MELO_BASE_SPEED 减速系数。"""
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    return max(0.4, min(3.0, 1.0 + pct / 100.0))

def synthesize_tts_kokoro(text: str, speaker: str, rate: str) -> bytes:
    """离线合成（Sherpa-ONNX Kokoro-82M，中英混读），返回 wav 字节。
    实测句间響度波动明显小于 Melo，仍保留响度归一化作双保险。"""
    import numpy as np
    sid = KOKORO_SPEAKERS.get(speaker, 47)
    with _kokoro_lock:
        tts = _load_kokoro_tts()
        g = tts.generate(text, sid=sid, speed=_kokoro_rate_to_speed(rate))
    if not len(g.samples):
        raise RuntimeError("Kokoro 未返回音频")
    samples = _normalize_loudness(g.samples, sample_rate=g.sample_rate)
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(g.sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()

# Piper 离线语音：voice id -> Hugging Face piper-voices 仓库里的路径前缀
PIPER_VOICES = {
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
    # 优先使用随程序附带的模型（U 盘/安装包一体分发，无需联网）
    b = bundled_models_dir()
    if b:
        bundled_onnx = os.path.join(b, "piper_models", f"{voice_id}.onnx")
        bundled_cfg = bundled_onnx + ".json"
        if os.path.isfile(bundled_onnx) and os.path.isfile(bundled_cfg):
            return bundled_onnx
    if not (os.path.isfile(onnx_path) and os.path.isfile(cfg_path)):
        _log_tts(f"[离线朗读] 首次使用，正在下载模型：{voice_id}（约 60MB，需联网一次）")
        for path, url in ((onnx_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx"),
                          (cfg_path, f"{PIPER_MODEL_BASE_URL}/{prefix}/{voice_id}.onnx.json")):
            tmp = path + ".part"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with net_urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
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
    """把 "+10%"/"-25%" 语速转成 Piper 的 length_scale（越大越慢）。"""
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

def _normalize_wav_bytes(data: bytes) -> bytes:
    """对 wav 字节里的 PCM 做响度归一化后重写，用于 Piper 等直接输出 wav 的引擎。"""
    import numpy as np
    r = io.BytesIO(data)
    with wave.open(r, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float32) / 32767.0
    samples = _normalize_loudness(samples, sample_rate=sr)
    out = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(sw); w.setframerate(sr)
        w.writeframes(out.tobytes())
    return buf.getvalue()

def synthesize_tts_piper(text: str, voice_id: str, rate: str) -> bytes:
    """离线合成（Piper），返回 wav 字节。"""
    from piper import SynthesisConfig
    voice = _load_piper_voice(voice_id)
    syn_config = SynthesisConfig(length_scale=_piper_rate_to_length_scale(rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return _normalize_wav_bytes(buf.getvalue())

# ---- Windows 系统语音（WinRT SpeechSynthesizer，离线，无需联网/Key）----
# 直接调用 Windows 自带 OneCore 语音（含「设置 → 时间和语言 → 语音」里可添加的自然音色，
# 如晓晓/云希/云扬等）。依赖 winsdk（pip install winsdk）走 WinRT；未安装则该引擎不可用，
# 不影响其它音色。
# 注意：WinRT 对不存在的音色名会「静默回退到系统默认音色」而不是报错，因此：
#   1. 优先用官方 SpeechSynthesizer.AllVoices 枚举（能拿到自然音色），注册表仅作兜底；
#   2. 合成时用 VoiceInformation 直接赋值 synth.voice 选音（按对象/ID 命中），避免 SSML
#      <voice name> 对不存在的音色静默回退到默认音、还误以为是音色本身的问题。
_winrt_voices_cache = None
_winrt_lang = {}
_winrt_ids = {}


def _lcid_to_lang(lcid):
    """把 Windows LCID（十进制或十六进制）映射成 BCP-47 语言标签（够 SSML xml:lang 用）。

    注册表里 LCID 存成十六进制字符串（无 0x 前缀），如 "804"=2052=zh-CN、"409"=1033=en-US。"""
    try:
        s = str(lcid).strip().lower()
        if s.startswith("0x"):
            v = int(s, 16)
        elif s and all(c in "0123456789abcdef" for c in s):
            v = int(s, 16)      # Windows LCID 十六进制字符串（无 0x 前缀）
        else:
            v = int(s)
    except Exception:
        return ""
    return {0x804: "zh-CN", 0x404: "zh-TW", 0x409: "en-US", 0x809: "en-GB",
            0x411: "ja-JP", 0x412: "ko-KR", 0x40c: "fr-FR", 0x407: "de-DE",
            0xc0a: "es-ES", 0x410: "it-IT", 0x419: "ru-RU"}.get(v, "")


def _winrt_all_voices():
    """枚举 WinRT SpeechSynthesizer.AllVoices（官方接口），返回 [(显示名, 语言标签, id), ...]。

    该接口能返回 OneCore 语音，也包含「设置 → 时间和语言 → 语音 → 添加语音」安装的自然音色
    （显示名形如 "Microsoft Xiaoxiao Online (Natural) - Chinese (Mainland)"），是官方推荐的
    枚举方式；注册表枚举（_winrt_registry_voices）只作兜底。"""
    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    voices = SpeechSynthesizer.all_voices
    out = []
    if voices is None:
        return out
    for i in range(voices.size):
        v = voices.get_at(i)
        out.append((v.display_name, v.language, v.id))
    return out


def _winrt_registry_voices():
    """枚举已安装的 Windows OneCore 语音，返回 [(显示名, 语言标签), ...]。"""
    import winreg
    out = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens")
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    token = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                name, lang = token, ""
                try:
                    ak = winreg.OpenKey(key, token + r"\Attributes")
                    try:
                        v = winreg.QueryValueEx(ak, "Name")[0]
                        if isinstance(v, str) and v.strip():
                            name = v
                    except OSError:
                        pass
                    try:
                        lang = _lcid_to_lang(winreg.QueryValueEx(ak, "Language")[0])
                    except OSError:
                        pass
                    winreg.CloseKey(ak)
                except OSError:
                    pass
                out.append((name, lang))
        finally:
            winreg.CloseKey(key)
    seen, dedup = set(), []
    for n, l in out:
        if n not in seen:
            seen.add(n)
            dedup.append((n, l))
    return dedup


def winrt_voices():
    """返回可用的 Windows 系统语音 [(label, voice_key), ...]；无 winsdk 或无 OneCore 音色时返回 []。

    voice_key 形如 "winrt:Microsoft Huihui"；自然音色形如
    "winrt:Microsoft Xiaoxiao Online (Natural) - Chinese (Mainland)"。
    优先用 AllVoices 枚举（含自然音色），失败再退回注册表；首次调用后缓存。"""
    global _winrt_voices_cache
    if _winrt_voices_cache is not None:
        return _winrt_voices_cache
    voices = []
    try:
        voices = _winrt_all_voices()
    except Exception:
        voices = []
    if not voices:
        # 兜底：读注册表（只有经典 OneCore 音色，看不到自然音色，但至少可用）
        try:
            voices = [(n, l, "") for n, l in _winrt_registry_voices()]
        except Exception:
            voices = []
    result = []
    for n, l, vid in voices:
        _winrt_lang[n] = l
        if vid:
            _winrt_ids[n] = vid
        label = ("%s（%s·系统离线）" % (n, l)) if l else ("%s（系统离线）" % n)
        result.append((label, "winrt:" + n))
    _winrt_voices_cache = result
    return result


def _winrt_find_voice(voice_name: str):
    """按显示名/ID/描述名在 AllVoices 里找到对应 VoiceInformation；找不到返回 None。

    用 VoiceInformation 直接赋值 synth.voice 是最可靠的选音方式（按对象/ID 命中，不是按名字猜），
    自然音色（"Microsoft Xiaoxiao Online (Natural) - ..."）也能正确命中。"""
    try:
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
        voices = SpeechSynthesizer.all_voices
        if voices is None:
            return None
        for i in range(voices.size):
            v = voices.get_at(i)
            if v.display_name == voice_name or v.id == voice_name or v.description == voice_name:
                return v
    except Exception:
        return None
    return None


def synthesize_tts_winrt(text: str, voice_name: str, rate: str) -> bytes:
    """Windows 系统语音离线合成，返回 wav 字节。voice_name 为系统音色显示名（含自然音色）。"""
    from xml.sax.saxutils import escape
    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    from winsdk.windows.storage.streams import DataReader
    winrt_voices()   # 确保 _winrt_lang 已填充（内部缓存，二次调用开销≈0）
    lang = _winrt_lang.get(voice_name, "zh-CN")
    lang_attr = (" xml:lang='%s'" % lang) if lang else ""
    vi = _winrt_find_voice(voice_name)   # 自然音色也能按显示名命中
    if vi is not None:
        # 选音交给 synth.voice（按对象/ID 命中），SSML 只负责语速，避免 <voice name> 静默回退
        ssml = ("<speak version='1.0'%s xmlns='http://www.w3.org/2001/10/synthesis'>"
                "<prosody rate='%s'>%s</prosody></speak>"
                % (lang_attr, rate, escape(text)))
    else:
        # 找不到对应 VoiceInformation（如旧配置残留）→ 退回 SSML <voice name>；若仍不存在会静默用默认音
        ssml = ("<speak version='1.0'%s xmlns='http://www.w3.org/2001/10/synthesis'>"
                "<voice name='%s'><prosody rate='%s'>%s</prosody></voice></speak>"
                % (lang_attr, escape(voice_name), rate, escape(text)))

    async def _go():
        synth = SpeechSynthesizer()
        if vi is not None:
            synth.voice = vi
        stream = await synth.synthesize_ssml_to_stream_async(ssml)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        data = bytearray(stream.size)
        reader.read_bytes(data)
        return bytes(data)
    return asyncio.run(_go())

def split_sentences(text: str, base: int = 0):
    """把文本切成句子：返回 [(起始偏移, 结束偏移, 句子文本)]，偏移为全局字符偏移。

    断句规则（按优先级）：
      1. 中文标点 。！？；  / 换行  → 立即断
      2. 英文句末：'. '/'! '/'? ' 后接大写或数字 → 断在空格后
      3. 超长兜底（120字符）→ 向后找最近空格断，避免从单词中间截断
    """
    res = []
    seg_start = 0
    i = 0
    n = len(text)
    MAX_LEN = 120   # 兜底截断阈值（字符数）

    while i < n:
        ch = text[i]
        buf_len = i - seg_start + 1

        # 规则1：中文标点 / 换行 → 立即断
        if ch in "。！？；" or ch == "\n":
            end = i + 1
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            i = end
            continue

        # 规则2：英文句末标点（. ! ?）后接空格+大写/数字 → 断在标点后，空格归下一句
        if ch in ".!?" and i + 2 < n and text[i + 1] == " " and (text[i + 2].isupper() or text[i + 2].isdigit()):
            end = i + 2          # 断在空格，下一句从大写字母开始（空格被包含进当前句，strip 后不影响 TTS）
            seg = text[seg_start:end]
            if seg.strip():
                res.append((base + seg_start, base + end, seg))
            seg_start = end
            i = end
            continue

        # 规则3：超长兜底 → 向后找最近空格，找不到就硬断
        if buf_len >= MAX_LEN:
            # 向后最多扫 20 字符找空格
            cut = i + 1
            for j in range(i + 1, min(i + 21, n)):
                if text[j] == " ":
                    cut = j + 1   # 断在空格之后（空格归下一句开头，strip 掉）
                    break
            seg = text[seg_start:cut]
            if seg.strip():
                res.append((base + seg_start, base + cut, seg))
            seg_start = cut
            i = cut
            continue

        i += 1

    if seg_start < n:
        seg = text[seg_start:n]
        if seg.strip():
            res.append((base + seg_start, base + n, seg))
    return res

# Azure Speech（REST，无需 SDK）。账号 0 为主、1 为备用；Key/区域由设置对话框写入，
# 主账号也可用环境变量 AZURE_SPEECH_KEY / AZURE_SPEECH_REGION。
_azure_accounts = [{"key": "", "region": ""}, {"key": "", "region": ""}]
_azure_skip_until = [0.0, 0.0]   # 账号额度用尽/认证失败后，暂时跳过的截止时间（秒级时间戳）
AZURE_FREE_QUOTA = 500_000       # F0 每月免费字符数
AZURE_SKIP_SECONDS = 600

def _norm_region(region: str) -> str:
    """接受 'eastus' 或完整端点 URL（https://eastus.api.cognitive.microsoft.com/），统一成区域名。"""
    import re
    r = (region or "").strip().lower()
    m = re.match(r"https?://([a-z0-9-]+)\.", r)
    return m.group(1) if m else r

def set_azure_config(key: str, region: str, key2: str = "", region2: str = ""):
    _azure_accounts[0] = {"key": (key or "").strip(), "region": _norm_region(region)}
    _azure_accounts[1] = {"key": (key2 or "").strip(), "region": _norm_region(region2)}
    _azure_skip_until[0] = _azure_skip_until[1] = 0.0

def _azure_usage_add(idx: int, text: str):
    """按 Azure 计费口径累计各账号本月字符数（中日韩字符每个算 2），接近免费额度时写日志告警。"""
    import json
    billed = sum(2 if ("⺀" <= c <= "鿿" or "가" <= c <= "힯" or "＀" <= c <= "￯") else 1
                 for c in text)
    path = os.path.join(APP_DIR, "azure_usage.json")
    month = time.strftime("%Y-%m")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cur = data.get(month, {})
    if not isinstance(cur, dict):          # 兼容旧格式 {"2026-10": 51}
        cur = {"0": cur}
    used = cur.get(str(idx), 0) + billed
    cur[str(idx)] = used
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({month: cur}, f)
    except Exception:
        pass
    if used >= AZURE_FREE_QUOTA * 0.8:
        _log_tts(f"[Azure] 账号{idx}本月已用约 {used} / {AZURE_FREE_QUOTA} 字符（免费额度的 {used * 100 // AZURE_FREE_QUOTA}%）")

class _AzureAccountError(Exception):
    """账号级错误（认证失败 / 额度用尽 / 持续限流），应切换到下一个账号。"""

# Azure 输出格式：用 48kbps mp3（≈ 6KB/s）而不是 24kHz PCM（48KB/s）。
# 起因：2026-10-17 为了修“新播放器放不了 mp3”把格式改成 riff-24khz-16bit-mono-pcm，
# 代价是每句要传的数据量×8（实测同一句57字：mp3 86KB vs WAV 689KB）。而本机到
# eastus 的链路在传输 ~100KB / 6~10 秒时会被重置（IncompleteRead），于是长句全部
# 被掐在句首→3 次重试全废→整句跳过→整章无声。改用 mp3 再本地解码成 PCM，
# 既能躲开该窗口，播放层仍然只处理 WAV/PCM（不需要改 TtsPlayer）。
AZURE_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
AZURE_PCM_RATE = 24000
# 链路被掐断时，已经收到的 mp3 仍然能解出前段音频（mp3 可流式解码），
# 宁可句尾缺几个字，也好过整句被跳过完全没声音。
AZURE_SALVAGE_MIN_BYTES = 4096


def _pcm_to_wav(pcm: bytes, sample_rate: int = AZURE_PCM_RATE) -> bytes:
    """把单声道 int16 PCM 打包成 wav 字节（与 TtsPlayer 期望的格式一致）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _mp3_to_wav(data: bytes, sample_rate: int = AZURE_PCM_RATE) -> bytes:
    """用 Qt 的 QAudioDecoder 把 mp3 解成单声道 int16 PCM（再包成 wav）。

    调用方可能处于普通 Python 工作线程（TtsWorker），因此这里自建一个 QEventLoop
    等待解码完成；QAudioDecoder 只在该线程内创建和使用，不跨线程。"""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QEventLoop, QTimer
    from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

    if not data:
        raise RuntimeError("空的 mp3 数据")
    src = QBuffer()
    src.setData(QByteArray(data))
    src.open(QIODevice.OpenModeFlag.ReadOnly)
    dec = QAudioDecoder()
    fmt = QAudioFormat()
    fmt.setSampleRate(sample_rate)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    dec.setAudioFormat(fmt)      # 强制解成播放层想要的格式，后续不需要重采样
    dec.setSourceDevice(src)

    pcm = bytearray()
    loop = QEventLoop()

    def _drain():
        b = dec.read()
        if b.isValid():
            pcm.extend(bytes(b.constData()))

    dec.bufferReady.connect(_drain)
    dec.finished.connect(loop.quit)
    dec.error.connect(lambda _e: loop.quit())
    guard = QTimer()                     # 解码卡住时保底退出（用完显式 stop，避免回调悬空）
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(30_000)
    dec.start()
    loop.exec()
    guard.stop()
    try:
        _drain()                             # 收尾：最后一块
        dec.stop()
    except Exception:
        pass
    if not pcm:
        raise RuntimeError(f"mp3 解码失败（{dec.errorString() or '无 PCM 输出'}）")
    return _pcm_to_wav(bytes(pcm), sample_rate)


def _azure_call(idx: int, acct: dict, voice_name: str, text: str, rate: str) -> bytes:
    import re
    import urllib.error
    from xml.sax.saxutils import escape
    key, region = acct["key"], acct["region"]
    if not re.fullmatch(r"[a-z0-9-]+", region):   # region 会拼进主机名，Key 会发往该主机，必须严格校验
        raise RuntimeError(f"Azure 区域格式不正确：{region!r}")
    lang = "-".join(voice_name.split("-")[:2])
    ssml = (f"<speak version='1.0' xml:lang='{lang}' xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{voice_name}'><prosody rate='{rate}'>{escape(text)}</prosody></voice></speak>")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT, "User-Agent": "PyReader"}
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=ssml.encode("utf-8"), headers=headers, method="POST")
            with net_urlopen(req, timeout=30) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError("Azure 未返回音频")
            _azure_usage_add(idx, text)
            return _mp3_to_wav(data)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise _AzureAccountError(f"认证失败或额度用尽（HTTP {e.code}）") from e
            last_err = e
        except http.client.IncompleteRead as e:
            # 链路在传输中途被掐断：已收到的 mp3 前缀仍可解码，直接用它，避免整句丢失
            partial = e.partial or b""
            if len(partial) >= AZURE_SALVAGE_MIN_BYTES:
                try:
                    wav = _mp3_to_wav(partial)
                    _azure_usage_add(idx, text)
                    _log_tts(f"[Azure截断] 只收到 {len(partial)}B（第{attempt + 1}次），"
                             f"用已收到的前段音频代替（句尾可能缺几个字）：{text[:30]!r}")
                    return wav
                except Exception as e2:
                    last_err = e2
            else:
                last_err = e
        except Exception as e:
            last_err = e
        _log_tts(f"[Azure重试] 账号{idx} 第{attempt + 1}次失败（原文前30字：{text[:30]!r}）：{last_err}")
        time.sleep(1.0 * (attempt + 1))
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 429:
        raise _AzureAccountError("持续被限流（HTTP 429）") from last_err
    raise RuntimeError(f"Azure 语音合成失败（已重试3次）：{last_err}")

def _azure_synthesize_once(text: str, voice_name: str, rate: str) -> bytes:
    """单次 Azure 合成（含账号切换 / edge-tts 回退），返回 wav 字节。
    主账号认证失败/额度用尽/持续限流时自动切换备用账号，出问题的账号会被暂时跳过一段时间，
    避免每句都先撞一次错误。所有账号都不可用时，自动回退到 edge-tts
    （同样的 Neural 音色名，免费但偶发因微软接口变更而失效，仅作补充；它的 mp3 也会同样解码成 wav）。"""
    accts = [dict(a) for a in _azure_accounts]
    if not accts[0]["key"]:
        accts[0] = {"key": os.environ.get("AZURE_SPEECH_KEY", "").strip(),
                    "region": _norm_region(os.environ.get("AZURE_SPEECH_REGION", ""))}
    cand = [i for i, a in enumerate(accts) if a["key"] and a["region"]]
    errors = []
    if not cand:
        errors.append("未配置 Azure Speech Key/区域")
    else:
        now = time.time()
        order = [i for i in cand if _azure_skip_until[i] <= now] or cand   # 都在冷却期则全部再试一遍
        for i in order:
            try:
                return _azure_call(i, accts[i], voice_name, text, rate)
            except _AzureAccountError as e:
                _azure_skip_until[i] = time.time() + AZURE_SKIP_SECONDS
                _log_tts(f"[Azure] 账号{i} 不可用，{AZURE_SKIP_SECONDS // 60} 分钟内跳过：{e}")
                errors.append(f"账号{i}：{e}")
    try:
        _log_tts(f"[Azure] 所有账号不可用，回退到 edge-tts（{voice_name}）")
        return _mp3_to_wav(synthesize_tts_edge(text, voice_name, rate))   # edge-tts 返回 mp3，同样解码成 wav
    except Exception as e:
        errors.append(f"edge-tts 回退也失败：{e}")
    raise RuntimeError("Azure Speech 所有账号均不可用，edge-tts 回退也失败（" + "；".join(errors) + "）")


# Azure 长文本切块：本机到 eastus 的链路会在传输 ~100KB 时被重置，而 mp3 约 6KB/s，
# 也就是单个连接大约只能传完 14~16 秒音频。超过 40 字的句子切块合成，
# 单块响应落在窗口内；即使某块被掐断（IncompleteRead），损失也只限于那一块的后半句，
# 不会像以前那样整句被跳过导致整章无声。
AZURE_CHUNK_CHARS = 40
AZURE_CHUNK_BREAKS = "， 。、； ： ！ ？ ,.!?;:"


def _split_for_azure(text: str, limit: int = AZURE_CHUNK_CHARS):
    """按标点把长句切成 ≤limit 字的块（找不到标点就硬切），保留原文顺序与标点。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= limit and ch in AZURE_CHUNK_BREAKS:
            chunks.append(cur); cur = ""
    if cur:
        chunks.append(cur)
    # 某块仍超长（整段没有标点）→ 硬切
    out = []
    for c in chunks:
        while len(c) > limit * 2:
            out.append(c[:limit]); c = c[limit:]
        if c.strip():
            out.append(c)
    return out


def synthesize_tts_azure(text: str, voice_name: str, rate: str) -> bytes:
    """Azure Speech 云端合成，返回 wav 字节（内部拿 mp3 再本地解码成 PCM，见 _mp3_to_wav）。

    长句会先切块再逐块合成，最后把 PCM 拼成一条音频（同采样率同格式，拼接无缝）。
    单块失败不会拖垮整句：用已成功的块继续，只缺那一块。"""
    chunks = _split_for_azure(text)
    if not chunks:
        # 归一化后只剩空白（纯标点/引号）：返回一段极短静音，不要抛错中断整章朗读
        return _pcm_to_wav(b"\x00\x00" * (AZURE_PCM_RATE // 100), AZURE_PCM_RATE)
    if len(chunks) == 1:
        return _azure_synthesize_once(chunks[0], voice_name, rate)

    pcm = bytearray()
    rate_hz = None
    failed = 0
    for i, part in enumerate(chunks):
        try:
            wav = _azure_synthesize_once(part, voice_name, rate)
        except Exception as e:
            failed += 1
            _log_tts(f"[Azure分块] 第{i + 1}/{len(chunks)}块失败，跳过：{e}（{part[:20]!r}）")
            continue
        with wave.open(io.BytesIO(wav), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            data = w.readframes(w.getnframes())
        if sr != AZURE_PCM_RATE or ch != 1 or sw != 2:
            # 罕见：解码器返回的格式不是预期的 24kHz/mono/int16 → 不做拼接，直接返回该块
            return wav
        rate_hz = sr
        pcm += data
    if not pcm:
        raise RuntimeError(f"Azure 分块合成都失败（{failed}/{len(chunks)} 块）")
    if failed:
        _log_tts(f"[Azure分块] {len(chunks)} 块中 {failed} 块失败，其余已拼接")
    return _pcm_to_wav(bytes(pcm), rate_hz or AZURE_PCM_RATE)

_edge_tts_skip_until = 0.0   # edge-tts 端点被封/403 后，暂时跳过的截止时间（秒级时间戳），避免每句都先撞一次错误
EDGE_TTS_SKIP_SECONDS = 600

def synthesize_tts_edge(text: str, voice_name: str, rate: str) -> bytes:
    """edge-tts 云端合成（微软 Edge 朗读非官方接口），返回 mp3 字节。免费、无额度限制，
    但依赖微软内部接口，偶发因对方改鉴权方式而失效（如 2026-10 的 403，需 edge-tts>=7.3.0 修复）。
    仅作为 Azure 额度用尽/账号不可用时的补充，不作为用户可选的主力音色。"""
    global _edge_tts_skip_until
    try:
        import edge_tts
    except Exception as e:
        raise RuntimeError("未安装 edge-tts，请执行：pip install edge-tts") from e
    now = time.time()
    if _edge_tts_skip_until > now:
        raise RuntimeError(f"edge-tts 处于冷却期（还剩 {int(_edge_tts_skip_until - now)} 秒）")
    # Azure 的 rate（如 "+10%"）与 edge-tts 的 rate 字符串格式一致，可直接复用
    async def go():
        com = edge_tts.Communicate(text, voice_name, rate=rate)
        out = []
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                out.append(chunk["data"])
        if not out:
            raise RuntimeError("edge-tts 未返回音频")
        return b"".join(out)
    last_err = None
    for attempt in range(3):
        try:
            return asyncio.run(go())
        except Exception as e:
            last_err = e
            _log_tts(f"[edge-tts重试] 第{attempt + 1}次失败（原文前30字：{text[:30]!r}）：{e}")
            time.sleep(0.6 * (attempt + 1))
    _edge_tts_skip_until = time.time() + EDGE_TTS_SKIP_SECONDS   # 连续失败多半是端点被封，冷却一段时间再试
    raise RuntimeError(f"edge-tts 合成失败（已重试3次）：{last_err}")

# 离线引擎（Melo/Piper）词表里没有的标点/符号 → 换成有停顿的等价标点或删除。
# 否则这些字符会被当成 OOV 静默丢弃（如 '…'），导致该有的停顿消失、语句粘连。
# 注意键的顺序：先匹配更长的（“……”→“，”在“…”→“，”之前，“——”在“—”之前）。
_TTS_PUNCT_REPLACE = {
    "……": "，", "…": "，", "‥": "，", "⋯": "，",
    "——": "，", "―": "，", "—": "，", "–": "，",
    "～": "，", "~": "，",
    "：": "，", "；": "，",   # 全角冒号/分号：Melo 词表里没有，不换成停顿会被静默丢弃导致语句粘连
    ":": "，", ";": "，",       # 半角冒号/分号（中文文本里混用半角标点常见），同样是 OOV
    "“": "", "”": "", "‘": "", "’": "",
    '"': "", "'": "",         # ASCII 直引号（很多 txt 小说文本用这种而不是中文弯引号），同样是 OOV，
                              # 且连续多个直引号会让整句只剩下引号本身，触发 Melo 整句转换失败（Failed to convert）
    "·": " ",            # 外国人名中点 → 空格（如 阿加莎·克里斯蒂）
    "《": "", "》": "", "〈": "", "〉": "",
    "（": " ", "）": " ", "【": " ", "】": " ", "〔": " ", "〕": " ",
    "℃": "度", "℉": "度",      # 摄氏度/华氏度符号 → “度”，保留语义而不是直接丢字
    "±": "", "°": "度", "※": "，", "•": "，", "′": "", "″": "",
    # 半角符号/标点：Melo 词表不认，全部会被静默丢弃
    "/": " ", "\\": " ", "|": "，", "@": " ", "#": " ", "$": " ",
    "%": " ", "^": " ", "&": " ", "*": " ",
}

def _normalize_tts_text(text: str) -> str:
    """把离线 TTS 不认识的标点替换成等价标点/删除，避免被静默丢弃导致停顿消失。"""
    for k, v in _TTS_PUNCT_REPLACE.items():
        text = text.replace(k, v)
    return text

# ---- SAPI5 语音（含 NaturalVoiceSAPIAdapter 桥接的自然语音）----
# Windows 自然语音（晓晓/云希等）是 AppX 包，只对讲述人等系统 App 可见，普通桌面程序走
# WinRT AllVoices / SAPI 令牌都枚举不到。装 NaturalVoiceSAPIAdapter
# （github.com/gexgd0419/NaturalVoiceSAPIAdapter，Release zip → 解压 → 管理员跑 Installer.exe）
# 后，自然语音会以 SAPI5 音色出现，这里走 SAPI5（comtypes + SAPI.SpVoice）调用。
_sapi_voices_cache = None


def sapi_voices():
    """枚举 SAPI5 音色，返回 [(显示名, "sapi:"+显示名), ...]。
    未装适配器时只有系统经典 SAPI 音色；装好后自然语音会一并出现。"""
    global _sapi_voices_cache
    if _sapi_voices_cache is not None:
        return _sapi_voices_cache
    out = []
    try:
        import comtypes.client
        v = comtypes.client.CreateObject("SAPI.SpVoice")
        toks = v.GetVoices()
        for i in range(toks.Count):
            t = toks.Item(i)
            out.append((t.GetDescription(), "sapi:" + t.GetDescription()))
    except Exception as e:
        _log_tts(f"[SAPI5] 枚举音色失败：{e}")
    _sapi_voices_cache = out
    return out


def synthesize_tts_sapi(text: str, voice_name: str, rate: str) -> bytes:
    """SAPI5 合成（含 NaturalVoiceSAPIAdapter 的自然语音），输出 wav 字节。"""
    import tempfile
    import comtypes.client
    v = comtypes.client.CreateObject("SAPI.SpVoice")
    toks = v.GetVoices()
    for i in range(toks.Count):
        t = toks.Item(i)
        if t.GetDescription() == voice_name:
            v.Voice = t
            break
    try:
        pct = int(str(rate).strip().replace("%", ""))
    except Exception:
        pct = 0
    v.Rate = max(-10, min(10, int(round(pct / 10.0))))   # SAPI 语速 -10..10，0 为默认
    tmp = os.path.join(tempfile.gettempdir(),
                       f"pyreader_sapi_{os.getpid()}_{threading.get_ident()}.wav")
    try:
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Open(tmp, 3)   # SSFMCreateForWrite
        v.AudioOutputStream = stream
        v.Speak(text)         # 阻塞直到合成完成
        stream.Close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def synthesize_tts(text: str, voice: str, rate: str) -> bytes:
    """把一段文本合成音频字节。voice 以 "kokoro:" 走本地 Kokoro-82M 离线合成（wav，中英混读，推荐），
    以 "sherpa:" 走本地 Melo 离线合成（wav，中英混读），以 "piper:" 开头走本地 Piper 离线合成
    （wav，仅英文），以 "winrt:" 走 Windows 系统语音离线合成（wav），以 "sapi:" 走 SAPI5
    （含 NaturalVoiceSAPIAdapter 桥接的自然语音），以 "azure:" 走 Azure Speech 云端合成（mp3）。"""
    if voice.startswith("azure:"):
        return synthesize_tts_azure(text, voice[len("azure:"):], rate)
    if voice.startswith("winrt:"):
        return synthesize_tts_winrt(text, voice[len("winrt:"):], rate)
    if voice.startswith("sapi:"):
        return synthesize_tts_sapi(text, voice[len("sapi:"):], rate)
    text = _normalize_tts_text(text)   # 离线引擎：先归一化标点，避免 OOV 字符被静默丢弃
    if not text.strip():
        # 归一化后只剩空白（如原句全是引号/标点，被替换成空字符串后只剩换行符），离线引擎
        # 会报 "Failed to convert" 而不是返回音频，这里直接返回一段极短静音，避免中断朗读。
        import struct
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(struct.pack("<h", 0) * 100)   # ~2.3ms 静音
        return buf.getvalue()
    if voice.startswith("kokoro:"):
        return synthesize_tts_kokoro(text, voice[len("kokoro:"):], rate)
    if voice.startswith("sherpa:") or voice == "piper:zh_CN-huayan-medium":   # 旧配置里的 Piper 中文音色迁移到 Melo
        return synthesize_tts_sherpa(text, rate)
    if voice.startswith("piper:"):
        return synthesize_tts_piper(text, voice[len("piper:"):], rate)

    raise RuntimeError(f"未知的朗读音色：{voice}")

def _log_tts(msg):
    """把朗读相关事件写入 ~/.pyreader/tts.log。"""
    try:
        with open(os.path.join(APP_DIR, "tts.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

class TtsWorker(QObject):
    ready = Signal(int, bytes)     # (句子序号, 音频字节；Azure/edge-tts 已解码为 wav，离线引擎本来就是 wav)
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
