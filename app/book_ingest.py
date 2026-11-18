"""纸质书摄取：把拍摄/扫描的书页图转成结构化数据。

产出统一写入 bookshelf/<book_id>/book_manifest.json（沿用现有 manifest 风格），
每页包含 OCR 文本块、页面语义、实体表；所有 bbox 一律为 0-1 归一化坐标。

OCR 优先使用 RapidOCR（onnxruntime，离线轻量）；不可用时回退视觉模型读文字，
并在结果的 ocr.engine / ocr.risk 中明确标注差异。
"""

import base64
import io
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

import requests
from PIL import Image, ImageOps

import config

COORD_SPACE = "normalized_0_1"
BBOX_FORMAT = "xyxy"
DEFAULT_MAX_SIDE = 2000
MAX_SOURCE_PIXELS = 40_000_000

_MANIFEST_LOCK = threading.Lock()
_OCR_LOCK = threading.Lock()
_OCR_ENGINE = None
_OCR_INIT_ERROR = None


class IngestError(Exception):
    """入库参数/流程错误（HTTP 400）。"""


class VisionError(IngestError):
    """视觉服务不可用或多次解析失败（HTTP 502）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_book_id(raw):
    text = str(raw or "").strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text)
    text = text.replace("..", "_").strip(" ._")
    text = text[:80].rstrip(" ._")
    if text and PureWindowsPath(text).is_reserved():
        return f"book_{text}"[:80]
    return text


def contained_path(root, *parts):
    try:
        root = Path(root).resolve()
        target = root.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise IngestError("存储路径无效") from exc
    if not target.is_relative_to(root):
        raise IngestError("存储路径越界")
    return target


def book_abs_dir(book_id):
    safe = _sanitize_book_id(book_id)
    if not safe:
        raise IngestError("书本标识无效")
    shelf = contained_path(config.TARGET_DIR, "bookshelf")
    return str(contained_path(shelf, safe))


def manifest_abs_path(book_id):
    return str(contained_path(book_abs_dir(book_id), "book_manifest.json"))


def rel_to_target(path):
    return contained_path(config.TARGET_DIR, path).relative_to(Path(config.TARGET_DIR).resolve()).as_posix()


def asset_path(book_id, filename):
    if not re.fullmatch(r"page[1-9]\d*(?:_[a-f0-9]{12})?\.jpg", filename):
        raise FileNotFoundError(filename)
    root = contained_path(book_abs_dir(book_id), "images")
    path = contained_path(root, filename)
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def vision_configured():
    return config.service_configured(config.image_analysis_url, config.image_api_key, config.image_model)


def normalize_bbox(raw, *, strict=False):
    """把 bbox 统一转换为 0-1 xyxy 坐标。

    strict 用于声明了 0-1 xyxy 的结果。旧版输出和回退 OCR 兼容
    0-1000 相对坐标以及颠倒的边界。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("bbox 必须是长度为 4 的数组")
    if strict and any(type(item) not in (int, float) for item in raw):
        raise ValueError("bbox 坐标必须是数字")
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"bbox 含无效数字: {raw}") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bbox 坐标必须为有限数字")
    scale = 1000.0 if not strict and max(values) > 1.5 else 1.0
    vals = [v / scale for v in values]
    if any(v < 0 or v > 1 for v in vals):
        raise ValueError(f"bbox 超出 {COORD_SPACE} 范围: {raw}")
    x1, y1, x2, y2 = vals
    if x1 > x2 or y1 > y2:
        if strict:
            raise ValueError("bbox 必须按左上、右下顺序给出")
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
    if x2 - x1 < 0.005 or y2 - y1 < 0.005:
        raise ValueError(f"bbox 面积过小: {raw}")
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def _extract_json(content):
    """从模型回复中提取 JSON 对象（兼容 ```json 包裹与前后解释文字）。"""
    if not isinstance(content, str):
        raise ValueError("模型回复不是文本")
    text = content.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if match:
            text = match.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("回复中找不到 JSON")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON 括号不完整")


