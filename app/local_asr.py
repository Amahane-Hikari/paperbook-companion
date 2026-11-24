"""Local ASR; use installed weights unless downloading was explicitly enabled."""
import os
import threading
import wave

import numpy as np

# 国内网络默认走镜像；已在环境中显式设置时不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_model = None
_model_lock = threading.Lock()


def _model_candidate():
    """返回本地模型路径（若配置的是目录则展开为绝对路径）。"""
    model_size = os.environ.get("WHISPER_MODEL", "models/faster-whisper-small")
    candidate = os.path.expanduser(model_size)
    if not os.path.isabs(candidate):
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), candidate)
    return candidate


def available() -> bool:
    """本地识别是否可用：依赖已安装，且模型已就位或允许自动下载。"""
    try:
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is None:
            return False
    except (ImportError, ValueError):
        return False
    return os.path.isdir(_model_candidate()) or os.environ.get(
        "WHISPER_ALLOW_DOWNLOAD", "false").lower() == "true"


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        model_size = os.environ.get("WHISPER_MODEL", "models/faster-whisper-small")
        candidate = os.path.expanduser(model_size)
        if not os.path.isabs(candidate):
            candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), candidate)
        if os.path.isdir(candidate):
            model_size = candidate
        elif os.environ.get('WHISPER_ALLOW_DOWNLOAD', 'false').lower() != 'true':
            raise RuntimeError('ASR model is not installed; configure WHISPER_MODEL or explicitly enable WHISPER_ALLOW_DOWNLOAD')
        device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        return _model


def _load_wav_16k(path: str) -> np.ndarray:
    """读取 WAV 并转成 16kHz 单声道 float32，避免依赖 PyAV 解码。"""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    audio = samples.astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if sample_rate != 16000 and audio.size:
        target_len = int(audio.size * 16000 / sample_rate)
        src = np.linspace(0, audio.size - 1, audio.size)
        dst = np.linspace(0, audio.size - 1, target_len)
        audio = np.interp(dst, src, audio).astype(np.float32)
    return audio


def _to_simplified(text: str) -> str:
    """Whisper 的中文常输出繁体，统一转成简体，避免问答检索匹配不上。"""
    try:
        import zhconv
        return zhconv.convert(text, 'zh-cn')
    except ImportError:
        return text


def transcribe(audio_path: str, language: str = None) -> str:
    """识别音频文件，返回纯文本。"""
    model = _get_model()
    if language is None:
        language = os.environ.get("WHISPER_LANGUAGE", "zh")
    try:
        audio = _load_wav_16k(audio_path)
    except wave.Error:
        # 非 WAV 时退回让 faster-whisper 自行解码
        audio = audio_path
    options = {'language': language or None, 'beam_size': 5, 'vad_filter': True}
    if (language or '').lower().startswith('zh'):
        options['initial_prompt'] = '以下是简体中文普通话。'
    segments, _info = model.transcribe(audio, **options)
    return _to_simplified("".join(segment.text for segment in segments).strip())
