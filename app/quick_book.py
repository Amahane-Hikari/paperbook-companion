"""上传即用：单页 PDF -> 可播放的互动绘本。

流程：PDF 选页 -> 渲染页图 -> 复用 book_ingest 摄取（OCR/视觉语义/实体）
-> 写旁白脚本 -> 分段 TTS 拼接 -> 原页图生视频 -> 就绪。
所有耗时、失败原因都记录在任务状态里，前端只轮询状态。
"""

import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image

import config
from config import get_capabilities, service_configured
from book_ingest import (
    _extract_json,
    asset_path as ingest_asset_path,
    atomic_json,
    book_abs_dir,
    checked_page_number,
    contained_path,
    ingest_page as ingest_page_impl,
    upsert_page as upsert_ingest_page,
    IngestError,
    rel_to_target,
    vision_configured,
    vision_request,
)
from book_pipeline import _sentences, _tokens
from book_video import concatenate_wav, wav_duration
import local_tts
from volcano_video import VolcanoVideoGenerator

MAX_SIDE = 2000
VIDEO_DURATION = 5
VIDEO_RESOLUTION = "480p"
VIDEO_RATIO = "adaptive"
VIDEO_STYLE_SUFFIX = (
    "保持这张绘本原图的画风、角色造型和配色完全一致；只做轻微自然的镜头推进和角色动作，"
    "不要添加任何文字、水印、边框，不要改变画面构图。"
)
DEFAULT_VOICE = "xiaosi"
VOICES = ("xiaosi", "xiaoyuan", "niudun", "oula")

_ID_SUFFIX = r"[0-9]{8}_[0-9]{6}_[a-f0-9]{12}"
_PREVIEW_NAME = r"page[1-9][0-9]*\.jpg"
_QUICK_MEDIA = r"(?:narration\.wav|dynamic\.mp4|tts/seg_[0-9]+\.wav|entities/entity_[0-9]+\.wav|points/point_[0-9._]+\.wav|qa/answer_[a-f0-9]{12}\.wav)"

WATERMARK_MARKERS = (
    "仅供", "请勿", "外传", "大赛", "参赛", "纸书智伴",
    "阅读指导", "汉字源流", "出版社", "版权所有", "版权", "ISBN",
    "定价", "官方网站", "官方微信",
)
BANNED_STORY_MARKERS = (
    "仅供", "请勿", "外传", "大赛", "参赛", "纸书智伴",
    "阅读指导", "汉字源流", "出版社", "版权所有", "版权", "ISBN",
)

_STAGES = [
    {"key": "identify", "label": "识别页面", "progress": 18},
    {"key": "write", "label": "撰写旁白", "progress": 38},
    {"key": "voice", "label": "生成配音", "progress": 58},
    {"key": "visual", "label": "生成动态画面", "progress": 88},
    {"key": "ready", "label": "可以播放", "progress": 100},
]

_JOBS = {}
_JOBS_LOCK = threading.RLock()
_VIDEO_LOCK = threading.Lock()
_VIDEO_GENERATOR = None
_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now_label():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"


def _is_id(value, prefix):
    return isinstance(value, str) and bool(re.fullmatch(f"{prefix}_{_ID_SUFFIX}", value))


def _media_url(path):
    return '/api/quickbook/assets/' + quote(rel_to_target(path), safe='/')


def asset_path(relative):
    preview = re.fullmatch(rf"output_dir/quick_uploads/(up_{_ID_SUFFIX})/preview/({_PREVIEW_NAME})", relative)
    if preview:
        upload_id, filename = preview.groups()
        path = contained_path(contained_path(_upload_dir(upload_id), "preview"), filename)
    else:
        generated = re.fullmatch(rf"bookshelf/(quick_{_ID_SUFFIX})/(images|quick)/(.+)", relative)
        if not generated:
            raise FileNotFoundError(relative)
        book_id, subdir, filename = generated.groups()
        if subdir == "images":
            return ingest_asset_path(book_id, filename)
        if not re.fullmatch(_QUICK_MEDIA, filename):
            raise FileNotFoundError(relative)
        path = contained_path(contained_path(book_abs_dir(book_id), subdir), filename)
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path


def _stage_public(job):
    current = job.get("stage", "identify")
    keys = [s["key"] for s in _STAGES]
    index = keys.index(current) if current in keys else 0
    ready = current == "ready" and job.get("status") == "ready"
    out = []
    for i, item in enumerate(_STAGES):
        if ready or i < index:
            state = "done"
        elif i == index:
            state = "current"
        else:
            state = "pending"
        out.append({**item, "state": state})
    return out


