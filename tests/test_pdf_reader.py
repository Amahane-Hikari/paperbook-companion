import io
from pathlib import Path

import fitz
from PIL import Image, ImageDraw
import pytest

from app.pdf_book_reader import PDFBookReader, normalize_objects


def mixed_pdf(path):
    image = Image.new('RGB', (300, 200), 'white')
    ImageDraw.Draw(image).text((15, 70), 'Scanned fox in a forest', fill='black')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    doc = fitz.open()
    doc.new_page(width=300, height=200).insert_text((20, 40), 'Rabbit shares apples.')
    doc.new_page(width=300, height=200).insert_image(fitz.Rect(0, 0, 300, 200), stream=buffer.getvalue())
    doc.new_page(width=300, height=200).draw_circle(fitz.Point(100, 100), 30)
    doc.save(path)
    doc.close()
    return path


class ControlledReader(PDFBookReader):
    def __init__(self):
        super().__init__(dpi=72)
        self.calls = []

    def ocr_page(self, path):
        page = Path(path).stem
        self.calls.append(('ocr', page))
        return {'p0001': 'Rabbit shares apples.', 'p0002': 'A fox lives in the forest.', 'p0003': '[无文字页面]'}[page]

    def verify_ocr(self, path, text):
        self.calls.append(('verify', Path(path).stem))
        return text, True

    def analyze_page_scene(self, path, book_text=''):
        self.calls.append(('scene', Path(path).stem))
        return {'summary': 'Visible shapes, inferred by the test model', 'scene': 'forest',
                'characters': [], 'key_objects': []}

    def detect_objects(self, path, max_objects=8, book_text=''):
        self.calls.append(('objects', Path(path).stem))
        return {'coordinate_space': 'pixel', 'detections': [{'label': 'shape', 'box': [1, 2, 50, 60]}]}


def test_real_fast_mixed_pdf_keeps_every_original_page(tmp_path):
    pdf = mixed_pdf(tmp_path / 'mixed.pdf')
    reader = ControlledReader()
    result = reader.read_book(pdf, tmp_path / 'book', mode='fast')
    assert len(result['pages']) == 3
    assert [p['page_id'] for p in result['pages']] == ['p0001', 'p0002', 'p0003']
    assert [p['page_num'] for p in result['pages']] == [1, 2, 3]
    assert result['pages'][0]['text'] == 'Rabbit shares apples.'
    assert [p['text_status'] for p in result['pages']] == ['pdf_text', 'needs_ocr', 'needs_ocr']
    assert reader.calls == []
    assert result['status'] == 'needs_ocr'
    for page in result['pages']:
        assert (tmp_path / 'book' / page['image_path']).is_file()
        assert page['image_size'] == {'width': 300, 'height': 200}


def test_auto_ocr_only_missing_text_and_distinguishes_confirmed_empty(tmp_path):
    reader = ControlledReader()
    result = reader.read_book(mixed_pdf(tmp_path / 'mixed.pdf'), tmp_path / 'book', mode='auto', verify_ocr=True)
    assert ('ocr', 'p0001') not in reader.calls
    assert ('ocr', 'p0002') in reader.calls and ('ocr', 'p0003') in reader.calls
    assert ('verify', 'p0003') in reader.calls
    assert result['pages'][1]['text'] == 'A fox lives in the forest.'
    assert result['pages'][2]['text_status'] == 'empty'
    assert result['status'] == 'ready'


def test_full_runs_all_vision_stages_even_without_text(tmp_path):
    reader = ControlledReader()
    result = reader.read_book(mixed_pdf(tmp_path / 'mixed.pdf'), tmp_path / 'book', mode='full', verify_ocr=True)
    for page in result['pages']:
        for stage in ('ocr', 'verify', 'scene', 'objects'):
            assert (stage, page['page_id']) in reader.calls
        assert page['objects']['source_id'] == page['page_id']
        assert page['objects']['coordinate_space'] == 'pixel'
        assert page['scene_analysis']['inferred'] is True
        assert page['scene_analysis']['source'] == 'vision_model'
    assert result['pages'][2]['text'] == ''


def test_missing_config_and_ocr_failure_are_explicit(tmp_path, monkeypatch):
    pdf = mixed_pdf(tmp_path / 'mixed.pdf')
    monkeypatch.setattr('app.pdf_book_reader.requests.post', lambda *a, **k: pytest.fail('unexpected network'))
    result = PDFBookReader(dpi=72).read_book(pdf, tmp_path / 'book', mode='full')
    assert result['status'] == 'needs_ocr'
    assert result['pages'][0]['text_status'] == 'pdf_text_unverified'
    assert all(p['scene_analysis']['status'] == 'not_configured' for p in result['pages'])
    assert all(p['objects']['status'] == 'not_configured' for p in result['pages'])
    reader = ControlledReader()
    reader.ocr_page = lambda path: (_ for _ in ()).throw(RuntimeError('controlled failure'))
    result = reader.read_book(pdf, tmp_path / 'failed', mode='auto')
    assert result['status'] == 'degraded'
    assert result['pages'][1]['text_status'] == 'ocr_failed'
    assert result['pages'][2]['text_status'] != 'empty'


def test_limit_rejects_instead_of_silently_truncating(tmp_path):
    with pytest.raises(ValueError, match='页数'):
        PDFBookReader().read_book(mixed_pdf(tmp_path / 'mixed.pdf'), tmp_path / 'book', max_pages=2)
    with pytest.raises(ValueError, match='mode'):
        PDFBookReader().read_book(tmp_path / 'mixed.pdf', tmp_path / 'book', mode='invalid')


def test_pixel_boxes_are_not_guessed_from_numeric_range():
    result = normalize_objects({'coordinate_space': 'pixel', 'detections': [{'label': 'rabbit', 'box': [100, 100, 600, 400]}]}, 864, 480, 'p0001')
    assert result['detections'][0]['box'] == [100, 100, 600, 400]
    normalized = normalize_objects({'coordinate_space': 'normalized_1000', 'detections': [{'label': 'rabbit', 'box': [0, 0, 1000, 1000]}]}, 864, 480, 'p0001')
    assert normalized['detections'][0]['box'] == [0, 0, 864, 480]
    with pytest.raises(ValueError):
        normalize_objects({'coordinate_space': 'guess', 'detections': [{'label': 'bad', 'box': [0, 0, 2000, 500]}]}, 864, 480, 'p0001')


def test_empty_ocr_and_unknown_vision_json_never_become_ready(tmp_path):
    reader = ControlledReader()
    reader.ocr_page = lambda path: '   '
    reader.analyze_page_scene = lambda path: {'error': 'service unavailable'}
    reader.detect_objects = lambda path, max_objects=8: {'unexpected': 'not detections'}
    result = reader.read_book(mixed_pdf(tmp_path / 'mixed.pdf'), tmp_path / 'book', mode='full')
    assert result['status'] == 'degraded'
    assert all(page['text_status'] == 'ocr_failed' for page in result['pages'])
    assert all(page['scene_analysis']['status'] == 'request_failed' for page in result['pages'])
    assert all(page['objects']['status'] == 'request_failed' for page in result['pages'])
    assert len(result['warnings']) == 3
    for malformed in ({}, {'unexpected': 'not detections'}, {'rabbit': [1, 2, 3, 4], 'error_info': 'bad'}):
        with pytest.raises(ValueError):
            normalize_objects(malformed, 864, 480, 'p0001')
    assert normalize_objects({'detections': []}, 864, 480, 'p0001')['detections'] == []
