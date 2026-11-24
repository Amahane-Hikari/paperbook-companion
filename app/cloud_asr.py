"""云端流式语音识别（ASR）。

移植自官方参考代码的 ASR 实现：把 16kHz / 16bit / 单声道 PCM 通过 WebSocket
分片发送给识别服务，收集返回的文本。

配置（环境变量）：
    ASR_WS_SERVER_URL  识别服务的 WebSocket 地址，留空表示未配置
    ASR_TIMEOUT        单次识别最长等待秒数，默认 15

前端已把录音统一转成 16kHz 单声道 WAV，因此这里直接读取 WAV 取 PCM。
未配置或调用失败时由调用方回退到本地 faster-whisper。
"""

import asyncio
import json
import os
import time
import wave

import numpy as np

try:
    import websockets
except ImportError:  # pragma: no cover - 依赖缺失时按未配置处理
    websockets = None

# 参考实现的分片大小：3840 字节 = 16kHz/16bit/单声道下的 60ms
_CHUNK_BYTES = 3840
_TARGET_RATE = 16000


def cloud_asr_configured() -> bool:
    """是否配置了云端识别服务（只检查配置与本机依赖，不发请求）。"""
    return bool(os.environ.get("ASR_WS_SERVER_URL", "").strip()) and websockets is not None


def _read_pcm_16k(audio_path: str) -> bytes:
    """读取 WAV，转成 16kHz/16bit/单声道 PCM 字节。"""
    with wave.open(audio_path, "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    if samples.size == 0:
        return b""

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)

    if rate != _TARGET_RATE:
        target_len = int(samples.size * _TARGET_RATE / rate)
        src = np.linspace(0, samples.size - 1, samples.size)
        dst = np.linspace(0, samples.size - 1, max(1, target_len))
        resampled = np.interp(dst, src, samples.astype(np.float32))
        samples = np.clip(resampled, -32768, 32767).astype(np.int16)

    return samples.tobytes()


def _merge_texts(texts) -> str:
    """合并分句结果：后一句若包含前一句（累积式返回）则取更长的那句。"""
    merged = ""
    for text in texts:
        text = (text or "").strip()
        if not text:
            continue
        if not merged:
            merged = text
        elif text.startswith(merged):
            merged = text
        elif merged.startswith(text):
            continue
        else:
            merged = f"{merged} {text}"
    return merged.strip()


async def _recognize(pcm: bytes, timeout: float) -> str:
    url = os.environ["ASR_WS_SERVER_URL"].strip()
    request_id = f"asr_{int(time.time() * 1000)}"
    init_json = {
        "type": "start",
        "request_id": request_id,
        "asr_wake_up_type": "action_wake_up",
        "continuous": "false",
        "oneshot_replace_word": "true",
        "enable_punc": "true",
        "max_start_silence": "7000",
        "max_end_silence": "800",
    }
    end_json = {"type": "end", "request_id": request_id}

    texts = []
    async with websockets.connect(url, ping_interval=None, open_timeout=10, max_size=None) as ws:
        await ws.send(json.dumps(init_json))
        for start in range(0, len(pcm), _CHUNK_BYTES):
            await ws.send(pcm[start:start + _CHUNK_BYTES])
            await asyncio.sleep(0.001)
        await ws.send(json.dumps(end_json))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(message, str):
                payload = json.loads(message)
                if payload.get("code") not in (0, None):
                    raise RuntimeError(f"ASR 服务返回错误：{payload.get('message') or payload}")
                data = payload.get("data") or {}
                if payload.get("type") == "offline_asr" and data.get("showText"):
                    texts.append(data["showText"])
                if data.get("end"):
                    break
    return _merge_texts(texts)


def transcribe_file(audio_path: str) -> str:
    """识别一个 WAV 文件，返回文本；调用失败抛异常由上层回退。"""
    if not cloud_asr_configured():
        raise RuntimeError("未配置云端语音识别服务（ASR_WS_SERVER_URL）")
    pcm = _read_pcm_16k(audio_path)
    if not pcm:
        raise RuntimeError("录音为空，请重新说话")
    timeout = float(os.environ.get("ASR_TIMEOUT", "15"))
    return asyncio.run(_recognize(pcm, timeout))