# ---------------------------------------------------------------------------
# 图像预处理
# ---------------------------------------------------------------------------

def _deskew_image(img, max_angle=12.0):
    """基于直线检测的轻量纠偏；检测不到可靠倾斜时原样返回。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return img
    try:
        arr = np.array(img.convert("L"))
        edges = cv2.Canny(arr, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180, threshold=80,
            minLineLength=max(60, img.width // 8), maxLineGap=20,
        )
        if lines is None:
            return img
        angles = []
        for line in lines[:200]:
            x1, y1, x2, y2 = line[0]
            if abs(x2 - x1) < 10:
                continue
            angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if abs(angle) <= max_angle:
                angles.append(angle)
        if len(angles) < 5:
            return img
        angle = float(np.median(angles))
        if abs(angle) < 0.4:
            return img
        return img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    except Exception:
        return img


def preprocess_image(source, dest_path, max_side=None, deskew=False):
    """统一方向、限制尺寸、可选纠偏，落盘为 JPEG。"""
    max_side = int(max_side or os.environ.get("INGEST_MAX_SIDE", DEFAULT_MAX_SIDE))
    max_side = min(4096, max(1, max_side))
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    try:
        with Image.open(source) as original:
            if original.width * original.height > MAX_SOURCE_PIXELS:
                raise IngestError("页图像素过多，请缩小后重新上传")
            img = ImageOps.exif_transpose(original)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            else:
                img = img.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise IngestError("页图像素过多，请缩小后重新上传") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise IngestError("无法读取这张页图") from exc

    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if deskew:
        img = _deskew_image(img)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    img.save(dest_path, "JPEG", quality=92)
    return {"width": img.width, "height": img.height, "path": dest_path}


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _get_ocr_engine():
    global _OCR_ENGINE, _OCR_INIT_ERROR
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None and _OCR_INIT_ERROR is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _OCR_ENGINE = RapidOCR()
            except Exception as e:  # 导入/模型缺失时回退视觉模型
                _OCR_INIT_ERROR = e
    return _OCR_ENGINE


def _ocr_via_vision(image_path, retries=2):
    """RapidOCR 不可用时的回退方案：让视觉模型读文字并给 0-1000 坐标。"""
    prompt = """请逐行识别这张书页图片中的全部文字（原文照录，不要改写），输出 JSON：
{
  "lines": [
    {"text": "第一行文字", "bbox": [左上x, 左上y, 右下x, 右下y]}
  ]
}
bbox 使用 0-1000 的相对坐标（左上角为原点），按从上到下的阅读顺序输出。
只输出 JSON，不要解释。"""
    def parse_lines(content):
        lines = json.loads(_extract_json(content)).get("lines")
        if not isinstance(lines, list):
            raise ValueError("视觉回退 OCR 未返回 lines 数组")
        return lines

    lines = vision_request(image_path, prompt, parser=parse_lines, retries=retries)
    blocks = []
    for line in lines[:200]:
        if not isinstance(line, dict) or not str(line.get("text", "")).strip():
            continue
        try:
            bbox = normalize_bbox(line.get("bbox"))
        except ValueError:
            continue
        blocks.append({
            "text": str(line["text"]).strip()[:1000],
            "bbox": bbox,
            "confidence": None,
        })
    return blocks


def run_ocr(image_path, width, height, retries=2):
    """返回 (text_blocks, engine_info)。"""
    engine = _get_ocr_engine()
    if engine is None:
        reason = type(_OCR_INIT_ERROR).__name__ if _OCR_INIT_ERROR else "未安装"
        blocks = _ocr_via_vision(image_path, retries=retries)
        return blocks, {
            "engine": "vision_fallback",
            "model": config.image_model,
            "risk": "RapidOCR 不可用，改用视觉模型读文字；坐标与文字准确率低于专用 OCR，长段落可能漏字/错字",
            "fallback_reason": reason,
        }

    try:
        with _OCR_LOCK:
            result, _ = engine(image_path)
    except Exception as exc:
        raise IngestError("RapidOCR 识别失败") from exc

    blocks = []
    for item in (result or [])[:200]:
        try:
            box, text, score = item[0], item[1], item[2]
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            if not str(text).strip():
                continue
            bbox = normalize_bbox([
                min(xs) / width, min(ys) / height,
                max(xs) / width, max(ys) / height,
            ])
            score = float(score)
            blocks.append({
                "text": str(text).strip()[:1000],
                "bbox": bbox,
                "confidence": round(score, 4) if math.isfinite(score) else None,
            })
        except Exception:
            continue

    blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    return blocks, {
        "engine": "rapidocr_onnxruntime",
        "offline": True,
        "risk": "插图类页面可能无印刷文字，text_blocks 为空属正常；手写体/艺术字识别率有限",
    }


# ---------------------------------------------------------------------------
# 视觉语义（IMAGE_ANALYSIS_*）
# ---------------------------------------------------------------------------

SEMANTIC_PROMPT = """你是绘本页面结构化助手。请只输出一个 JSON 对象，不要任何解释或 markdown。

