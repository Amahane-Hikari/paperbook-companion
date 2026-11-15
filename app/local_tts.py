"""本地免费 TTS：基于 edge-tts，输出 PCM WAV 以兼容现有 wave 拼接逻辑。

对外只暴露 synthesize()，请求/返回结构与原远程 TTS 无关，由调用方保持原有 API 契约。
"""
import asyncio
import os
import subprocess
import tempfile
import time

import edge_tts
import imageio_ffmpeg

# 前端音色 ID -> Edge TTS 音色
VOICE_MAP = {
    "xiaoyuan": "zh-CN-XiaoxiaoNeural",
    "xiaosi": "zh-CN-XiaoyiNeural",
    "niudun": "zh-CN-YunjianNeural",
    "oula": "zh-CN-YunyangNeural",
    "M001": "zh-CN-YunxiNeural",
    "F001": "zh-CN-XiaoxiaoNeural",
    # README 中的旧音色 ID
    "xiaomei": "zh-CN-XiaoxiaoNeural",
    "xiaogang": "zh-CN-YunxiNeural",
    "xiaoyun": "zh-CN-XiaoxiaoNeural",
    "xiaoming": "zh-CN-YunyangNeural",
    "xiaoxi": "zh-CN-XiaoyiNeural",
    "laozhang": "zh-CN-YunjianNeural",
    # 英文音色
    "en_thinkie": "en-US-AnaNeural",
    "en_F001": "en-US-JennyNeural",
    "en_F002": "en-US-AriaNeural",
    "en_xiaosi": "en-US-MichelleNeural",
}

DEFAULT_CN_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_EN_VOICE = "en-US-AriaNeural"


def resolve_edge_voice(voice: str, lang: str = "cn") -> str:
    if voice in VOICE_MAP:
        return VOICE_MAP[voice]
    if lang == "en":
        return DEFAULT_EN_VOICE
    return DEFAULT_CN_VOICE


def _mp3_to_wav(mp3_path: str, wav_path: str):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", mp3_path,
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败: {result.stderr.strip()}")


def synthesize(text: str, voice: str, output_path: str = None,
               lang: str = "cn", rate: str = "+10%") -> str:
    """用 edge-tts 合成语音并转成 24kHz 单声道 WAV，返回输出文件路径。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("文本不能为空")

    if not output_path:
        from config import TARGET_DIR
        output_dir = os.path.join(TARGET_DIR, "history_res")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(
            output_dir, f"tts_{int(time.time() * 1000)}_{voice}.wav"
        )

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    edge_voice = resolve_edge_voice(voice, lang)
    fd, mp3_path = tempfile.mkstemp(prefix="edge_tts_", suffix=".mp3")
    os.close(fd)
    try:
        communicate = edge_tts.Communicate(text, edge_voice, rate=rate)
        asyncio.run(communicate.save(mp3_path))
        _mp3_to_wav(mp3_path, output_path)
    finally:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)

    return output_path
