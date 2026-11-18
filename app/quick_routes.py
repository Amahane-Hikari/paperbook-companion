"""HTTP entry points for single-page PDF reading and photographed books."""
import base64
import binascii
import json
import math
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, send_from_directory

import book_ingest
import config
import quick_book
from book_ingest import IngestError, VisionError


routes = Blueprint('quick_book', __name__)
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _json_body():
    if not request.is_json:
        return {}
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise IngestError('请求必须是 JSON 对象')
    return payload


def _boolean(value):
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _public_page(page):
    return {name: value for name, value in page.items() if name != 'image_abs'}


def _decode_image(value):
    if not isinstance(value, str):
        raise IngestError('图片必须是 base64 字符串')
    encoded = value.strip()
    if encoded.startswith('data:'):
        header, _, encoded = encoded.partition(',')
        if not header.startswith('data:image/') or not header.endswith(';base64'):
            raise IngestError('图片 data URL 无效')
    if not encoded or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise IngestError('图片为空或超过 16 MiB')
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise IngestError('图片 base64 无效') from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise IngestError('图片为空或超过 16 MiB')
    return data


def _page_items(payload):
    files = [file for name in ('images', 'files', 'pages', 'file')
             for file in request.files.getlist(name)]
    if files:
        if len(files) > config.BOOK_MAX_PAGES:
            raise IngestError('图片数量超过上限')
        orders = request.form.get('orders') or request.form.get('order')
        parsed = [None] * len(files)
        if orders:
            try:
                if orders.lstrip().startswith('['):
                    parsed = json.loads(orders)
                else:
                    parsed = [value.strip() for value in orders.replace(';', ',').split(',')]
            except ValueError as exc:
                raise IngestError('orders 参数无法解析') from exc
            if len(parsed) != len(files):
                raise IngestError('orders 数量必须与图片数量一致')
        items = []
        for file, order in zip(files, parsed):
            data = file.read(MAX_IMAGE_BYTES + 1)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise IngestError('图片为空或超过 16 MiB')
            items.append({'source': data, 'filename': file.filename, 'order': order})
        return items

    images = payload.get('images') or payload.get('images_base64') or payload.get('pages')
    if not images:
        images = payload.get('image_base64') or payload.get('base64') or []
    if isinstance(images, (str, dict)):
        images = [images]
    if not isinstance(images, list) or len(images) > config.BOOK_MAX_PAGES:
        raise IngestError('images 必须是页图数组，且数量在上限内')
    items = []
    for index, entry in enumerate(images, 1):
        if isinstance(entry, str):
            entry = {'image_base64': entry, 'order': index}
        if not isinstance(entry, dict):
            raise IngestError('页图必须是 base64 字符串或对象')
        items.append({
            'source': _decode_image(entry.get('image_base64') or entry.get('base64') or entry.get('image')),
            'filename': entry.get('filename') or f'page{index}.jpg',
            'order': entry.get('order'),
        })
    return items


@routes.errorhandler(IngestError)
def _ingest_error(error):
    return jsonify(success=False, error=str(error)), 400


@routes.errorhandler(VisionError)
def _vision_error(error):
    return jsonify(success=False, error='页图识别失败，请检查视觉服务配置后重试'), 502


@routes.errorhandler(quick_book.UploadTooLarge)
def _upload_limit(error):
    return jsonify(success=False, error=str(error)), 413


@routes.errorhandler(FileNotFoundError)
def _missing_file(error):
    return jsonify(success=False, error='文件不存在'), 404


@routes.get('/quick')
@routes.get('/quick.html')
def quick_home():
    return send_from_directory(Path(__file__).resolve().parent, 'quick.html')