输出结构：
{
  "coord_space": "normalized_0_1",
  "bbox_format": "xyxy",
  "description": "画面里发生了什么（1-2句，具体到人物动作与环境）",
  "characters": ["出场角色中文名", "..."],
  "scene": "场景（地点/时间/氛围，短语）",
  "plot_summary": "这一页推动剧情的一句话摘要",
  "style_anchor": "画风锚点：一句话概括画风、色调、线条特征",
  "entities": [
    {
      "name_cn": "实体中文名（如 白兔、红苹果、编织篮）",
      "name_en": "英文名",
      "bbox": [0.12, 0.24, 0.31, 0.58],
      "description": "一句话描述该实体在此页的形态/状态"
    }
  ]
}

要求：
1. 保留上述 coord_space 和 bbox_format。bbox 按 [左边x, 上边y, 右边x, 下边y] 给出四个 0 到 1 的数字。
   用整张输入图量位置，左上角为 (0, 0)，右下角为 (1, 1)。x 按整图宽度计算，y 按整图高度计算。
2. 为每个实体分别框出紧贴其可见轮廓的矩形，缩小空白并避免包含相邻物体或文字。
   即使一个物体处在另一个物体中，也根据各自可见轮廓分别标框，例如篮子里的苹果。
3. entities 最多给 6 个真实可见、可辨认且容易点读的实体（如角色、独立道具、显著景物），按视觉重要性排序。
   同一个实体只给一个框。有几个就给几个，没有则返回空数组。以图片为准，不根据参考文字添加图中没有的实体。