def _save_job(job):
    root = contained_path(book_abs_dir(job["book_id"]), "quick")
    atomic_json(contained_path(root, "job.json"), job)


def _load_job_from_disk(job_id):
    if not _is_id(job_id, "qb"):
        return None
    book_id = f"quick_{job_id.removeprefix('qb_')}"
    try:
        book_dir = book_abs_dir(book_id)
        path = contained_path(contained_path(book_dir, "quick"), "job.json")
        with path.open("r", encoding="utf-8") as f:
            job = json.load(f)
    except (IngestError, OSError, ValueError):
        return None
    if not isinstance(job, dict) or job.get("job_id") != job_id or job.get("book_id") != book_id:
        return None
    job["book_dir"] = book_dir
    if job.get("status") == "running":
        _fail(job, job.get("stage", "identify"), "生成已中断", "服务已重启，请重新生成这一页")
    return job


def _update(job, **fields):
    with _JOBS_LOCK:
        job.update(fields)
        job["updated_at"] = time.time()
        job["updated_label"] = _now_label()
        try:
            _save_job(job)
        except OSError:
            _LOG.exception("Unable to save quick book job %s", job["job_id"])


def get_job(job_id):
    if not _is_id(job_id, "qb"):
        return None
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job:
            return job
        job = _load_job_from_disk(job_id)
        if job:
            _JOBS[job_id] = job
        return job


def public_status(job):
    end = time.time() if job.get("status") == "running" else job.get("updated_at", time.time())
    elapsed = round(max(0, end - job.get("started_at", end)), 1)
    return {
        "success": True,
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "stage_label": next((s["label"] for s in _STAGES if s["key"] == job.get("stage")), ""),
        "stages": _stage_public(job),
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "elapsed_seconds": elapsed,
        "page": job.get("page_no"),
        "page_count": job.get("page_count"),
        "warnings": job.get("warnings", []),
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "timings": job.get("timings", {}),
    }


