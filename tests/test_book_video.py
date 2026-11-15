import json
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import pytest

from app.book_video import (MediaError, concatenate_wav, export_book, probe_media,
                            wav_duration, write_silence)


def wav(path, duration, rate=24000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 1000) * round(duration * rate))
    return path


def clip(path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and ffprobe are required for real media verification')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([shutil.which('ffmpeg'), '-y', '-v', 'error', '-f', 'lavfi',
                    '-i', 'color=c=green:s=160x120:r=24:d=0.25', '-an', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
    return path


def manifest_for_export(root):
    root = Path(root)
    clip(root / 'media/p0001/clip.mp4')
    clip(root / 'media/p0002/clip.mp4')
    wav(root / 'media/p0001/a.wav', .25)
    wav(root / 'media/p0001/b.wav', .30)
    duration = concatenate_wav([root / 'media/p0001/a.wav', root / 'media/p0001/b.wav'], root / 'media/p0001/narration.wav')
    write_silence(root / 'media/p0002/narration.wav', .75)
    return {'pages': [
        {'page_id': 'p0001', 'page_num': 1, 'text': 'Rabbit. Apples.', 'media': {
            'tts': {'status': 'ready', 'path': 'media/p0001/narration.wav', 'duration': duration,
                    'sentences': [{'text': 'Rabbit.', 'start': 0, 'end': .25, 'duration': .25},
                                  {'text': 'Apples.', 'start': .25, 'end': .55, 'duration': .3}]},
            'video': {'status': 'ready', 'path': 'media/p0001/clip.mp4'}}},
        {'page_id': 'p0002', 'page_num': 2, 'text': '', 'media': {
            'tts': {'status': 'silent', 'path': 'media/p0002/narration.wav', 'duration': .75, 'sentences': []},
            'video': {'status': 'ready', 'path': 'media/p0002/clip.mp4'}}}]}


def test_real_ffmpeg_export_contains_audio_video_subtitles_and_timeline(tmp_path):
    manifest = manifest_for_export(tmp_path)
    result = export_book(tmp_path, manifest)
    probe = probe_media(tmp_path / result['video_path'])
    assert probe['width'] == 1280 and probe['height'] == 720
    assert probe['fps'] == 24
    assert probe['video_codec'] == 'h264' and probe['audio_codec'] == 'aac'
    assert abs(probe['duration'] - 1.3) < .25
    timeline = json.loads((tmp_path / result['timeline_path']).read_text('utf8'))
    assert timeline['duration'] == pytest.approx(1.3)
    assert timeline['pages'][1]['book_start'] == pytest.approx(.55)
    assert timeline['pages'][0]['sentences'][1]['book_start'] == .25
    assert timeline['pages'][0]['sentences'][1]['frame_start'] < .26
    assert 'frame_time' in timeline['pages'][0]['frame_time_mapping']
    subtitle = (tmp_path / result['srt_path']).read_text('utf8')
    assert '00:00:00,250 --> 00:00:00,550' in subtitle and 'Apples.' in subtitle
    assert not list((tmp_path / 'exports').glob('.export-*'))


def test_export_cache_and_force_rebuild(tmp_path, monkeypatch):
    import app.book_video as video
    manifest = manifest_for_export(tmp_path)
    first = export_book(tmp_path, manifest, {'width': 320, 'height': 180, 'fps': 24})
    original = video._run
    calls = []
    monkeypatch.setattr(video, '_run', lambda args, timeout=300: (calls.append(args), original(args, timeout))[1])
    second = export_book(tmp_path, manifest, {'width': 320, 'height': 180, 'fps': 24})
    assert second['cache_key'] == first['cache_key'] and second['cached'] is True
    assert not any('libx264' in args for args in calls)
    third = export_book(tmp_path, manifest, {'width': 320, 'height': 180, 'fps': 24}, force=True)
    assert third['cached'] is False
    assert any('libx264' in args for args in calls)


def test_missing_tools_and_missing_narration_fail_clearly(tmp_path):
    with pytest.raises(MediaError, match='找不到 ffprobe'):
        probe_media(wav(tmp_path / 'one.wav', .1), ffprobe=str(tmp_path / 'missing-tool'))
    manifest = manifest_for_export(tmp_path / 'book')
    manifest['pages'][0]['media']['tts']['status'] = 'silent'
    with pytest.raises(MediaError, match='有文字页'):
        export_book(tmp_path / 'book', manifest)
    manifest['pages'][0]['media']['tts']['status'] = 'failed'
    with pytest.raises(MediaError, match='未完成'):
        export_book(tmp_path / 'book', manifest)


def test_wav_measurement_format_validation_and_path_bounds(tmp_path):
    a, b = wav(tmp_path / 'a.wav', .123), wav(tmp_path / 'b.wav', .234)
    assert wav_duration(a) == pytest.approx(.123)
    assert concatenate_wav([a, b], tmp_path / 'joined.wav') == pytest.approx(.357)
    other = wav(tmp_path / 'other.wav', .1, rate=16000)
    with pytest.raises(MediaError, match='不一致'):
        concatenate_wav([a, other], tmp_path / 'bad.wav')
    with pytest.raises(MediaError, match='PCM WAV'):
        wav_duration(tmp_path / 'missing.wav')
    with pytest.raises(ValueError):
        write_silence(tmp_path / 'silence.wav', float('nan'))
    from app.book_video import _inside
    with pytest.raises(MediaError):
        _inside(tmp_path, '../outside.wav')