4. characters 只列出画面中可见的角色。除 name_en 和两个坐标声明外，文字字段用中文填写。
5. 输出要精简：description 不超过 50 字，plot_summary 不超过 30 字，style_anchor 不超过 20 字，每个实体的 description 不超过 20 字。"""


def vision_request(image, prompt, *, parser=None, retries=2, extra_context="", timeout=60, reference_image=None):
    if not vision_configured():
        raise VisionError("IMAGE_ANALYSIS_URL / IMAGE_API_KEY 未配置，无法调用视觉语义模型")

    image_inputs = (image,) if reference_image is None else (image, reference_image)
    image_parts = []
    for source in image_inputs:
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        image_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    if parser is None:
        parser = lambda content: json.loads(_extract_json(content))
    headers = {
        "Authorization": f"Bearer {config.image_api_key}",
        "Content-Type": "application/json",
    }
    last_error = None
    for attempt in range(retries + 1):
        prompt_text = prompt + (("\n\n参考 OCR 文本（可能有误）：\n" + extra_context) if extra_context else "")
        if attempt > 0:
            prompt_text += "\n\n请精简每个字段，输出完整的 JSON。"
        payload = {
            "model": config.image_model,
            "temperature": 0.1,
            "max_tokens": 8000,   # 该模型会先消耗推理 token，额度不足会导致 JSON 为空或被截断
            "stream": False,
            "messages": [
                {"role": "system", "content": "你是严谨的绘本图像结构化助手，只输出合法 JSON。"},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    *image_parts,
                ]},
            ],
        }
        try:
            response = requests.post(config.image_analysis_url, headers=headers, json=payload, timeout=timeout)
            if response.status_code != 200:
                raise VisionError(f"视觉服务 HTTP {response.status_code}")
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return parser(content)
        except (VisionError, requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise VisionError(f"视觉识别失败（已尝试 {retries + 1} 次），请检查服务配置或稍后重试") from last_error


def _bbox_iou(first, second):
    """计算两个有效 xyxy 框的交并比。"""
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = width * height
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection)


def _parse_semantic_json(content):
    raw = _extract_json(content)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("顶层不是 JSON 对象")

    missing = [k for k in ("description", "characters", "scene", "plot_summary", "style_anchor") if k not in data]
    if missing:
        raise ValueError(f"缺少字段: {missing}")
    strict_bbox = "coord_space" in data or "bbox_format" in data
    coord_space = data.get("coord_space", COORD_SPACE)
    bbox_format = data.get("bbox_format", BBOX_FORMAT)
    if coord_space != COORD_SPACE or bbox_format != BBOX_FORMAT:
        raise ValueError("实体坐标须使用 normalized_0_1 xyxy 格式")

    characters = data.get("characters") or []
    if isinstance(characters, str):
        characters = [characters]
    if not isinstance(characters, list):
        raise ValueError("characters 必须是数组")
    normalized_characters = []
    for item in characters[:20]:
        if isinstance(item, dict):
            item = item.get("name_cn") or item.get("name") or item.get("name_en")
        if item and str(item).strip():
            normalized_characters.append(str(item).strip()[:80])

    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        raise ValueError("entities 必须是数组")

    entities = []
    boxes_by_label = {}
    for item in raw_entities[:30]:
        if not isinstance(item, dict):
            continue
        name_cn = str(item.get("name_cn") or item.get("name") or "").strip()[:80]
        if not name_cn:
            continue
        try:
            bbox = normalize_bbox(item.get("bbox"), strict=strict_bbox)
        except ValueError:
            continue
        name_en = str(item.get("name_en") or "").strip()[:80]
        labels = {" ".join(name.casefold().split()) for name in (name_cn, name_en) if name}
        matching_boxes = (existing for label in labels for existing in boxes_by_label.get(label, ()))
        if any(_bbox_iou(bbox, existing) >= 0.85 for existing in matching_boxes):
            continue
        for label in labels:
            boxes_by_label.setdefault(label, []).append(bbox)
        entities.append({
            "name_cn": name_cn,
            "name_en": name_en,
            "bbox": bbox,
            "description": str(item.get("description") or "").strip()[:200],
        })

    if raw_entities and not entities:
        raise ValueError("entities 中没有可用的 0-1 bbox 实体")

    return {
        "description": str(data.get("description") or "").strip()[:400],
        "characters": normalized_characters,
        "scene": str(data.get("scene") or "").strip()[:200],
        "plot_summary": str(data.get("plot_summary") or "").strip()[:200],
        "style_anchor": str(data.get("style_anchor") or "").strip()[:200],
        "entities": entities,
    }


def analyze_page_semantics(image_path, ocr_text="", retries=2):
    context = (ocr_text or "").strip()
    if len(context) > 400:
        context = context[:400]
    return vision_request(image_path, SEMANTIC_PROMPT, parser=_parse_semantic_json,
                          retries=retries, extra_context=context)


# ---------------------------------------------------------------------------
# 单页 / 整本摄取
# ---------------------------------------------------------------------------

def checked_page_number(value):
    if type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        page = int(value)
        if 1 <= page <= config.BOOK_MAX_PAGES:
            return page
    raise IngestError(f"页码必须是 1 到 {config.BOOK_MAX_PAGES} 之间的整数")


def ingest_page(source, book_id, page_no, deskew=False, retries=2, *, native_blocks=None, allow_degraded=False):
    """处理单页并返回结构化结果（不写 manifest）。"""
    page_no = checked_page_number(page_no)
    if not allow_degraded and not vision_configured():
        raise VisionError("IMAGE_ANALYSIS_URL / IMAGE_API_KEY 未配置，无法调用视觉语义模型")

    images = contained_path(book_abs_dir(book_id), "images")
    images.mkdir(parents=True, exist_ok=True)
    dest = images / f"page{page_no}_{uuid.uuid4().hex[:12]}.jpg"
    warnings = []
    with tempfile.TemporaryDirectory(prefix="ingest-", dir=images) as temporary:
        pending = os.path.join(temporary, "page.jpg")
        info = preprocess_image(source, pending, deskew=deskew)
        width, height = info["width"], info["height"]
        if native_blocks:
            text_blocks, ocr_info = native_blocks, {"engine": "pdf_native_text", "offline": True}
        else:
            try:
                text_blocks, ocr_info = run_ocr(pending, width, height, retries=retries)
            except IngestError:
                if not allow_degraded:
                    raise
                text_blocks, ocr_info = [], {"engine": "unavailable"}
                warnings.append("这一页没有可用的文字识别结果。")
        text = "\n".join(b["text"] for b in text_blocks)
        try:
            semantics = analyze_page_semantics(pending, text, retries=retries)
        except VisionError:
            if not allow_degraded:
                raise
            semantics = dict(description="", characters=[], scene="", plot_summary="", style_anchor="", entities=[])
            warnings.append("视觉识别不可用，点读需要页面实体或文字区域。")
        os.replace(pending, dest)

    entry = {
        "page": page_no,
        "text": text,
        "image": rel_to_target(dest),
        "image_url": f"/api/ingest/assets/{quote(_sanitize_book_id(book_id), safe='')}/{dest.name}",
        "image_size": {"width": width, "height": height},
        "coord_space": COORD_SPACE,
        "text_blocks": text_blocks,
        "page_semantics": {
            "description": semantics["description"],
            "characters": semantics["characters"],
            "scene": semantics["scene"],
            "plot_summary": semantics["plot_summary"],
            "style_anchor": semantics["style_anchor"],
        },
        "entities": semantics["entities"],
        "ocr": ocr_info,
    }
    if warnings:
        entry["warnings"] = warnings
    if text_blocks:
        avg = [b["confidence"] for b in text_blocks if b.get("confidence") is not None]
        if avg:
            entry["ocr"]["avg_confidence"] = round(sum(avg) / len(avg), 4)
    return entry


def load_manifest(book_id):
    path = manifest_abs_path(book_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _new_manifest(book_id, title):
    return {
        "book_id": book_id,
        "title": title or book_id,
        "book_dir": f"bookshelf/{book_id}",
        "page_count": 0,
        "source": "paper_ingest",
        "coord_space": COORD_SPACE,
        "created_at": _now(),
        "updated_at": _now(),
        "pages": [],
    }


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="write-", dir=path.parent) as temporary:
        pending = Path(temporary) / path.name
        with pending.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(pending, path)


def save_manifest(book_id, manifest):
    path = manifest_abs_path(book_id)
    manifest["updated_at"] = _now()
    manifest["page_count"] = len(manifest.get("pages", []))
    atomic_json(path, manifest)
    return path


def upsert_page(book_id, page_entry, title=None):
    """把单页结果合并进 manifest（按页码去重、按页码排序）。"""
    with _MANIFEST_LOCK:
        manifest = load_manifest(book_id) or _new_manifest(book_id, title)
        if title:
            manifest["title"] = title
        pages = {int(p["page"]): p for p in manifest.get("pages", []) if "page" in p}
        pages[int(page_entry["page"])] = page_entry
        manifest["pages"] = [pages[k] for k in sorted(pages)]
        return save_manifest(book_id, manifest), manifest


def resolve_book_id(book_id, title, avoid_existing=True):
    """确定落盘目录：显式 book_id 优先；否则标题；已存在则加后缀避免覆盖旧书。"""
    explicit = _sanitize_book_id(book_id)
    unique = uuid.uuid4().hex[:12]
    if explicit:
        base = f"paper_{explicit}"[:80] if explicit.lower().startswith("quick_") else explicit
        if not avoid_existing or not os.path.isfile(manifest_abs_path(base)):
            return base
        existing = load_manifest(base) or {}
        if existing.get("source") == "paper_ingest":
            return base
        candidate = f"{base[:73]}_ingest"
        if not os.path.isfile(manifest_abs_path(candidate)):
            return candidate
        return f"{candidate[:67]}_{unique}"

    base = _sanitize_book_id(title) or f"ingest_{unique}"
    if base.lower().startswith("quick_"):
        base = f"paper_{base}"[:80]
    if not avoid_existing or not os.path.isfile(manifest_abs_path(base)):
        return base
    candidate = f"{base[:73]}_ingest"
    if not os.path.isfile(manifest_abs_path(candidate)):
        return candidate
    return f"{candidate[:67]}_{unique}"


def ingest_book(items, book_id=None, title=None, deskew=False, retries=2):
    """按顺序逐页处理并汇总成一本。

    items: [{"source": bytes|path, "order": int, "filename": str}]
    """
    if not items:
        raise IngestError("没有收到任何页图")
    if len(items) > config.BOOK_MAX_PAGES:
        raise IngestError(f"一次最多摄取 {config.BOOK_MAX_PAGES} 张页图")

    orders = [checked_page_number(item["order"]) if item.get("order") is not None else None for item in items]
    explicit = [order for order in orders if order is not None]
    if len(explicit) != len(set(explicit)):
        raise IngestError("同一次上传中不能有重复页码")
    if not explicit:
        matches = [re.search(r"[0-9]+", str(item.get("filename") or "")) for item in items]
        if all(matches):
            try:
                inferred = [checked_page_number(match[0]) for match in matches]
            except IngestError:
                inferred = []
            if len(set(inferred)) == len(items):
                orders = inferred
    used = {order for order in orders if order is not None}
    available = (page for page in range(1, config.BOOK_MAX_PAGES + 1) if page not in used)
    ordered = sorted((order if order is not None else next(available), item) for order, item in zip(orders, items))

    resolved_id = resolve_book_id(book_id, title)
    pages = []
    try:
        for order, item in ordered:
            pages.append(ingest_page(item["source"], resolved_id, order, deskew=deskew, retries=retries))

        with _MANIFEST_LOCK:
            manifest = load_manifest(resolved_id) or _new_manifest(resolved_id, title)
            if title:
                manifest["title"] = title
            merged = {int(p["page"]): p for p in manifest.get("pages", []) if "page" in p}
            for entry in pages:
                merged[int(entry["page"])] = entry
            manifest["pages"] = [merged[k] for k in sorted(merged)]
            path = save_manifest(resolved_id, manifest)
    except Exception:
        for entry in pages:
            asset_path(resolved_id, Path(entry["image"]).name).unlink(missing_ok=True)
        raise

    return {
        "book_id": resolved_id,
        "manifest_path": path,
        "manifest": manifest,
        "requested_book_id": _sanitize_book_id(book_id) or None,
    }
