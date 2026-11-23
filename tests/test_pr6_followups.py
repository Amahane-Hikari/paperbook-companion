from app.book_pipeline import _pinyin_hits
from tests.test_book_pipeline import media, pipeline  # noqa: F401  (pytest fixture)
from tests.test_pdf_reader import ControlledReader, mixed_pdf


def test_pinyin_fallback_matches_homophones_but_not_function_words():
    page = '小兔子把积木搭成了高高的塔，妈妈在那里看着它。'
    assert _pinyin_hits('鸡目在哪里', page) >= 1
    assert _pinyin_hits('火箭在哪里', page) == 0
    assert _pinyin_hits('他在干什么', page) == 0


def test_pinyin_fallback_respects_syllable_boundaries():
    # 西安 = xi an，不能匹配 先 = xian
    assert _pinyin_hits('西安', '我们先走吧，小狗先到了') == 0


class FailingVisionReader(ControlledReader):
    def analyze_page_scene(self, path, book_text=''):
        raise RuntimeError('timeout')

    def detect_objects(self, path, max_objects=8, book_text=''):
        raise RuntimeError('timeout')


def test_full_mode_vision_failures_mark_book_degraded(tmp_path):
    result = FailingVisionReader().read_book(mixed_pdf(tmp_path / 'mixed.pdf'), tmp_path / 'book', mode='full')
    assert result['status'] == 'degraded'
    assert {w['page_id'] for w in result['warnings']} == {p['page_id'] for p in result['pages']}
    assert all({'stage': 'scene', 'code': 'request_failed'} in w['errors'] for w in result['warnings'])


def test_anchor_citations_carry_export_timeline(pipeline):
    import io

    import fitz

    client, service, *_ = pipeline
    document = fitz.open()
    for line in ('Rabbit Bob lives here.', 'Fox Amy visits.', 'He shares apples.'):
        document.new_page(width=300, height=100).insert_text((10, 30), line, fontsize=8)
    data = document.tobytes()
    document.close()
    response = client.post('/api/books/import', data={'file': (io.BytesIO(data), 'anchors.pdf'),
                                                      'mode': 'auto', 'verify_ocr': 'false'})
    book = response.get_json()['book_id']
    assert service.wait_for_job(response.get_json()['job_id'])['status'] == 'succeeded'
    assert media(client, service, book)['status'] == 'succeeded'
    export = client.post(f'/api/books/{book}/export', json={})
    assert service.wait_for_job(export.get_json()['job_id'])['status'] == 'succeeded'
    service.adapters['answer'] = lambda *args: 'Bob [p0001] shares apples [p0003].'
    qa = client.post(f'/api/books/{book}/qa', json={'question': 'apples'}).get_json()
    anchor = next(c for c in qa['citations'] if c['page_id'] == 'p0001')
    assert anchor['time_status'] == 'export_timeline' and anchor['start'] == 0
