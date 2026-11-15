"""Real HTTP transport against a loopback provider, without external API use."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from PIL import Image
import pytest

from gemini_analyze import ImageObjectAnalyzer
from nlu import QAAssistant


def test_vision_and_grounded_qa_use_actual_http_and_reject_invalid_contracts(tmp_path, monkeypatch):
    state = {'requests': [], 'bad_vision': False, 'bad_citation': False}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append((self.path, payload))
            if self.path == '/vision':
                objects = {'unexpected': 'wrong schema'} if state['bad_vision'] else {
                    'coordinate_space': 'pixel', 'reference_width': 864, 'reference_height': 480,
                    'detections': [{'label': 'rabbit', 'box': [100, 100, 600, 400]}]}
                content = '```json\n' + json.dumps(objects, indent=2) + '\n```'
            else:
                content = '送给小狐狸。[p9999]' if state['bad_citation'] else '送给小狐狸。[p0001]'
            body = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv('NO_PROXY', 'localhost,127.0.0.1')
    try:
        base = f'http://127.0.0.1:{httpd.server_port}'
        image = tmp_path / 'page.png'
        Image.new('RGB', (864, 480), 'white').save(image)
        analyzer = ImageObjectAnalyzer(base + '/vision', 'controlled-key', 'controlled-vision')
        objects = analyzer.call_with_metadata({'image_path': str(image), 'source_id': 'p0001'})
        assert objects['detections'][0]['box'] == [100, 100, 600, 400]
        assert state['requests'][0][1]['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
        state['bad_vision'] = True
        with pytest.raises(ValueError):
            analyzer.call_with_metadata({'image_path': str(image)})
        qa = QAAssistant(base + '/text', 'controlled-key', 'controlled-text')
        context = [{'page_id': 'p0001', 'excerpt': '小兔子把红苹果送给小狐狸。'}]
        assert qa.call('送给谁？', context=context) == '送给小狐狸。[p0001]'
        assert json.loads(state['requests'][-1][1]['messages'][1]['content'])['evidence'] == context
        state['bad_citation'] = True
        with pytest.raises(ValueError):
            qa.call('送给谁？', context=context)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=3)
