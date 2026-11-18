"""Exercise the real application and adapters; providers remain controlled."""
import base64
import importlib
import io
from pathlib import Path
import shutil
import subprocess
import time
import wave

import pymupdf
import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv('TARGET_DIR', str(tmp_path / 'application-data'))
    import config
    importlib.reload(config)
    import story_tts_server
    service = importlib.reload(story_tts_server)
    yield service
    service.book_service.close()
    service.video_executor.shutdown(wait=True)


def document():
    pdf = pymupdf.open()
    pdf.new_page(width=300, height=200).insert_text((20, 30), 'Rabbit shares apples.')
    pdf.new_page(width=300, height=200).draw_circle((100, 100), 30)
    data = pdf.tobytes()
    pdf.close()
    return data


def import_book(server):
    client = server.app.test_client()
    response = client.post('/tts-story-book/api/books/import', data={
        'file': (io.BytesIO(document()), 'controlled.pdf'), 'mode': 'fast'})
    assert response.status_code == 202
    result = response.get_json()
    assert server.book_service.wait_for_job(result['job_id'])['status'] == 'succeeded'
    return client, result['book_id']


def test_actual_app_starts_and_reports_capabilities_without_external_calls(server):
    client = server.app.test_client()
    health = client.get('/health').get_json()
    assert health['status'] == 'ok' and health['online_verified'] is False
    assert health['capabilities']['pdf'] is True
    assert not server.service_configured('http://localhost/model', 'None', 'model')
    assert client.get('/tts-story-book/api/books').status_code == 200
    assert client.get('/static/config.py').status_code == 404
    assert client.get('/api/capabilities').get_json()['capabilities']['answer'] is False
    for endpoint in ('/api/nlu', '/api/generate-image', '/api/generate-video'):
        response = client.post(endpoint, json={'question': 'test', 'prompt': 'test'})
        assert response.status_code == 503
        assert response.get_json()['code'] == 'capability_not_configured'


def test_pdf_is_in_mainline_with_real_images_and_unconfigured_qa_is_honest(server):
    client, book_id = import_book(server)
    book = client.get('/api/books/' + book_id).get_json()['book']
    assert len(book['pages']) == 2
    assert book['pages'][1]['text_status'] == 'needs_ocr'
    assert client.get(book['pages'][0]['image_url']).status_code == 200
    response = client.post(f'/api/books/{book_id}/qa', json={'question': 'Rabbit apples'})
    assert response.status_code == 200
    assert response.get_json()['answer_source'] == 'extractive'
    assert client.post(f'/api/books/{book_id}/transcribe', data={}).status_code == 503
    assert client.get(f'/api/books/{book_id}/assets/source.pdf').status_code == 400


def test_quick_pdf_generates_readable_page_and_recovers_from_disk(server, monkeypatch):
    import quick_book

    monkeypatch.setattr(quick_book, 'get_capabilities', lambda: {'tts': False, 'video': False})
    client = server.app.test_client()
    prefix = '/tts-story-book'
    assert b'/quick' in client.get('/').data
    assert client.get(prefix + '/quick').status_code == 200
    upload = client.post(prefix + '/api/quickbook/upload', data={
        'file': (io.BytesIO(document()), 'controlled.pdf')}).get_json()
    assert upload['success'] and upload['page_count'] == 2
    preview = client.post(prefix + '/api/quickbook/preview',
                          json={'upload_id': upload['upload_id'], 'page': 2}).get_json()
    assert preview['page'] == 2
    assert client.get(prefix + preview['image_url']).mimetype == 'image/jpeg'
    assert client.post(prefix + '/api/quickbook/preview',
                       json={'upload_id': upload['upload_id'], 'page': 0}).status_code == 400

    started = client.post(prefix + '/api/quickbook/start',
                          json={'upload_id': upload['upload_id'], 'page': 1}).get_json()
    job_id = started['job_id']
    deadline = time.monotonic() + 10
    while True:
        status = client.get(prefix + f'/api/quickbook/status/{job_id}').get_json()
        if status['status'] != 'running' or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    assert status['status'] == 'ready', status
    with quick_book._JOBS_LOCK:
        quick_book._JOBS.pop(job_id)
    result = client.get(prefix + f'/api/quickbook/result/{job_id}').get_json()
    assert 'Rabbit shares apples.' in result['clean_text']
    assert result['video_url'] is None and result['narration']['audio_url'] is None
    assert client.get(prefix + result['image_url']).status_code == 200
    baseline = f'output_dir/quick_uploads/{upload["upload_id"]}/source.pdf'
    for endpoint in ('/api/quickbook/assets/', '/api/image/', '/api/video/'):
        assert client.get(prefix + endpoint + baseline).status_code == 404
    point = client.post(prefix + '/api/quickbook/point', json={
        'job_id': job_id, 'x': .15, 'y': .14, 'allow_vision': False}).get_json()
    assert point['matched'] and point['source'] == 'text'
    answer = client.post(prefix + '/api/quickbook/ask',
                         json={'job_id': job_id, 'question': 'Rabbit apples'}).get_json()
    assert answer['source'] == 'page_content' and 'Rabbit' in answer['answer']


