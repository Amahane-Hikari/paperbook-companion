"""火山引擎“录音文件识别大模型”适配：submit 提交任务，query 轮询结果。"""
import base64
import os
import time
import uuid
import wave

import requests

SUBMIT_URL = os.environ.get('VOLC_ASR_SUBMIT_URL', 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit')
QUERY_URL = os.environ.get('VOLC_ASR_QUERY_URL', 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/query')
RESOURCE_ID = os.environ.get('VOLC_ASR_RESOURCE_ID', 'volc.seedasr.auc')


def configured() -> bool:
    return bool(os.environ.get('VOLC_ASR_API_KEY', '').strip())


def _audio_params(path):
    params = {'format': os.path.splitext(path)[1].lstrip('.').lower() or 'wav',
              'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1}
    try:
        with wave.open(path, 'rb') as audio:
            params.update(rate=audio.getframerate(), bits=audio.getsampwidth() * 8,
                          channel=audio.getnchannels(), format='wav', codec='raw')
    except (wave.Error, OSError):
        params.update(format='mp3', codec='raw')
    return params


def _default_timeout():
    try:
        return max(1.0, float(os.environ.get('VOLC_ASR_TIMEOUT', '20')))
    except ValueError:
        return 20.0


def transcribe(audio_path, timeout=None):
    if timeout is None:
        timeout = _default_timeout()
    key = os.environ.get('VOLC_ASR_API_KEY', '').strip()
    if not key:
        raise RuntimeError('火山语音识别未配置')
    request_id = str(uuid.uuid4())
    headers = {'Content-Type': 'application/json', 'x-api-key': key,
               'X-Api-Resource-Id': RESOURCE_ID, 'X-Api-Request-Id': request_id,
               'X-Api-Sequence': '-1'}
    with open(audio_path, 'rb') as audio:
        encoded = base64.b64encode(audio.read()).decode('ascii')
    payload = {'user': {'uid': 'paperbook'},
               'audio': {**_audio_params(audio_path), 'data': encoded},
               'request': {'model_name': 'bigmodel', 'enable_itn': True, 'enable_punc': True}}
    submit = requests.post(SUBMIT_URL, headers=headers, json=payload, timeout=60)
    submit.raise_for_status()
    if submit.headers.get('X-Api-Status-Code') not in (None, '20000000'):
        raise RuntimeError(f"火山识别提交失败: {submit.headers.get('X-Api-Message')}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        query = requests.post(QUERY_URL, headers=headers, json={}, timeout=30)
        code = query.headers.get('X-Api-Status-Code')
        if code == '20000000':
            body = query.json() or {}
            return str((body.get('result') or {}).get('text') or '').strip()
        if code not in (None, '20000001', '20000002'):
            raise RuntimeError(f"火山识别失败: {query.headers.get('X-Api-Message')}")
    raise RuntimeError('火山语音识别超时')
