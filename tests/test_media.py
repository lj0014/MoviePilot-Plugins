"""Metadata probing and timing selection regressions, run for both generations."""
import importlib.util
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

DIRECTORY = Path(__file__).resolve().parents[1] / 'plugins.v2/thundersubtitles'
sys.path.insert(0, str(DIRECTORY))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

m = load('thunder_media_checks', DIRECTORY / 'media.py')
t = load('thunder_timing_checks', DIRECTORY / 'thunder.py')


class MediaTests(unittest.TestCase):
    def test_chinese_bilingual_and_unknown_labels(self):
        chinese = {'tags': {'language': 'zho', 'title': '简体中文'}}
        bilingual = {'tags': {'language': 'chi', 'title': '简体中文 English'}}
        self.assertTrue(m.embedded_subtitle([chinese]))
        self.assertFalse(m.embedded_subtitle([chinese], 'bilingual'))
        self.assertTrue(m.embedded_subtitle([bilingual], 'bilingual'))
        self.assertTrue(m.embedded_subtitle([{'tags': {'title': 'CHS_ENG'}}], 'bilingual'))
        self.assertTrue(m.embedded_subtitle([{'tags': {'language': 'und', 'title': '中英双语'}}], 'bilingual'))
        for stream in ({'tags': {'language': 'eng'}}, {'tags': {'language': 'und'}}, {},
                       {'tags': {'language': 'zho', 'title': '双语'}, 'disposition': {'forced': 1}},
                       {'tags': {'language': 'zh-CN', 'title': '特效字幕'}},
                       {'tags': {'language': 'chi', 'title': 'Signs and Songs'}},
                       {'tags': {'language': 'zh', 'title': 'Commentary'}}):
            self.assertFalse(m.embedded_subtitle([stream]))
        self.assertFalse(m.embedded_subtitle([chinese], 'off'))
        self.assertFalse(m.embedded_subtitle([{'tags': {'language': 'chi', 'title': '双语'}}], 'bilingual'))

    def test_probe_reads_only_metadata_with_safe_argument_list(self):
        payload = {'streams': [{'tags': {'language': 'chi'}}], 'format': {'duration': '7200.25'}}
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(payload).encode(), None)
        process.poll.return_value = 0
        with patch.object(m.shutil, 'which', return_value='/bin/ffprobe'), patch.object(m.subprocess, 'Popen', return_value=process) as start:
            info = m.probe_media(Path('movie $(test).mkv'))
        self.assertEqual(info['duration'], 7200.25)
        self.assertTrue(m.embedded_subtitle(info['streams']))
        args = start.call_args.args[0]
        self.assertNotIn('-show_frames', args)
        self.assertNotIn('-show_packets', args)
        self.assertIn('-protocol_whitelist', args)
        self.assertNotIn('shell', start.call_args.kwargs)

    def test_probe_failures_preserve_download_fallback(self):
        with patch.object(m.shutil, 'which', return_value=None), patch.object(m.subprocess, 'Popen') as start:
            self.assertIn('ffprobe', m.probe_media('movie.mkv')['error'])
            start.assert_not_called()
        for output in (b'invalid json', b'[]', b'{"format":[],"streams":null}'):
            process = Mock(returncode=0)
            process.communicate.return_value = (output, None)
            process.poll.return_value = 0
            with patch.object(m.shutil, 'which', return_value='/bin/ffprobe'), patch.object(m.subprocess, 'Popen', return_value=process):
                self.assertIsNone(m.probe_media('movie.mkv')['duration'])
        for value in ('NaN', 'Infinity', '-1', 0, None):
            self.assertIsNone(m.positive_seconds(value))

    def test_probe_timeout_and_cancel_reap_child(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired('ffprobe', 0.2), (b'', None)]
        with patch.object(m.shutil, 'which', return_value='/bin/ffprobe'), patch.object(m.subprocess, 'Popen', return_value=process), patch.object(m.time, 'monotonic', side_effect=[0, 0.1, 1]):
            self.assertIn('超时', m.probe_media('movie.mkv', timeout=0.5)['error'])
        process.kill.assert_called_once()
        stop = threading.Event()
        def communicate(**kwargs):
            if kwargs:
                stop.set()
                raise subprocess.TimeoutExpired('ffprobe', 0.2)
            return b'', None
        process.reset_mock()
        process.communicate.side_effect = communicate
        with patch.object(m.shutil, 'which', return_value='/bin/ffprobe'), patch.object(m.subprocess, 'Popen', return_value=process):
            self.assertIsNone(m.probe_media('movie.mkv', stop=stop)['duration'])
        process.kill.assert_called_once()

    def test_cue_end_parsing_and_malformed_timestamps(self):
        self.assertEqual(t.subtitle_end('1\n00:01:02,345 --> 00:01:03,456\n中文', '.srt'), 63.456)
        self.assertEqual(t.subtitle_end('WEBVTT\n\n01:02.345 --> 01:03.456 align:start\n中文', '.vtt'), 63.456)
        self.assertEqual(t.subtitle_end('[Events]\nFormat: End, Start, Text\nDialogue: 0:02:03.45,0:01:00.00,中文', '.ass'), 123.45)
        self.assertEqual(t.subtitle_end('[Events]\nDialogue: 0,0:01:00.00,0:02:00.00,Default,,0,0,0,,中文', '.ass'), 120)
        self.assertIsNone(t.subtitle_end('00:99:00,000 --> 00:99:02,000\n中文', '.srt'))
        self.assertIsNone(t.subtitle_end('00:02:00,000 --> 00:01:00,000\n中文', '.srt'))
        self.assertIsNone(t.subtitle_end('<SAMI><SYNC Start=12345>中文', '.smi'))

    def test_timing_only_penalizes_gross_outliers(self):
        self.assertEqual(t.timing_penalty(7000, 7200)[0], 0)
        self.assertEqual(t.timing_penalty(6000, 7200)[0], 0)
        self.assertEqual(t.timing_penalty(0, 7200)[0], 0)
        self.assertEqual(t.timing_penalty(7000, None)[0], 0)
        self.assertLess(t.timing_penalty(700, 7200)[0], 0)
        self.assertEqual(t.timing_penalty(8000, 7200)[0], -120)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe not installed')