def test_ingest_accepts_multipart_and_json_and_keeps_page_order(server, monkeypatch):
    import book_ingest
    from PIL import Image

    client = server.app.test_client()
    with io.BytesIO() as raw:
        Image.new('RGB', (24, 16), 'red').save(raw, 'PNG')
        image = raw.getvalue()
    assert client.post('/api/ingest/page', data={'file': (io.BytesIO(image), 'page.png')}).status_code == 503
    monkeypatch.setattr(book_ingest, 'vision_configured', lambda: True)
    monkeypatch.setattr(book_ingest, 'run_ocr', lambda *args, **kwargs: (
        [{'text': 'A red page', 'bbox': [0, 0, 1, 1], 'confidence': .9}], {'engine': 'controlled'}))
    monkeypatch.setattr(book_ingest, 'analyze_page_semantics', lambda *args, **kwargs: {
        'description': 'A red page', 'characters': [], 'scene': '', 'plot_summary': '',
        'style_anchor': '', 'entities': []})
    response = client.post('/api/ingest/book', data={
        'book_id': 'sample', 'orders': '[3,1]',
        'images': [(io.BytesIO(image), 'third.png'), (io.BytesIO(image), 'first.png')]})
    assert response.status_code == 200
    assert [page['page'] for page in response.get_json()['pages']] == [1, 3]
    response = client.post('/api/ingest/page', json={
        'book_id': 'sample', 'page': 2, 'image_base64': base64.b64encode(image).decode()})
    assert response.status_code == 200 and response.get_json()['page']['page'] == 2
    manifest = client.get('/api/book/sample').get_json()
    assert [page['page'] for page in manifest['book']['pages']] == [1, 2, 3]
    assert str(server.TARGET_DIR) not in manifest['manifest_path']
    for page in manifest['book']['pages']:
        assert page['coord_space'] == 'normalized_0_1' and 'image_abs' not in page
        assert client.get(page['image_url']).mimetype == 'image/jpeg'


def test_legacy_point_read_uses_browser_frame_or_server_capture(server, monkeypatch):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg required')
    from PIL import Image

    video = Path(server.TARGET_DIR) / 'output_dir' / 'video.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=160x120:d=0.25',
                    '-c:v', 'libx264', str(video)], check=True, capture_output=True)
    monkeypatch.setattr(server, 'get_capabilities', lambda: {'vision': True})

    def analyze(params):
        with Image.open(params['image_path']) as frame:
            assert frame.format == 'PNG'
            return {'测试': [1, 2, min(640, frame.width), min(200, frame.height)]}

    monkeypatch.setattr(server.analyzer, 'call', analyze)
    client = server.app.test_client()
    route = '/tts-story-book/api/analyze-video-frame'
    request_body = {'video_path': 'output_dir/video.mp4', 'time_seconds': 0}
    with io.BytesIO() as raw:
        Image.new('RGB', (800, 400), 'green').save(raw, 'JPEG')
        uploaded = 'data:image/jpeg;base64,' + base64.b64encode(raw.getvalue()).decode()
    browser = client.post(route, json={**request_body, 'frame_base64': uploaded}).get_json()
    assert browser['frame_source'] == 'browser' and browser['frame_size'] == {'width': 800, 'height': 400}
    assert browser['objects']['测试'] == [1, 2, 640, 200]
    fallback = client.post(route, json=request_body).get_json()
    assert fallback['frame_source'] == 'server' and fallback['frame_size'] == {'width': 160, 'height': 120}
    assert client.post(route, json={**request_body, 'frame_base64': 'invalid!'}).status_code == 400


