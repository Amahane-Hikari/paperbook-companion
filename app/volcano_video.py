#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火山方舟（Volcano Ark）豆包 Seedance 图生视频适配层。

依据官方文档实现：
- 创建任务：POST {base_url}/contents/generations/tasks
- 查询任务：GET  {base_url}/contents/generations/tasks/{task_id}
- 鉴权：Authorization: Bearer <API Key>
- 结果：查询响应 content.video_url（预签名 URL，24 小时内下载）

对外主入口：
    generate_video_from_image(image_path, prompt=None, duration=5,
                              ratio="16:9", resolution="720p",
                              output_path=None) -> 本地 mp4 绝对路径
"""

import argparse
import base64
import json
import os
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

from config import (
    image2video_api_key as CONFIG_API_KEY,
    image2video_model as CONFIG_MODEL,
    image2video_url as CONFIG_BASE_URL,
)

# 官方文档给出的默认服务地址（Base URL 及鉴权）
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# 豆包 Seedance 1.0 pro fast 的实际 Model ID（文档：模型列表）
DEFAULT_MODEL = "doubao-seedance-1-0-pro-fast-251015"

CREATE_PATH = "/contents/generations/tasks"
QUERY_PATH = "/contents/generations/tasks/{task_id}"

# 文档：Seedance 1.0 pro / pro fast 输出 2~12 秒、24 fps、mp4
DURATION_RANGE = (2, 12)
ALLOWED_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive")
ALLOWED_RESOLUTIONS = ("480p", "720p", "1080p")

# 文档：单张图片 < 30 MB，请求体 < 64 MB；Base64 建议仅用于较小文件
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_SIDE = 2560
MIN_IMAGE_SIDE = 300
IMAGE_RATIO_RANGE = (0.4, 2.5)

DEFAULT_PROMPT = (
    "画面中的人物与物体自然、轻微地运动起来，镜头缓慢推进，"
    "保持原有画风、角色形象和色调一致，画面稳定。"
)

_PLACEHOLDER_KEYS = {
    "", "xxxx", "xxx", "your-api-key", "your_api_key", "ark_api_key",
    "placeholder", "none", "null",
}


class VolcanoVideoError(RuntimeError):
    """适配层统一异常（配置、请求、任务失败、下载失败）。"""


class VolcanoConfigError(VolcanoVideoError):
    """缺少或未填写火山方舟 API Key。"""


def _mask_key(key):
    if not key:
        return "<空>"
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:4]}...{key[-4:]}"


def _resolve_config(api_key=None, model=None, base_url=None):
    """读取配置：显式参数 > config.py（.env）> 官方默认值。"""
    key = (api_key if api_key is not None else CONFIG_API_KEY) or ""
    mdl = model or CONFIG_MODEL or DEFAULT_MODEL
    url = base_url or CONFIG_BASE_URL or DEFAULT_BASE_URL
    return key.strip(), mdl.strip(), url.rstrip("/")


def is_config_ready(api_key=None):
    """密钥是否已填写（仅检查本地配置，不发起网络请求）。"""
    key, _, _ = _resolve_config(api_key=api_key)
    return key.lower() not in _PLACEHOLDER_KEYS


def _ensure_config(api_key, base_url):
    if api_key.lower() in _PLACEHOLDER_KEYS:
        raise VolcanoConfigError(
            "未配置火山方舟 API Key，无法调用图生视频。\n"
            "请在项目根目录 .env 中填写：\n"
            "    IMAGE2VIDEO_API_KEY=<你的火山方舟 API Key>\n"
            "（也兼容 ARK_API_KEY；Key 可在 https://console.volcengine.com/ark "
            "的「API Key 管理」页面获取）"
        )
    if not base_url.startswith("http"):
        raise VolcanoConfigError(
            f"IMAGE2VIDEO_URL 配置无效：{base_url!r}，"
            f"应形如 {DEFAULT_BASE_URL}"
        )


def _prepare_image(image_source, max_bytes=MAX_IMAGE_BYTES):
    """
    把本地页图转成文档要求的 Base64 Data URI。

    文档格式：data:image/<图片格式>;base64,<Base64 编码>（格式名小写）。
    仅本地文件会被读取并规范化（转 RGB、限制尺寸、压缩体积）；
    公网 URL / asset:// / 已有 data URI 原样返回。

    Returns:
        (image_ref, meta)   image_ref 可直接放入 content.image_url.url
    """
    if not image_source:
        raise VolcanoVideoError("image_source 为空，无法提交图生视频任务")

    src = str(image_source)
    if src.startswith(("http://", "https://", "asset://", "data:")):
        return src, {"kind": "remote"}

    path = Path(src)
    if not path.is_file():
        raise VolcanoVideoError(f"页图不存在：{src}")

    try:
        with Image.open(path) as im:
            im.seek(0)  # 动图/GIF 只取首帧
            width, height = im.size
            ratio = width / height if height else 0
            if not (IMAGE_RATIO_RANGE[0] <= ratio <= IMAGE_RATIO_RANGE[1]):
                raise VolcanoVideoError(
                    f"图片宽高比 {ratio:.2f} 超出文档允许范围 "
                    f"[{IMAGE_RATIO_RANGE[0]}, {IMAGE_RATIO_RANGE[1]}]：{src}"
                )

            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                im = background
            elif im.mode != "RGB":
                im = im.convert("RGB")

            # 限制尺寸：过大缩放，过小放大到文档下限
            scale = 1.0
            if max(width, height) > MAX_IMAGE_SIDE:
                scale = MAX_IMAGE_SIDE / max(width, height)
            if min(width, height) * scale < MIN_IMAGE_SIDE:
                scale = MIN_IMAGE_SIDE / min(width, height)
            if abs(scale - 1.0) > 1e-6:
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                im = im.resize(new_size, Image.LANCZOS)

            quality = 92
            shrink_times = 0
            while True:
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= max_bytes or shrink_times >= 8:
                    break
                if quality > 70:
                    quality -= 8
                else:
                    shrink_times += 1
                    im = im.resize(
                        (max(1, int(im.size[0] * 0.8)), max(1, int(im.size[1] * 0.8))
                    ), Image.LANCZOS)
                    quality = 80

        b64 = base64.b64encode(data).decode("ascii")
        data_uri = f"data:image/jpeg;base64,{b64}"
        meta = {
            "kind": "base64",
            "path": str(path.resolve()),
            "width": im.size[0],
            "height": im.size[1],
            "bytes": len(data),
        }
        return data_uri, meta
    except VolcanoVideoError:
        raise
    except Exception as e:
        raise VolcanoVideoError(f"读取/转换页图失败：{src}，原因：{e}") from e


def build_request_body(image_source, prompt=None, duration=5, ratio="16:9",
                       resolution="720p", model=None):
    """
    构造创建任务请求体（不发起请求，可用于参数校验/联调）。

    文档字段：
      - model：Model ID
      - content：文本 + image_url（图生视频-首帧，role=first_frame 可省略）
      - duration / ratio / resolution：强校验，位于 body 顶层
    """
    mdl = (model or CONFIG_MODEL or DEFAULT_MODEL).strip()

    try:
        dur = int(duration)
    except (TypeError, ValueError):
        raise VolcanoVideoError(f"duration 必须是整数秒，收到：{duration!r}")
    if not (DURATION_RANGE[0] <= dur <= DURATION_RANGE[1]):
        raise VolcanoVideoError(
            f"duration={dur} 超出 Seedance 1.0 pro / pro fast 的文档范围 "
            f"{DURATION_RANGE[0]}~{DURATION_RANGE[1]} 秒"
        )
    if ratio not in ALLOWED_RATIOS:
        raise VolcanoVideoError(
            f"ratio={ratio!r} 不受支持，可选：{', '.join(ALLOWED_RATIOS)}"
        )
    if resolution not in ALLOWED_RESOLUTIONS:
        raise VolcanoVideoError(
            f"resolution={resolution!r} 不受支持，可选：{', '.join(ALLOWED_RESOLUTIONS)}"
        )

    text = (prompt or DEFAULT_PROMPT).strip()
    image_ref, _ = _prepare_image(image_source)

    return {
        "model": mdl,
        "content": [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": image_ref},
                "role": "first_frame",
            },
        ],
        "duration": dur,
        "ratio": ratio,
        "resolution": resolution,
    }


def _error_text(response):
    """从方舟错误响应中提取 code/message（文档错误结构为 error.code/message）。"""
    try:
        payload = response.json()
    except Exception:
        return (response.text or "")[:300]
    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or ""
        msg = err.get("message") or ""
        return f"{code}: {msg}".strip(": ")
    return str(payload.get("message") or payload)[:300]


def create_task(body, api_key, base_url, timeout=(10, 60), max_retries=3, verbose=True):
    """提交视频生成任务，返回 task_id（文档：响应 id，形如 cgt-xxx）。"""
    url = f"{base_url}{CREATE_PATH}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    backoff = 2
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_error = f"网络异常：{e}"
            if attempt < max_retries:
                if verbose:
                    print(f"  提交失败（第 {attempt} 次）：{e}，{backoff}s 后重试")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise VolcanoVideoError(f"提交任务失败（已重试 {max_retries} 次）：{e}") from e

        if resp.status_code in (200, 201):
            try:
                payload = resp.json()
            except Exception as e:
                raise VolcanoVideoError(f"提交任务响应不是 JSON：{resp.text[:200]}") from e
            task_id = payload.get("id") or payload.get("task_id")
            if not task_id:
                raise VolcanoVideoError(f"提交任务成功但响应缺少 id：{payload}")
            return task_id

        detail = _error_text(resp)
        last_error = f"HTTP {resp.status_code} {detail}"
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            if verbose:
                print(f"  提交失败（第 {attempt} 次）：{last_error}，{backoff}s 后重试")
            time.sleep(backoff)
            backoff *= 2
            continue
        raise VolcanoVideoError(f"提交任务失败：{last_error}")

    raise VolcanoVideoError(f"提交任务失败：{last_error}")


def fetch_task(task_id, api_key, base_url, timeout=(10, 30)):
    """查询一次任务状态，返回查询响应 JSON。"""
    url = f"{base_url}{QUERY_PATH.format(task_id=task_id)}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise VolcanoVideoError(
            f"查询任务 {task_id} 失败：HTTP {resp.status_code} {_error_text(resp)}"
        )
    return resp.json()


def wait_for_task(task_id, api_key, base_url, timeout=900, poll_interval=5,
                  max_poll_errors=3, verbose=True):
    """
    轮询任务直到成功。

    文档状态：queued / running / succeeded / failed / cancelled / expired。
    网络类错误连续出现时重试，不直接判定任务失败。
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    poll_errors = 0
    interval = poll_interval

    while True:
        if time.monotonic() > deadline:
            raise VolcanoVideoError(
                f"任务 {task_id} 轮询超时（{timeout}s），请稍后用查询接口继续查询"
            )

        try:
            payload = fetch_task(task_id, api_key, base_url)
            poll_errors = 0
        except requests.exceptions.RequestException as e:
            poll_errors += 1
            if poll_errors >= max_poll_errors:
                raise VolcanoVideoError(
                    f"查询任务 {task_id} 连续失败 {poll_errors} 次：{e}"
                ) from e
            if verbose:
                print(f"  查询异常（{poll_errors}/{max_poll_errors}）：{e}，稍后重试")
            time.sleep(interval)
            continue

        status = (payload.get("status") or "").lower()

        if status == "succeeded":
            if verbose:
                elapsed = time.monotonic() - start
                print(f"  任务完成（耗时 {elapsed:.0f}s）")
            return payload

        if status in ("failed", "expired", "cancelled"):
            err = payload.get("error") or {}
            detail = ""
            if isinstance(err, dict):
                detail = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
            raise VolcanoVideoError(
                f"任务 {task_id} 状态为 {status} {detail}".strip()
            )

        if status not in ("queued", "running", ""):
            if verbose:
                print(f"  未知任务状态 {status!r}，继续轮询")

        if verbose:
            elapsed = time.monotonic() - start
            print(f"  任务 {task_id} 状态：{status or 'queued'}（已等待 {elapsed:.0f}s）")

        # 简单的分段退避：前半分钟 3s，之后 5s，3 分钟后 10s
        elapsed = time.monotonic() - start
        if elapsed > 180:
            interval = 10
        elif elapsed > 30:
            interval = 5
        time.sleep(interval)