class RealProbeTests(unittest.TestCase):
    def test_real_container_duration_and_subtitle_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / 'caption.srt'
            subtitle.write_text('1\n00:00:00,100 --> 00:00:01,500\nChinese English\n', encoding='utf-8')
            video = root / 'movie $(name).mkv'
            subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=black:s=16x16:r=1:d=2', '-i', str(subtitle), '-i', str(subtitle),
                '-map', '0:v', '-map', '1:0', '-map', '2:0', '-c:v', 'ffv1', '-c:s', 'srt',
                '-metadata:s:s:0', 'language=chi', '-metadata:s:s:0', 'title=Chinese English',
                '-metadata:s:s:1', 'language=chi', '-metadata:s:s:1', 'title=Signs',
                '-disposition:s:1', 'forced', str(video)], check=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            info = m.probe_media(video)
            self.assertFalse(info['error'])
            self.assertAlmostEqual(info['duration'], 2, delta=0.1)
            self.assertEqual(len(info['streams']), 2)
            self.assertTrue(m.embedded_subtitle(info['streams'], 'bilingual'))
            partial = [s for s in info['streams'] if s.get('tags', {}).get('title') == 'Signs']
            self.assertEqual(len(partial), 1)
            self.assertFalse(m.embedded_subtitle(partial))


class TimingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / 'Movie.2004.BluRay-GROUP.mkv'
        self.video.write_bytes(b'video')
        self.client = t.ThunderClient()

    @staticmethod
    def candidate(key, duration=0, name='Movie.2004.BluRay.srt'):
        return {'name': name, 'url': 'https://subtitle.v.geilijiasu.com/' + key + '.srt',
                'language': '中文', 'duration_ms': duration, 'rate': 0}

    @staticmethod
    def body(end):
        return ('1\n00:00:01,000 --> ' + end + '\n这是中文字幕\n').encode()

    def choose(self, candidates, bodies, duration=7200):
        with patch.object(self.client, 'search', return_value=candidates), patch.object(self.client, 'fetch', side_effect=lambda url, stop: bodies[url.rsplit('/', 1)[-1]]) as fetch:
            result = self.client.find(self.video, video_duration=duration)
        return result, fetch

    def test_api_outlier_is_demoted_before_download(self):
        result, fetch = self.choose([self.candidate('short', 700000), self.candidate('full', 7000000)],
                                   {'short.srt': self.body('00:12:00,000'), 'full.srt': self.body('01:56:00,000')})
        self.assertEqual(fetch.call_args_list[0].args[0].rsplit('/', 1)[-1], 'full.srt')
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('120.0', result['timing'])

    def test_downloaded_overrun_is_rejected_even_if_api_looks_good(self):
        result, fetch = self.choose([self.candidate('wrong', 7000000), self.candidate('full', 7000000)],
                                   {'wrong.srt': self.body('02:30:00,000'), 'full.srt': self.body('01:56:00,000')})
        self.assertEqual(fetch.call_count, 2)
        self.assertIn(b'01:56:00', result['data'])

    def test_short_body_falls_back_only_after_other_candidates(self):
        result, fetch = self.choose([self.candidate('short'), self.candidate('full')],
                                   {'short.srt': self.body('00:12:00,000'), 'full.srt': self.body('01:56:00,000')})
        self.assertEqual(fetch.call_count, 2)
        self.assertIn(b'01:56:00', result['data'])
        result, _ = self.choose([self.candidate('short')], {'short.srt': self.body('00:12:00,000')})
        self.assertIn('偏短', result['timing'])

    def test_bad_api_duration_cannot_hide_a_better_verified_release(self):
        result, fetch = self.choose([self.candidate('plain', 7000000),
            self.candidate('group', 700000, 'Movie.2004.BluRay-GROUP.srt')],
            {'plain.srt': self.body('01:55:00,000'), 'group.srt': self.body('01:56:00,000')})
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('GROUP', result['name'])

    def test_no_probe_or_disabled_timing_preserves_normal_selection(self):
        result, fetch = self.choose([self.candidate('first'), self.candidate('second')],
                                   {'first.srt': self.body('02:30:00,000')}, duration=None)
        self.assertIsNotNone(result)
        self.assertEqual(fetch.call_count, 1)

    def test_all_overruns_return_no_match(self):
        result, _ = self.choose([self.candidate('wrong')], {'wrong.srt': self.body('02:30:00,000')})
        self.assertIsNone(result)

    def test_api_overrun_does_not_reject_good_subtitle_body(self):
        result, fetch = self.choose([self.candidate('full', 9000000)], {'full.srt': self.body('01:56:00,000')})
        self.assertIsNotNone(result)
        self.assertEqual(fetch.call_count, 1)