def test_recognition_contract_and_speak_use_real_book_assets(server, monkeypatch):
    client, book_id = import_book(server)
    original = server.get_capabilities()
    monkeypatch.setattr(server, 'get_capabilities', lambda: {**original, 'vision': True, 'tts': True})
    from coordinates import object_contract
    from PIL import Image
    def analyze(params):
        with Image.open(params['image_path']) as image:
            width, height = image.size
        return object_contract({'detections': [{'label': 'rabbit', 'box': [5, 5, 70, 80]}]},
                               width, height, params['source_id'])
    monkeypatch.setattr(server.analyzer, 'call_with_metadata', analyze)
    response = client.post(f'/api/books/{book_id}/recognize', json={
        'page_id': 'p0001', 'source': 'image', 'request_id': 'current-1'})
    result = response.get_json()
    assert response.status_code == 200 and result['request_id'] == 'current-1'
    assert result['objects']['coordinate_space'] == 'pixel'
    assert result['objects']['detections'][0]['box'] == [5, 5, 70, 80]
    assert client.post(f'/api/books/{book_id}/recognize', json={
        'page_id': 'p9999'}).status_code == 404
    def synthesize(text, voice, output_path=None, **kwargs):
        with wave.open(str(output_path), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 4800)
        return output_path
    monkeypatch.setattr(server.local_tts, 'synthesize', synthesize)
    spoken = client.post(f'/api/books/{book_id}/speak', json={'text': 'rabbit', 'voice': 'xiaoyuan'})
    assert spoken.status_code == 200 and spoken.get_json()['duration'] == pytest.approx(.2)
    assert client.get(spoken.get_json()['audio_url']).status_code == 200


def test_legacy_force_reaches_generator_and_cache_tracks_input(server, monkeypatch, tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg required')
    clip = tmp_path / 'source.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=160x120:r=24:d=0.25', '-an', '-c:v', 'libx264', str(clip)],
                   check=True, capture_output=True)
    image = tmp_path / 'image.bin'
    image.write_bytes(b'image-one')
    session = Path(server.TARGET_DIR) / 'output_dir' / 'legacy'
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        shutil.copyfile(clip, kwargs['output_path'])
        return kwargs['output_path']
    monkeypatch.setattr(server.video_generator, 'generate', generate)
    first = server.generate_video_task('one', str(image), str(session), 0)
    assert Path(first).is_file()
    assert server.generate_video_task('one', str(image), str(session), 0) == first and len(calls) == 1
    server.generate_video_task('one', str(image), str(session), 0, force=True)
    assert len(calls) == 2
    image.write_bytes(b'image-two')
    server.generate_video_task('one', str(image), str(session), 0)
    assert len(calls) == 3
    assert all(item['resolution'] == '720p' for item in calls)


def test_optional_sound_effects_do_not_disable_ordinary_narration(server, monkeypatch):
    caps = server.get_capabilities()
    monkeypatch.setattr(server, 'get_capabilities', lambda: {**caps, 'tts': True, 'sfx': False})
    calls = []
    def synthesize(text, voice, output_path=None, **kwargs):
        calls.append(text)
        path = Path(output_path)
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 4800)
        return str(path)
    monkeypatch.setattr(server.local_tts, 'synthesize', synthesize)
    monkeypatch.setattr(server, 'sfx_generator', None)
    client = server.app.test_client()
    result = client.post('/api/generate-audio', json={
        'text': '普通朗读不需要音效服务。', 'voice': 'xiaoyuan', 'session_dir': 'output_dir/narration-only'})
    assert result.status_code == 200, result.get_json()
    assert calls == ['普通朗读不需要音效服务。']
    assert client.get(result.get_json()['audio_url']).status_code == 200
    assert not Path(result.get_json()['audio_path']).is_absolute()