def download_video(video_url, output_path, timeout=(10, 180), max_retries=3, verbose=True):
    """下载生成结果到本地（预签名 URL 无需鉴权头），成功返回绝对路径。"""
    if not video_url:
        raise VolcanoVideoError("查询结果中缺少 content.video_url")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")

    backoff = 2
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(video_url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            size = tmp.stat().st_size
            if size <= 0:
                raise VolcanoVideoError("下载到的视频文件为空")
            os.replace(tmp, out)
            if verbose:
                print(f"  已下载视频：{out}（{size / 1024 / 1024:.2f} MB）")
            return str(out.resolve())
        except Exception as e:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if attempt < max_retries:
                if verbose:
                    print(f"  下载失败（第 {attempt} 次）：{e}，{backoff}s 后重试")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise VolcanoVideoError(f"下载视频失败（已重试 {max_retries} 次）：{e}") from e

    raise VolcanoVideoError("下载视频失败")


def _default_output_path(image_source):
    src = str(image_source)
    if src.startswith(("http://", "https://", "data:", "asset://")):
        return str(Path.cwd() / f"seedance_{int(time.time())}.mp4")
    path = Path(src)
    return str(path.with_name(f"{path.stem}_seedance.mp4"))


def generate_video_from_image(image_path, prompt=None, duration=5, ratio="16:9",
                              resolution="720p", output_path=None, api_key=None,
                              model=None, base_url=None, timeout=900,
                              poll_interval=5, max_retries=3, verbose=True):
    """
    图生视频主入口：本地页图 -> 本地 mp4。

    Args:
        image_path: 本地页图路径（也兼容公网 URL）
        prompt: 可选提示词，缺省使用内置的“轻微运动/镜头推进”提示
        duration: 秒，Seedance 1.0 pro fast 文档范围 2~12
        ratio: 16:9 / 4:3 / 1:1 / 3:4 / 9:16 / 21:9 / adaptive
        resolution: 480p / 720p / 1080p（该模型默认 1080p）
        output_path: 输出 mp4 路径；缺省为页图同目录 <name>_seedance.mp4
        api_key/model/base_url: 覆盖 config.py 中的 .env 配置
        timeout: 轮询总超时（秒）
        max_retries: 提交/下载的重试次数

    Returns:
        str: 本地 mp4 绝对路径

    Raises:
        VolcanoConfigError: 未配置 API Key
        VolcanoVideoError: 参数、提交、任务或下载失败
    """
    key, mdl, url = _resolve_config(api_key=api_key, model=model, base_url=base_url)
    _ensure_config(key, url)

    body = build_request_body(
        image_path, prompt=prompt, duration=duration,
        ratio=ratio, resolution=resolution, model=mdl,
    )
    text = body["content"][0]["text"]
    if len(text) > 500 and verbose:
        print(f"⚠️  提示词长度 {len(text)} 字，文档建议中文不超过 500 字")

    if verbose:
        print("🎬 火山方舟 Seedance 图生视频")
        print(f"  模型: {mdl}")
        print(f"  地址: {url}{CREATE_PATH}")
        print(f"  参数: duration={body['duration']}s, ratio={body['ratio']}, "
              f"resolution={body['resolution']}")
        print(f"  图片: {image_path}")
        print(f"  提示词: {text[:60]}{'...' if len(text) > 60 else ''}")

    task_id = create_task(
        body, key, url, max_retries=max_retries, verbose=verbose
    )
    if verbose:
        print(f"  任务已提交：{task_id}")

    result = wait_for_task(
        task_id, key, url, timeout=timeout,
        poll_interval=poll_interval, verbose=verbose,
    )
    video_url = (result.get("content") or {}).get("video_url")
    if not video_url:
        raise VolcanoVideoError(
            f"任务 {task_id} 成功但响应缺少 content.video_url：{result}"
        )

    out_path = output_path or _default_output_path(image_path)
    return download_video(video_url, out_path, max_retries=max_retries, verbose=verbose)


class VolcanoVideoGenerator:
    """兼容原 VideoGenerator 调用方式的封装，供服务端直接替换使用。"""

    def __init__(self, base_url=None, api_key=None, model=None, verbose=True,
                 timeout=900, poll_interval=5, max_retries=3):
        self.api_key, self.model, self.base_url = _resolve_config(
            api_key=api_key, model=model, base_url=base_url
        )
        self.verbose = verbose
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_retries = max_retries

    def is_configured(self):
        return is_config_ready(self.api_key)

    def generate(self, prompt, image_path=None, image_url=None, output_path=None,
                 duration=5, resolution="720p", aspect_ratio=None, ratio=None,
                 **kwargs):
        """
        兼容旧调用：image_url 参数也可传入本地路径或公网 URL；
        aspect_ratio 与 ratio 等价，ratio 优先。
        """
        source = image_path or image_url
        if not source:
            raise VolcanoVideoError("缺少 image_path/image_url，无法生成视频")
        return generate_video_from_image(
            source,
            prompt=prompt,
            duration=duration,
            ratio=ratio or aspect_ratio or "16:9",
            resolution=resolution,
            output_path=output_path,
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
            poll_interval=self.poll_interval,
            max_retries=self.max_retries,
            verbose=self.verbose,
        )


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="火山方舟 Seedance 图生视频适配层（本地页图 -> mp4）"
    )
    parser.add_argument("image", nargs="?", help="本地页图路径（必填，除非 --check-config）")
    parser.add_argument("--prompt", default=None, help="提示词")
    parser.add_argument("--duration", type=int, default=5, help="时长（2~12 秒）")
    parser.add_argument("--ratio", default="16:9", help="16:9 / 9:16 / 1:1 / adaptive ...")
    parser.add_argument("--resolution", default="720p", help="480p / 720p / 1080p")
    parser.add_argument("--output", default=None, help="输出 mp4 路径")
    parser.add_argument("--timeout", type=float, default=900, help="轮询超时（秒）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只构造并校验请求体，不发起网络请求")
    parser.add_argument("--check-config", action="store_true",
                        help="检查本地配置并退出")
    args = parser.parse_args(argv)

    key, mdl, url = _resolve_config()
    if args.check_config:
        print(f"API Key: {'已配置' if is_config_ready() else '未配置'}（{_mask_key(key)}）")
        print(f"模型: {mdl}")
        print(f"地址: {url}")
        return 0 if is_config_ready() else 1

    if not args.image:
        parser.error("缺少 image 参数（或用 --check-config 仅检查配置）")

    try:
        body = build_request_body(
            args.image, prompt=args.prompt, duration=args.duration,
            ratio=args.ratio, resolution=args.resolution, model=mdl,
        )
        if args.dry_run:
            preview = dict(body)
            preview["content"] = [
                dict(item, image_url={"url": f"<data-uri {len(item['image_url']['url'])} chars>"})
                if item.get("type") == "image_url" else item
                for item in body["content"]
            ]
            print("✅ 请求体校验通过（未发送请求）：")
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            print(f"（完整 Base64 长度：{len(body['content'][1]['image_url']['url'])} 字符）")
            return 0

        result = generate_video_from_image(
            args.image, prompt=args.prompt, duration=args.duration,
            ratio=args.ratio, resolution=args.resolution,
            output_path=args.output, timeout=args.timeout,
        )
        print(f"✅ 视频已生成：{result}")
        return 0
    except VolcanoVideoError as e:
        print(f"❌ {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
