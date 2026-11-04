# -*- coding: utf-8 -*-
"""列出本机 Windows 语音（含讲述人自然语音），用于验证 Reader 能否用自然音色。

用法（在项目根目录）：
    python tools/check_voices.py

若某音色名里带 "Online (Natural)"（如 "Microsoft Xiaoxiao Online (Natural) - Chinese (Mainland)"），
说明已装上讲述人自然语音，Reader 的音色下拉里也会出现、可离线朗读。
可选加一个音色显示名做参数，直接合成一句测试音：
    python tools/check_voices.py "Microsoft Xiaoxiao Online (Natural) - Chinese (Mainland)"
"""
import sys

def main():
    natural = []
    try:
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
        voices = SpeechSynthesizer.all_voices
        if voices is None:
            print("[!] winsdk 未安装或 AllVoices 返回空")
            return 1
        print("== WinRT SpeechSynthesizer.AllVoices（Reader 实际用的枚举）共 %d 个 ==" % voices.size)
        for i in range(voices.size):
            v = voices.get_at(i)
            is_nat = "Online (Natural)" in (v.display_name or "") or "Natural" in (v.display_name or "")
            if is_nat:
                natural.append(v)
            tag = "  << 自然音色" if is_nat else ""
            print("  %-60s | %s | %s%s" % (v.display_name, v.language, v.id, tag))
    except Exception as e:
        print("[!] 枚举失败：%r" % (e,))
        return 1

    print()
    if not natural:
        print("未发现自然音色。说明：Windows「讲述人自然语音」（晓晓/云希/云扬等）是讲述人专用")
        print("的独立语音系统，不走 Speech_OneCore，不会出现在上面的列表里，Reader 也调不到。")
        print("要用它们需借助第三方 NaturalVoiceSAPIAdapter（SAPI 通道）；离线自然中文推荐 Melo。")
        return 0

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        target = natural[0].display_name
    print("合成测试：%s" % target)
    try:
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
        from winsdk.windows.storage.streams import DataReader
        import asyncio

        vi = None
        for i in range(SpeechSynthesizer.all_voices.size):
            v = SpeechSynthesizer.all_voices.get_at(i)
            if v.display_name == target:
                vi = v
                break
        if vi is None:
            print("[!] 未找到音色：%s" % target)
            return 1

        async def go():
            synth = SpeechSynthesizer()
            synth.voice = vi
            ssml = ("<speak version='1.0' xml:lang='%s' xmlns='http://www.w3.org/2001/10/synthesis'>"
                    "<prosody rate='+0%'>你好，这是自然语音测试。</prosody></speak>" % vi.language)
            st = await synth.synthesize_ssml_to_stream_async(ssml)
            r = DataReader(st.get_input_stream_at(0))
            await r.load_async(st.size)
            d = bytearray(st.size)
            r.read_bytes(d)
            return bytes(d)

        data = asyncio.run(go())
        out = "natural_voice_test.wav"
        with open(out, "wb") as f:
            f.write(data)
        print("OK，已写出 %s（%d 字节）" % (out, len(data)))
    except Exception as e:
        print("[!] 合成失败：%r" % (e,))
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
