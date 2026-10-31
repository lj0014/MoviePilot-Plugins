import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('thunder_core', ROOT / 'plugins.v2/thundersubtitles/thunder.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
CHINESE = '1\n00:00:01,000 --> 00:00:03,000\n这是字幕测试。\n' * 10


def response(data, status=200, headers=None):
    r = Mock(status_code=status, headers=headers or {})
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.iter_content.return_value = [data]
    return r


class HashTests(unittest.TestCase):
    def test_reference_hash_at_threshold_and_large_file(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'movie.mkv'
            for length in (1, 0xEFFF, 0xF000, 200000):
                data = bytes(i % 251 for i in range(length))
                p.write_bytes(data)
                sample = data if length < 0xF000 else data[:0x5000] + data[length // 3:length // 3 + 0x5000] + data[-0x5000:]
                self.assertEqual(t.cid_hash(p), hashlib.sha1(sample).hexdigest().upper())

    def test_empty_file_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'movie.mkv'
            p.touch()
            with self.assertRaises(t.SubtitleError): t.cid_hash(p)
            p.write_bytes(b'movie')
            stop = threading.Event(); stop.set()
            with self.assertRaises(t.Cancelled): t.cid_hash(p, stop)


class SearchTests(unittest.TestCase):
    @patch.object(t.requests, 'get')
    def test_corrupt_metadata_deduplication_ranking_and_english(self, get):
        records = [None, {}, {'sname': 'english.srt', 'surl': 'http://subtitle.v.geilijiasu.com/eng.srt', 'language': '英语', 'rate': '5'},
                   {'sname': '繁体.srt', 'surl': 'http://subtitle.v.geilijiasu.com/cht.srt', 'language': '繁体', 'rate': '5'},
                   {'sname': '简体.srt', 'surl': 'http://subtitle.v.geilijiasu.com/chs.srt', 'language': '简体', 'rate': '3'},
                   {'surl': 'file:///etc/passwd'}, {'surl': 'https://subtitle.v.geilijiasu.com/chs.srt'}]
        get.return_value = response(json.dumps({'sublist': records}).encode())
        found = t.ThunderClient().search('A' * 40)
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0]['name'], '简体.srt')
        self.assertTrue(all(c['url'].startswith('https://') for c in found))

    @patch.object(t.requests, 'get')
    def test_legacy_gbk_name(self, get):
        get.return_value = response(b'{"sublist":[{"sname":"' + '阿甘正传.srt'.encode('gbk') + b'","surl":"http://subtitle.v.geilijiasu.com/a.srt"}]}')
        self.assertEqual(t.ThunderClient().search('A' * 40)[0]['name'], '阿甘正传.srt')

    @patch.object(t.requests, 'get')
    def test_bounded_network_failures_and_invalid_json(self, get):
        for bad in (b'not json', b'{}', b'[]', b'{"sublist":null}'):
            get.reset_mock(); get.return_value = response(bad)
            with self.assertRaises(t.SubtitleError): t.ThunderClient().search('A' * 40)
            self.assertEqual(get.call_count, 3)
        get.side_effect = t.requests.Timeout()
        with self.assertRaises(t.SubtitleError): t.ThunderClient().search('A' * 40)

    @patch.object(t.requests, 'get')
    def test_empty_results_and_cancel(self, get):
        get.return_value = response(b'{"sublist":[]}')
        self.assertEqual(t.ThunderClient().search('A' * 40), [])
        get.reset_mock(); stop = threading.Event(); stop.set()
        with self.assertRaises(t.Cancelled): t.ThunderClient().search('A' * 40, stop=stop)
        get.assert_not_called()

    def test_names_urls_and_episode_compatibility(self):
        self.assertEqual(t.clean_name('[����].Forrest.Gump.1994.srt'), '.Forrest.Gump.1994.srt')
        self.assertEqual(t.clean_name('%B0%A2%B8%CA%D5%FD%B4%AB.srt'), '阿甘正传.srt')
        for url in ('http://127.0.0.1/a', 'file:///tmp/a', 'https://user:pass@subtitle.v.geilijiasu.com/a', 'https://subtitle.v.geilijiasu.com:8000/a'):
            self.assertIsNone(t.download_url(url))
        self.assertFalse(t.compatible('show.S01E02.srt', 'show.S01E01.mkv'))
        self.assertTrue(t.compatible('show.S01E01.srt', 'show.S01E01.mkv'))
        self.assertFalse(t.compatible('movie.CD2.srt', 'movie.CD1.mkv'))


class DownloadTests(unittest.TestCase):
    @patch.object(t.requests, 'get')
    def test_redirect_cannot_reach_private_host(self, get):
        get.return_value = response(b'', 302, {'Location': 'http://127.0.0.1/admin'})
        with self.assertRaises(t.SubtitleError): t.ThunderClient().fetch('https://subtitle.v.geilijiasu.com/a.srt')
        self.assertEqual(get.call_count, 1)

    def test_size_empty_and_non_subtitle_validation(self):
        with self.assertRaises(t.SubtitleError): t.read_response(response(b'1234'), 3)
        for content in (b'', b'<html>error', b'just text', b'1\n00:00:00,000 --> 00:00:03,000\nEnglish only'):
            with self.assertRaises(t.SubtitleError): t.validate_subtitle(content, '.srt')

    def test_chinese_encodings_and_ass_dialogue_only(self):
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32', 'gb18030', 'big5'):
            sample = CHINESE.replace('这', '這').replace('测', '測').replace('试', '試') if encoding == 'big5' else CHINESE
            self.assertEqual(t.validate_subtitle(sample.encode(encoding), '.srt'), sample)
        with self.assertRaises(t.SubtitleError):
            t.validate_subtitle('[Script Info]\nTitle: 中文字幕\n[Events]\nDialogue: 0,0,0,Default,,0,0,0,,English'.encode(), '.ass')

    def test_zip_filters_wrong_episodes_and_never_extracts_paths(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../../show.S01E02.srt', CHINESE)
            z.writestr('../../show.S01E01.srt', CHINESE)
            z.writestr('run.exe', 'ignored')
        choices = list(t.ThunderClient().choices(archive.getvalue(), {}, 'show.S01E01.mkv'))
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0][2], 'show.S01E01.srt')

    def test_zip_expansion_limit(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z: z.writestr('a.srt', b'a' * 1025)
        with patch.object(t, 'MAX_BYTES', 1024), self.assertRaises(t.SubtitleError):
            list(t.ThunderClient().choices(archive.getvalue(), {}, 'movie.mkv'))

    def test_full_find_falls_back_from_invalid_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'movie.mkv'; movie.write_bytes(b'video')
            client = t.ThunderClient()
            records = [{'name': 'a.srt', 'language': '简体', 'url': 'https://subtitle.v.geilijiasu.com/a.srt'},
                       {'name': 'b.srt', 'language': '简体', 'url': 'https://subtitle.v.geilijiasu.com/b.srt'}]
            with patch.object(client, 'search', return_value=records), patch.object(client, 'fetch', side_effect=[b'<html>error', CHINESE.encode('gb18030')]):
                found = client.find(movie)
            self.assertEqual(found['data'].decode('utf-8'), CHINESE)

    def test_existing_subtitles_save_no_overwrite_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'movie.mkv'; movie.touch()
            (movie.parent / 'movie.en.srt').write_text('English')
            (movie.parent / 'movie2.zh.srt').write_text('other movie')
            self.assertFalse(t.has_subtitle(movie))
            subtitle = {'data': CHINESE.encode(), 'extension': '.srt'}
            target = t.save_subtitle(movie, subtitle)
            self.assertTrue(t.has_subtitle(movie))
            with self.assertRaises(FileExistsError): t.save_subtitle(movie, {'data': b'replacement', 'extension': '.srt'})
            self.assertEqual(target.read_bytes(), subtitle['data'])
            self.assertFalse(list(movie.parent.glob('*.part')))
            stop = threading.Event(); stop.set()
            with self.assertRaises(t.Cancelled): t.save_subtitle(movie, {'data': b'new', 'extension': '.ass'}, stop)
            self.assertFalse(movie.with_suffix('.zh.ass').exists())

    def test_nas_without_hardlinks_uses_exclusive_creation(self):
        import errno
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'movie.mkv'; movie.touch()
            with patch.object(t.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')):
                target = t.save_subtitle(movie, {'data': b'first', 'extension': '.srt'})
                with self.assertRaises(FileExistsError): t.save_subtitle(movie, {'data': b'second', 'extension': '.srt'})
            self.assertEqual(target.read_bytes(), b'first')


if __name__ == '__main__': unittest.main()