@routes.post('/api/ingest/page')
@routes.post('/api/ingest/book')
def ingest():
    payload = _json_body()
    book_id = payload.get('book_id', request.form.get('book_id'))
    title = payload.get('title', request.form.get('title'))
    if any(value is not None and not isinstance(value, str) for value in (book_id, title)):
        raise IngestError('书本标识和标题必须是字符串')
    if title and len(title) > 200:
        raise IngestError('标题最多 200 字')
    deskew = _boolean(payload.get('deskew', request.form.get('deskew')))
    items = _page_items(payload)
    if not items:
        raise IngestError('请上传至少一张页图')
    single_page = request.path.endswith('/page')
    if single_page and len(items) != 1:
        raise IngestError('单页接口只接收一张页图')
    if single_page:
        items[0]['order'] = book_ingest.checked_page_number(payload.get('page', request.form.get('page', 1)))
    if not book_ingest.vision_configured():
        return jsonify(success=False, error='页图结构化需要配置视觉服务',
                       code='capability_not_configured', capability='vision'), 503
    retries = payload.get('retries', request.form.get('retries', 2))
    if str(retries) not in {'1', '2', '3'}:
        raise IngestError('retries 必须为 1 至 3')
    result = book_ingest.ingest_book(items, book_id=book_id, title=title, deskew=deskew, retries=int(retries))
    pages = [_public_page(page) for page in result['manifest']['pages']]
    info = {'success': True, 'book_id': result['book_id'], 'page_count': len(pages),
            'manifest_path': book_ingest.rel_to_target(result['manifest_path'])}
    if single_page:
        page_no = items[0]['order']
        return jsonify(**info, page=next(page for page in pages if page['page'] == page_no))
    return jsonify(**info, coord_space=book_ingest.COORD_SPACE, pages=pages, summary=[
        {'page': page['page'], 'image': page['image'], 'image_url': page.get('image_url'),
         'text_block_count': len(page.get('text_blocks', [])), 'entity_count': len(page.get('entities', [])),
         'ocr_engine': page.get('ocr', {}).get('engine')}
        for page in pages
    ])


@routes.get('/api/book/<book_id>')
def ingest_manifest(book_id):
    manifest = book_ingest.load_manifest(book_id)
    if not manifest:
        raise FileNotFoundError
    manifest = {**manifest, 'pages': [_public_page(page) for page in manifest['pages']]}
    return jsonify(success=True, book_id=book_id, book=manifest,
                   manifest_path=book_ingest.rel_to_target(book_ingest.manifest_abs_path(book_id)))


@routes.get('/api/ingest/assets/<book_id>/<filename>')
def ingest_asset(book_id, filename):
    return send_file(book_ingest.asset_path(book_id, filename))


@routes.get('/api/quickbook/assets/<path:relative>')
def quick_asset(relative):
    return send_file(quick_book.asset_path(relative))


@routes.post('/api/quickbook/upload')
def quick_upload():
    upload = request.files.get('file') or request.files.get('pdf')
    if not upload:
        raise IngestError('请选择一个 PDF 文件')
    return jsonify(quick_book.create_upload(upload))


@routes.post('/api/quickbook/preview')
@routes.post('/api/quickbook/start')
def quick_page():
    payload = _json_body()
    upload_id = payload.get('upload_id')
    if not isinstance(upload_id, str) or not upload_id:
        raise IngestError('缺少上传文件标识，请重新上传')
    page = payload.get('page', 1)
    if request.path.endswith('/preview'):
        return jsonify(quick_book.render_preview(upload_id, page))
    voice = payload.get('voice') or quick_book.DEFAULT_VOICE
    if not isinstance(voice, str) or len(voice) > 100:
        raise IngestError('音色无效')
    return jsonify(quick_book.start_job(upload_id, page, voice=voice))


@routes.get('/api/quickbook/status/<job_id>')
@routes.get('/api/quickbook/result/<job_id>')
def quick_job(job_id):
    job = quick_book.get_job(job_id)
    if not job:
        return jsonify(success=False, error='这个生成任务不存在或已失效'), 404
    if request.path.startswith('/api/quickbook/status/'):
        return jsonify(quick_book.public_status(job))
    if job.get('status') != 'ready':
        status = quick_book.public_status(job)
        return jsonify(success=False, error=status['error'] or '互动绘本还没有生成完成',
                       stage=status['stage']), 409
    return jsonify(quick_book.public_result(job))


@routes.post('/api/quickbook/point')
@routes.post('/api/quickbook/ask')
def quick_interact():
    payload = _json_body()
    job_id = payload.get('job_id')
    if not isinstance(job_id, str) or not job_id:
        raise IngestError('缺少任务标识')
    if request.path.endswith('/ask'):
        question = payload.get('question')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 200:
            raise IngestError('问题必须在 1 至 200 字内')
        return jsonify(quick_book.ask_page(job_id, question.strip()))
    try:
        x, y = float(payload.get('x')), float(payload.get('y'))
    except (TypeError, ValueError, OverflowError) as exc:
        raise IngestError('点读坐标无效') from exc
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x, y)):
        raise IngestError('点读坐标必须在 0 至 1 之间')
    return jsonify(quick_book.point_read(job_id, x, y, allow_vision=_boolean(payload.get('allow_vision', True))))