def public_result(job):
    if job.get("status") != "ready":
        raise IngestError("互动绘本还没有生成完成")
    return {
        "success": True,
        "job_id": job.get("job_id"),
        "book_id": job.get("book_id"),
        "title": job.get("result", {}).get("title"),
        "page": job.get("page_no"),
        "page_count": job.get("page_count"),
        "image_url": job.get("result", {}).get("image_url"),
        "image_size": job.get("result", {}).get("image_size"),
        "video_url": job.get("result", {}).get("video_url"),
        "video_error": job.get("result", {}).get("video_error"),
        "narration": job.get("result", {}).get("narration"),
        "storyboard": job.get("result", {}).get("storyboard"),
        "entities": job.get("result", {}).get("entities", []),
        "clean_text": job.get("result", {}).get("clean_text"),
        "timings": job.get("timings", {}),
        "warnings": job.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# PDF 上传与选页
# ---------------------------------------------------------------------------

def _fitz():
    try:
        import pymupdf
        return pymupdf
    except ImportError as exc:
        raise IngestError("服务缺少 PDF 处理组件，请先安装 PyMuPDF") from exc


def _open_doc(pdf_path):
    try:
        doc = _fitz().open(pdf_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IngestError("PDF 无法打开，请检查文件是否完整") from exc
    if doc.needs_pass:
        doc.close()
        raise IngestError("请先移除 PDF 的密码保护")
    if not 1 <= doc.page_count <= config.BOOK_MAX_PAGES:
        doc.close()
        raise IngestError(f"PDF 页数必须在 1 到 {config.BOOK_MAX_PAGES} 之间")
    return doc


def _selected_page(doc, page_no):
    page_no = checked_page_number(page_no)
    if page_no > doc.page_count:
        raise IngestError(f"页码超出范围：第 {page_no} 页，共 {doc.page_count} 页")
    return doc[page_no - 1]


def _render_page(page, out_path):
    extent = max(page.rect.width, page.rect.height)
    if not math.isfinite(extent) or extent <= 0:
        raise IngestError("这一页的尺寸无效")
    max_side = min(4096, max(1, int(os.environ.get("QUICK_PAGE_MAX_SIDE", MAX_SIDE))))
    zoom = min(max_side / extent, 4.0)
    try:
        pix = page.get_pixmap(matrix=_fitz().Matrix(zoom, zoom), alpha=False)
        if pix.width < 1 or pix.height < 1:
            raise IngestError("这一页的尺寸无效")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="preview-", dir=out_path.parent) as temporary:
            pending = Path(temporary) / out_path.name
            pix.save(pending)
            os.replace(pending, out_path)
    except (ValueError, RuntimeError) as exc:
        raise IngestError("这一页无法预览，请检查 PDF 是否完整") from exc
    return {"width": pix.width, "height": pix.height}


def _native_text_blocks(page):
    fitz = _fitz()
    width, height = page.rect.width, page.rect.height
    if width <= 0 or height <= 0:
        return []
    try:
        raw_blocks = page.get_text("blocks", sort=True)
    except (RuntimeError, ValueError):
        return []
    blocks = []
    for x1, y1, x2, y2, raw_text, _, block_type in raw_blocks:
        text = raw_text.strip()[:1000]
        if block_type != 0 or not text:
            continue
        box = (fitz.Rect(x1, y1, x2, y2) * page.rotation_matrix) & page.rect
        if box.is_empty:
            continue
        bbox = [round(box.x0 / width, 4), round(box.y0 / height, 4),
                round(box.x1 / width, 4), round(box.y1 / height, 4)]
        blocks.append({"text": text, "bbox": bbox, "confidence": None})
        if len(blocks) == 200:
            break
    return blocks


def _upload_dir(upload_id):
    if not _is_id(upload_id, "up"):
        raise IngestError("上传文件标识无效，请重新上传 PDF")
    root = contained_path(config.TARGET_DIR, "output_dir", "quick_uploads")
    return contained_path(root, upload_id)


def _upload_pdf_path(upload_id):
    path = contained_path(_upload_dir(upload_id), "source.pdf")
    if not path.is_file():
        raise IngestError("上传文件已失效，请重新上传 PDF")
    return path


class UploadTooLarge(IngestError):
    """上传体积超过服务配置。"""


def create_upload(file_storage):
    """保存上传的 PDF，返回 upload_id、总页数、第 1 页预览。"""
    filename = getattr(file_storage, "filename", "") or "upload.pdf"
    if not filename.lower().endswith(".pdf"):
        raise IngestError("请上传 PDF 文件")
    upload_id = _new_id("up")
    target = _upload_dir(upload_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    maximum = config.BOOK_MAX_UPLOAD_BYTES
    with tempfile.TemporaryDirectory(prefix="upload-", dir=target.parent) as temporary:
        staged = Path(temporary) / upload_id
        staged.mkdir()
        source = staged / "source.pdf"
        with source.open("wb") as output:
            size = 0
            while chunk := file_storage.read(min(1024 * 1024, maximum - size + 1)):
                if size + len(chunk) > maximum:
                    raise UploadTooLarge("PDF 超过上传大小限制")
                if size == 0 and b"%PDF-" not in chunk[:1024]:
                    raise IngestError("这个文件不是有效的 PDF，请重新选择")
                size += len(chunk)
                output.write(chunk)
            if not size:
                raise IngestError("这个文件不是有效的 PDF，请重新选择")
        with _open_doc(source) as doc:
            count = doc.page_count
            preview = Path("preview") / "page1.jpg"
            info = _render_page(doc[0], staged / preview)
        os.replace(staged, target)
    return {
        "success": True,
        "upload_id": upload_id,
        "filename": os.path.basename(filename.replace("\\", "/"))[:200],
        "page_count": count,
        "page": 1,
        "width": info["width"],
        "height": info["height"],
        "image_url": _media_url(target / preview),
    }


def render_preview(upload_id, page_no):
    pdf_path = _upload_pdf_path(upload_id)
    page_no = checked_page_number(page_no)
    preview = contained_path(_upload_dir(upload_id), "preview")
    out = contained_path(preview, f"page{page_no}.jpg")
    with _open_doc(pdf_path) as doc:
        page = _selected_page(doc, page_no)
        count = doc.page_count
        if not out.is_file():
            _render_page(page, out)
    return {
        "success": True,
        "upload_id": upload_id,
        "page": page_no,
        "page_count": count,
        "image_url": _media_url(out),
    }


# ---------------------------------------------------------------------------
# 内容生成
# ---------------------------------------------------------------------------

def _is_watermark_or_footer(block):
    text = str(block.get("text") or "").strip()
    if not text:
        return True
    bbox = block.get("bbox") or [0, 0, 1, 1]
    if bbox[3] > 0.93:
        return True
    if bbox[1] < 0.045 and len(text) <= 6 and re.search(r"\d", text):
        return True
    return any(marker in text for marker in WATERMARK_MARKERS)


def clean_page_text(entry):
    blocks = []
    for block in entry.get("text_blocks", []):
        if _is_watermark_or_footer(block):
            continue
        blocks.append(block)
    blocks.sort(key=lambda b: (round(b["bbox"][1], 2), b["bbox"][0]))
    text = "\n".join(b["text"] for b in blocks)
    return text.strip(), blocks


def _llm_chat(messages, temperature=0.5, max_tokens=2600):
    payload = {
        "model": config.simple_story_writer_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {config.simple_story_writer_api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(config.simple_story_writer_url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"模型服务 HTTP {resp.status_code}")
    data = resp.json()
    content = data["choices"][0]["message"].get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def _fallback_story(entry, clean_text, page_no):
    semantics = entry.get("page_semantics") or {}
    source = clean_text or semantics.get("description", "")
    sentences = [s.strip() for s in re.split(r"(?<=[。！？.!?])|\n+", source[:720]) if s.strip()]
    groups = []
    for sentence in sentences:
        if groups and len("".join(groups[-1])) < 55:
            groups[-1].append(sentence)
        else:
            groups.append([sentence])
    if not groups:
        groups = [["请看看这一页的画面。"]]
    narration = [{"text": " ".join(g)} for g in groups[:3]]
    title = f"第 {page_no} 页的故事"
    scene = semantics.get("scene", "")
    if scene:
        title = f"第 {page_no} 页 · {scene[:10]}"
    notes = []
    for entity in entry.get("entities", []) or []:
        desc = str(entity.get("description") or "").strip()
        notes.append({
            "name": entity.get("name_cn"),
            "text": desc or f"这是{entity.get('name_cn')}。",
        })
    return {"title": title, "narration": narration, "entity_notes": notes, "source": "page_content"}


def write_story(entry, clean_text, page_no):
    if not service_configured(config.simple_story_writer_url, config.simple_story_writer_api_key,
                              config.simple_story_writer_model):
        return _fallback_story(entry, clean_text, page_no)
    semantics = entry.get("page_semantics") or {}
    entities = entry.get("entities", []) or []
    entity_lines = "\n".join(
        f"- {e.get('name_cn')}：{e.get('description') or ''}" for e in entities
    )
    prompt = f"""你是一位儿童绘本旁白编辑。只能依据这一页的内容写旁白，不要凭空增加与画面矛盾的情节。

【本页正文（已尽力过滤水印/页脚，可能仍有少量脏字）】
{clean_text[:3000] or "（这一页没有可识别的印刷正文）"}

【画面语义】
描述：{semantics.get('description', '')}
角色：{'、'.join(semantics.get('characters') or [])}
场景：{semantics.get('scene', '')}
画面情节：{semantics.get('plot_summary', '')}
画风：{semantics.get('style_anchor', '')}

【画面实体】
{entity_lines or "（无）"}

要求：
1. 写 2-3 段中文旁白，总字数 90-180 字，不注水，不要分章节。
2. 保留本页出现的人物名、动作和数字；如果正文里的数字只是图注，可以自然地放进旁白。不要把“三块”“四块”这类数量词当作人物，角色只用画面语义里列出的。
3. 严禁出现“仅供、请勿外传、大赛、参赛、阅读指导、汉字源流、出版社、版权”等水印/页脚字样。
4. 每段 1-2 句，适合 5-9 秒朗读。
5. 为每个画面实体写一句 10-25 字的儿童化点读讲解，必须沿用给定实体名。
6. 只输出 JSON：
{{"title":"...","narration":[{{"text":"..."}}],"entity_notes":[{{"name":"实体名","text":"..."}}]}}"""

    for attempt in range(2):
        try:
            content = _llm_chat(
                [
                    {"role": "system", "content": "你是严谨的中文绘本旁白编辑，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.45,
                max_tokens=2600,
            )
            data = json.loads(_extract_json(content))
            narration = []
            for item in data.get("narration") or []:
                raw = item.get("text") if isinstance(item, dict) else item
                text = raw.strip()[:300] if isinstance(raw, str) else ""
                if text and not any(m in text for m in BANNED_STORY_MARKERS):
                    narration.append({"text": text})
            if not narration:
                raise ValueError("旁白为空或被水印污染")
            title = str(data.get("title") or f"第 {page_no} 页的故事").strip()[:120]
            if any(m in title for m in BANNED_STORY_MARKERS):
                title = f"第 {page_no} 页的故事"
            notes = []
            for item in data.get("entity_notes") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                text = str(item.get("text") or "").strip()
                if name and text and not any(m in text for m in BANNED_STORY_MARKERS):
                    notes.append({"name": name[:80], "text": text[:80]})
            return {"title": title, "narration": narration[:3], "entity_notes": notes, "source": "model"}
        except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, IndexError, requests.RequestException):
            if attempt == 0:
                time.sleep(1)
    return _fallback_story(entry, clean_text, page_no)


def _synthesize_segments(job, narration, voice):
    quick_dir = contained_path(job["book_dir"], "quick")
    tts_dir = contained_path(quick_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)
    audio_paths = []
    segments = []
    start = 0.0
    for index, item in enumerate(narration):
        text = item["text"]
        path = contained_path(tts_dir, f"seg_{index}.wav")
        _synthesize_audio(text, voice, path)
        duration = wav_duration(path)
        audio_paths.append(path)
        segments.append({
            "index": index,
            "text": text,
            "start": round(start, 2),
            "end": round(start + duration, 2),
            "duration": duration,
            "audio_url": _media_url(path),
        })
        start += duration
        _update(job, message=f"正在生成第 {index + 1}/{len(narration)} 段配音")
    narration_path = contained_path(quick_dir, "narration.wav")
    with tempfile.TemporaryDirectory(prefix="audio-", dir=quick_dir) as temporary:
        pending = os.path.join(temporary, "narration.wav")
        total = concatenate_wav(audio_paths, pending)
        os.replace(pending, narration_path)
    return {
        "audio_url": _media_url(narration_path),
        "duration": total,
        "segments": segments,
    }


def _synthesize_audio(text, voice, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audio-", dir=path.parent) as temporary:
        pending = os.path.join(temporary, "speech.wav")
        local_tts.synthesize(text, voice, pending, lang="cn")
        wav_duration(pending)
        os.replace(pending, path)


def _silent_narration(segments):
    return {"audio_url": None, "duration": 0,
            "segments": [{"index": index, "text": segment["text"], "start": None, "end": None,
                          "duration": None, "audio_url": None} for index, segment in enumerate(segments)]}


def _entity_notes(entry, story_notes):
    provided = {n.get("name"): n.get("text") for n in (story_notes or [])}
    out = []
    for index, entity in enumerate(entry.get("entities", []) or []):
        name = str(entity.get("name_cn") or "").strip()
        note = provided.get(name) or str(entity.get("description") or "").strip()
        if not note:
            note = f"这是{name}。"
        if len(note) > 80:
            note = note[:80]
        out.append({
            "index": index,
            "name_cn": name,
            "name_en": str(entity.get("name_en") or "").strip(),
            "bbox": entity.get("bbox"),
            "description": str(entity.get("description") or "").strip(),
            "note": note,
        })
    return out


def _synthesize_entities(job, entities, voice):
    quick_dir = contained_path(job["book_dir"], "quick", "entities")
    for entity in entities:
        index = entity["index"]
        path = contained_path(quick_dir, f"entity_{index}.wav")
        try:
            _synthesize_audio(entity["note"], voice, path)
            entity["audio_url"] = _media_url(path)
        except Exception:
            entity["audio_url"] = None
    return entities


def _video_generator():
    global _VIDEO_GENERATOR
    with _VIDEO_LOCK:
        if _VIDEO_GENERATOR is None:
            _VIDEO_GENERATOR = VolcanoVideoGenerator(
                base_url=config.image2video_url,
                api_key=config.image2video_api_key,
                model=config.image2video_model,
                verbose=False,
            )
        return _VIDEO_GENERATOR


def _make_dynamic_video(job, entry, narration, image_path):
    if not get_capabilities().get("video"):
        return {"path": None, "error": "图生视频服务未配置", "seconds": 0}
    semantics = entry.get("page_semantics") or {}
    first = narration[0]["text"] if narration else ""
    prompt = (
        f"以这张绘本原图为第一帧，表现这一页的内容：{semantics.get('plot_summary') or first}。"
        f"镜头轻微推进，画面里的人或物有自然的小动作。{VIDEO_STYLE_SUFFIX}"
    )
    quick_dir = contained_path(job["book_dir"], "quick")
    out = contained_path(quick_dir, "dynamic.mp4")
    result = {"path": None, "error": None, "seconds": 0}
    started = time.time()

    def _run():
        try:
            with tempfile.TemporaryDirectory(prefix="video-", dir=quick_dir) as temporary:
                pending = os.path.join(temporary, "dynamic.mp4")
                _video_generator().generate(
                    prompt=prompt, image_path=image_path, output_path=pending, duration=VIDEO_DURATION,
                    resolution=VIDEO_RESOLUTION, ratio=VIDEO_RATIO,
                )
                if os.path.isfile(pending) and os.path.getsize(pending) > 1024:
                    os.replace(pending, out)
                    result["path"] = out
                else:
                    result["error"] = "视频服务没有返回有效文件"
        except Exception:
            result["error"] = "图生视频服务暂不可用"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=4)
        elapsed = int(time.time() - started)
        _update(job, message=f"正在用本页原图生成动态画面（已等 {elapsed} 秒）")
    result["seconds"] = round(time.time() - started, 1)
    return result


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def start_job(upload_id, page_no, voice=DEFAULT_VOICE):
    pdf_path = _upload_pdf_path(upload_id)
    page_no = checked_page_number(page_no)
    with _open_doc(pdf_path) as doc:
        _selected_page(doc, page_no)
        count = doc.page_count
    if voice not in VOICES:
        voice = DEFAULT_VOICE

    job_id = _new_id("qb")
    book_id = f"quick_{job_id.removeprefix('qb_')}"
    book_dir = book_abs_dir(book_id)

    job = {
        "job_id": job_id,
        "book_id": book_id,
        "book_dir": book_dir,
        "upload_id": upload_id,
        "page_no": page_no,
        "page_count": count,
        "voice": voice,
        "status": "running",
        "stage": "identify",
        "progress": 5,
        "message": "开始识别这一页",
        "warnings": [],
        "error": None,
        "error_code": None,
        "started_at": time.time(),
        "timings": {},
    }
    with _JOBS_LOCK:
        _save_job(job)
        _JOBS[job_id] = job
    thread = threading.Thread(target=_worker, args=(job,), daemon=True)
    thread.start()
    return {"success": True, "job_id": job_id, "book_id": book_id, "page": page_no, "page_count": count}


def _worker(job):
    try:
        t0 = time.time()
        _update(job, stage="identify", progress=8, message="正在识别页面里的文字和画面")
        page_no = job["page_no"]
        upload_id = job["upload_id"]
        preview = contained_path(_upload_dir(upload_id), "preview")
        source_page = contained_path(preview, f"page{page_no}.jpg")
        with _open_doc(_upload_pdf_path(upload_id)) as doc:
            page = _selected_page(doc, page_no)
            if not source_page.is_file():
                _render_page(page, source_page)
            native_blocks = _native_text_blocks(page)
        entry = ingest_page_impl(source_page, job["book_id"], page_no, retries=1,
                                 native_blocks=native_blocks, allow_degraded=True)
        upsert_ingest_page(job["book_id"], entry, title=f"上传绘本 第 {job['page_no']} 页")
        job["timings"]["identify_seconds"] = round(time.time() - t0, 1)
        clean_text, clean_blocks = clean_page_text(entry)
        job["warnings"].extend(entry.get("warnings", []))
        _update(
            job,
            stage="identify",
            progress=18,
            message=f"识别完成：{len(entry.get('text_blocks', []))} 个文字块，{len(entry.get('entities', []))} 个画面元素",
        )

        t1 = time.time()
        _update(job, stage="write", progress=24, message="正在根据这一页撰写旁白")
        story = write_story(entry, clean_text, job["page_no"])
        if story.get("source") == "page_content":
            job["warnings"].append("旁白使用本页正文或画面描述。")
        job["timings"]["write_seconds"] = round(time.time() - t1, 1)
        job["story"] = story
        _update(
            job,
            stage="write",
            progress=38,
            message=f"旁白写好了：{len(story['narration'])} 段",
        )

        t2 = time.time()
        _update(job, stage="voice", progress=42, message="正在生成旁白配音")
        entities = _entity_notes(entry, story.get("entity_notes"))
        narration = _silent_narration(story["narration"])
        if get_capabilities().get("tts"):
            try:
                narration = _synthesize_segments(job, story["narration"], job["voice"])
                _synthesize_entities(job, entities, job["voice"])
            except Exception:
                pass
        if not narration["audio_url"]:
            job["warnings"].append("配音暂不可用，播放器会显示文字旁白。")
        job["timings"]["voice_seconds"] = round(time.time() - t2, 1)
        _update(
            job,
            stage="voice",
            progress=58,
            message=f"配音完成：整页旁白 {narration['duration']} 秒" if narration["audio_url"] else "文字旁白已就绪",
        )

        t3 = time.time()
        _update(job, stage="visual", progress=62, message="正在用本页原图生成动态画面")
        image_path = str(ingest_asset_path(job["book_id"], Path(entry["image"]).name))
        video = _make_dynamic_video(job, entry, story["narration"], image_path)
        job["timings"]["visual_seconds"] = video["seconds"]
        video_url = None
        if video["path"]:
            video_url = _media_url(video["path"])
        else:
            job["warnings"].append(f"{video['error']}，播放时会显示原页静图。")

        image_url = _media_url(image_path)
        storyboard = [
            {
                "index": seg["index"],
                "text": seg["text"],
                "start": seg["start"],
                "end": seg["end"],
                "visual": f"原页图 · 第 {job['page_no']} 页",
                "video_index": 0,
            }
            for seg in narration["segments"]
        ]
        job["result"] = {
            "title": story["title"],
            "image_url": image_url,
            "image_name": Path(image_path).name,
            "image_size": entry.get("image_size"),
            "video_url": video_url,
            "video_error": video["error"],
            "narration": {k: narration[k] for k in ("audio_url", "duration", "segments")},
            "storyboard": storyboard,
            "entities": entities,
            "clean_text": clean_text,
            "text_blocks": clean_blocks,
        }
        job["timings"]["total_seconds"] = round(time.time() - job["started_at"], 1)
        _update(
            job,
            status="ready",
            stage="ready",
            progress=100,
            message="互动绘本已就绪，正在进入播放",
            result=job["result"],
        )
    except IngestError as exc:
        _fail(job, job.get("stage", "identify"), "生成失败", str(exc))
    except Exception:
        _LOG.exception("Quick book job %s failed", job["job_id"])
        stage = job.get("stage", "identify")
        label = next((s["label"] for s in _STAGES if s["key"] == stage), "生成")
        _fail(job, stage, f"{label}失败", "生成过程中发生错误，请重新生成")


def _fail(job, stage, message, reason):
    _update(
        job,
        status="failed",
        stage=stage,
        error=reason,
        error_code="generation_failed",
        message=message,
    )


# ---------------------------------------------------------------------------
# 点读与问答
# ---------------------------------------------------------------------------

def _hit_entity(entities, x, y, pad=0.012):
    hits = []
    for entity in entities:
        try:
            x1, y1, x2, y2 = [float(v) for v in entity.get("bbox")]
        except Exception:
            continue
        if x1 - pad <= x <= x2 + pad and y1 - pad <= y <= y2 + pad:
            hits.append((abs((x2 - x1) * (y2 - y1)), entity))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


def _vision_click(job, x, y):
    image_path = ingest_asset_path(job["book_id"], job["result"]["image_name"])

    with Image.open(image_path) as img:
        width, height = img.size
        cx, cy = min(width - 1, int(x * width)), min(height - 1, int(y * height))
        radius = max(1, round(min(width, height) * 0.28))
        left = max(0, cx - radius)
        top = max(0, cy - radius)
        right = min(width, cx + radius)
        bottom = min(height, cy + radius)
        crop = io.BytesIO()
        img.crop((left, top, right, bottom)).save(crop, format="JPEG", quality=90)
    prompt = (
        f"这是绘本页面的局部截图。用户点击了距左边 {(cx - left) / (right - left):.0%}、"
        f"距上边 {(cy - top) / (bottom - top):.0%} 的位置。"
        "只识别该位置最主要的对象。输出 JSON："
        '{"name_cn":"...","description":"一句儿童化讲解"}。'
        "如果该处没有明确物体，name_cn 返回空字符串。"
    )
    data = vision_request(crop.getvalue(), prompt, retries=0)
    name = str(data.get("name_cn") or "").strip()[:80]
    if not name:
        raise IngestError("这里没有识别到可讲解的内容")
    note = str(data.get("description") or "").strip()[:200] or f"这是{name}。"
    return {"name_cn": name, "note": note}


def point_read(job_id, x, y, allow_vision=True):
    job = get_job(job_id)
    if not job:
        raise IngestError("任务不存在")
    if job.get("status") != "ready":
        raise IngestError("互动绘本还没有准备好")
    try:
        x, y = float(x), float(y)
    except (ValueError, TypeError) as exc:
        raise IngestError("点读位置需要 0 到 1 的坐标") from exc
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise IngestError("点读位置需要 0 到 1 的坐标")
    result = job["result"]
    entities = result.get("entities", [])
    hit = _hit_entity(entities, x, y)
    if hit:
        return {
            "success": True,
            "matched": True,
            "source": "entity",
            "name": hit.get("name_cn"),
            "label": f"🔍 {hit.get('name_cn')}",
            "note": hit.get("note"),
            "audio_url": hit.get("audio_url"),
            "bbox": hit.get("bbox"),
        }

    text = _hit_entity(result.get("text_blocks", []), x, y, pad=0)
    if text:
        return {"success": True, "matched": True, "source": "text", "name": "本页文字",
                "label": "本页文字", "note": text["text"], "audio_url": None, "bbox": text["bbox"]}

    if not allow_vision or not vision_configured():
        return {"success": True, "matched": False, "message": "这里没有可讲解的内容，换个位置点点看"}

    key = f"{round(x * 20) / 20:.2f}_{round(y * 20) / 20:.2f}"
    with _JOBS_LOCK:
        cache = job.setdefault("point_cache", {})
        if key in cache:
            return {**cache[key], "source": "vision-cache"}

    try:
        found = _vision_click(job, x, y)
    except (IngestError, OSError, ValueError):
        return {
            "success": True,
            "matched": False,
            "message": "这里暂时没有识别到可讲解的内容，换个位置点点看",
        }

    half = 0.09
    bbox = [round(max(0, x - half), 4), round(max(0, y - half), 4),
            round(min(1, x + half), 4), round(min(1, y + half), 4)]
    note = found["note"]
    audio_url = _synthesize_interaction(job, note, "points", f"point_{key}.wav")

    payload = {
        "success": True,
        "matched": True,
        "source": "vision",
        "name": found["name_cn"],
        "label": f"🔍 {found['name_cn']}",
        "note": note,
        "audio_url": audio_url,
        "bbox": bbox,
    }
    with _JOBS_LOCK:
        cache[key] = payload
        _save_job(job)
        return payload


def _synthesize_interaction(job, text, directory, filename):
    if not get_capabilities().get("tts"):
        return None
    try:
        root = contained_path(job["book_dir"], "quick", directory)
        audio_path = contained_path(root, filename)
        _synthesize_audio(text, job.get("voice") or DEFAULT_VOICE, audio_path)
        return _media_url(audio_path)
    except Exception:
        return None


def _fallback_answer(question, result):
    for entity in result.get("entities", []):
        name = entity.get("name_cn")
        if name and name in question:
            return entity.get("note") or entity.get("description") or f"图中有{name}。"
    text = result.get("clean_text") or ""
    if text and re.search(r"(?:这[一]?页|本页|当前页).*(?:说|讲|内容|写|原文)|原文|"
                          r"what (?:does|is on) (?:this|the current) page", question, re.I):
        return text
    query = {token for token in _tokens(question) if len(token) > 1}
    best_score, best_sentence = 0, ""
    for sentence in _sentences(text):
        hits = query & _tokens(sentence)
        if len(hits) >= 2 and len(hits) > best_score:
            best_score, best_sentence = len(hits), sentence
    if best_sentence:
        return best_sentence
    return "这一页里没有找到足够的信息。"


def ask_page(job_id, question):
    job = get_job(job_id)
    if not job:
        raise IngestError("任务不存在")
    if job.get("status") != "ready":
        raise IngestError("互动绘本还没有准备好")
    question = str(question or "").strip()
    if not question:
        raise IngestError("请先输入问题")
    if len(question) > 200:
        question = question[:200]

    result = job.get("result") or {}
    entities = result.get("entities", [])
    entity_text = "、".join(
        f"{e.get('name_cn')}（{e.get('description') or e.get('note')}）" for e in entities
    )
    segments = " ".join(s.get("text", "") for s in (result.get("narration") or {}).get("segments", []))
    system_prompt = f"""你是“纸书智伴”的绘本导读老师，只根据下面这一页的内容回答。
【本页正文】{(result.get('clean_text') or '')[:3000] or '（无可识别正文）'}
【本页旁白】{segments}
【画面实体】{entity_text}
要求：用中文回答，1-3 句，60 字以内，适合朗读；不知道就说“这一页里没有讲到”，不要编造。"""
    answer = ""
    for url, key, model in (
        (config.nlu_url, config.nlu_api_key, config.nlu_model),
        (config.simple_story_writer_url, config.simple_story_writer_api_key, config.simple_story_writer_model),
    ):
        if service_configured(url, key, model):
            try:
                answer = _qa_chat(system_prompt, question, url, key, model)
                break
            except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, IndexError, requests.RequestException):
                continue
    source = "model" if answer else "page_content"
    answer = answer or _fallback_answer(question, result)
    answer = re.sub(r"\s+", " ", answer).strip()
    if len(answer) > 100:
        answer = answer[:100]

    audio_url = _synthesize_interaction(job, answer, "qa", f"answer_{uuid.uuid4().hex[:12]}.wav")
    return {"success": True, "answer": answer, "audio_url": audio_url, "source": source}


def _qa_chat(system_prompt, question, url, key, model):
    payload = {
        "model": model,
        "temperature": 0.5,
        "max_tokens": 500,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"问答服务 HTTP {resp.status_code}")
    data = resp.json()
    content = data["choices"][0]["message"].get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content.strip():
        raise RuntimeError("问答服务返回空内容")
    return content
