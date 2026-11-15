"""Observe decoded pictures and final-page speech, not just container duration."""
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import numpy as np
import pytest

from app.book_video import export_book


def test_twenty_fractional_pages_preserve_every_picture_and_last_narration(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('real FFmpeg and ffprobe required')
    media = tmp_path / 'media'
    media.mkdir()
    for color in ('red', 'blue', 'green'):
        subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                        f'color=c={color}:s=160x120:r=24:d=0.5', '-an', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(media / f'{color}.mp4')], check=True, capture_output=True)
    for name, frequency in (('speech', 440), ('last', 880)):
        samples = b''.join(struct.pack('<h', round(12000 * math.sin(2 * math.pi * frequency * n / 24000)))
                           for n in range(2640))
        with wave.open(str(media / f'{name}.wav'), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(samples)
    pages = []
    for index in range(20):
        color = 'green' if index == 19 else ('red' if index % 2 == 0 else 'blue')
        audio = 'media/last.wav' if index == 19 else 'media/speech.wav'
        pages.append({'page_id': f'p{index + 1:04d}', 'page_num': index + 1, 'text': str(index + 1),
                      'media': {'tts': {'status': 'ready', 'path': audio,
                                        'sentences': [{'text': str(index + 1), 'start': 0, 'end': .11,
                                                       'duration': .11, 'audio_path': audio}]},
                                'video': {'status': 'ready', 'path': f'media/{color}.mp4'}}})
    result = export_book(tmp_path, {'pages': pages}, {'width': 160, 'height': 120, 'fps': 24})
    movie = tmp_path / result['video_path']
    pixels = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(movie), '-vf', 'scale=1:1',
                                       '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    colors = [max(range(3), key=lambda channel: pixels[index + channel]) for index in range(0, len(pixels), 3)]
    runs = [(0, colors[0])]
    runs.extend((index, color) for index, color in enumerate(colors[1:], 1) if color != colors[index - 1])
    assert len(runs) == 20 and runs[-1][1] == 1  # green final page survives
    timeline = json.loads((tmp_path / result['timeline_path']).read_text('utf-8'))
    assert timeline['duration'] == pytest.approx(2.2)
    assert abs(result['probe']['duration'] - 2.2) <= 1 / 24 + .03
    for (frame, _), page in zip(runs, timeline['pages']):
        assert frame == page['video_start_frame']
        assert frame / 24 == pytest.approx(page['video_start'])
        assert abs(page['video_start'] - page['book_start']) <= .5 / 24 + 1e-9
        assert 'video_start' in page['frame_time_mapping']
        assert page['sentences'][0]['timing_source'] == 'measured_sentence_wav'
    assert timeline['pages'][1]['sentences'][0]['frame_start'] is None
    audio_bytes = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(movie), '-map', '0:a:0',
                                          '-ac', '1', '-ar', '24000', '-f', 'f32le', '-'])
    samples = np.frombuffer(audio_bytes, dtype='<f4')
    last = samples[round(2.13 * 24000):round(2.19 * 24000)]
    assert len(last) > 1000
    dominant = np.argmax(np.abs(np.fft.rfft(last))) * 24000 / len(last)
    assert 800 < dominant < 950  # last-page speech was not cut or replaced by earlier audio
