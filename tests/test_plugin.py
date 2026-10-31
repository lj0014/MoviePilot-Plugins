"""Load the plugin in the same app.plugins namespace used by MoviePilot."""
from abc import ABC, abstractmethod
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
DATA = {'data': '1\n00:00:01,000 --> 00:00:03,000\n中文字幕\n'.encode(), 'extension': '.srt', 'name': '中文.srt'}


class PluginBase(ABC):
    def __init__(self): self.data = {}; self.saved_config = None
    @abstractmethod
    def init_plugin(self, config=None): pass
    @abstractmethod
    def get_state(self): pass
    @abstractmethod
    def get_form(self): pass
    @abstractmethod
    def get_page(self): pass
    @abstractmethod
    def get_api(self): pass
    @abstractmethod
    def stop_service(self): pass
    def get_data(self, key): return self.data.get(key)
    def save_data(self, key, value): self.data[key] = value
    def update_config(self, config): self.saved_config = config


class EventType(Enum):
    TransferComplete = 'transfer_complete'


class PluginTests(unittest.TestCase):
    def setUp(self):
        modules = {name: types.ModuleType(name) for name in ('app', 'app.core', 'app.core.config', 'app.core.event', 'app.log', 'app.plugins', 'app.schemas', 'app.schemas.types')}
        modules['app.plugins'].__path__ = []
        modules['app.plugins']._PluginBase = PluginBase
        modules['app.core.config'].settings = types.SimpleNamespace(TZ='Asia/Shanghai')
        modules['app.core.event'].eventmanager = types.SimpleNamespace(register=lambda event: lambda callback: callback)
        modules['app.core.event'].Event = types.SimpleNamespace
        modules['app.schemas.types'].EventType = EventType
        modules['app.log'].logger = Mock()
        p = patch.dict(sys.modules, modules)
        p.start(); self.addCleanup(p.stop)
        spec = importlib.util.spec_from_file_location('app.plugins.thundersubtitles', ROOT / 'plugins.v2/thundersubtitles/__init__.py', submodule_search_locations=[str(ROOT / 'plugins.v2/thundersubtitles')])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module
        self.plugin = module.ThunderSubtitles()
        self.addCleanup(self.plugin.stop_service)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'movie.mkv'; self.video.write_bytes(b'video')

    def event(self, files=None, storage='local', success=True):
        return types.SimpleNamespace(event_data={'transferinfo': types.SimpleNamespace(success=success,
            target_diritem=types.SimpleNamespace(storage=storage), file_list_new=[str(self.video)] if files is None else files)})

    def test_load_contract_form_and_empty_history(self):
        self.plugin.init_plugin()
        form, model = self.plugin.get_form()
        self.assertFalse(model['enabled'])
        self.assertTrue(model['skip_existing'])
        self.assertEqual(form[0]['component'], 'VForm')
        self.assertEqual(self.plugin.get_api(), [])
        self.assertEqual(self.plugin.get_command(), [])
        self.assertEqual(self.plugin.get_page()[0]['component'], 'VAlert')

    def test_transfer_downloads_sidecar_and_history(self):
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': True})
            self.plugin.transfer_complete(self.event())
            self.plugin._jobs.join()
            self.assertEqual(client.return_value.find.call_count, 1)
        self.assertEqual(self.video.with_suffix('.zh.srt').read_bytes(), DATA['data'])
        self.assertEqual(self.plugin.get_data('history')[0]['status'], '成功')
        self.assertEqual(self.plugin.get_page()[0]['component'], 'VList')

    def test_disabled_failed_remote_and_strm_events_are_ignored(self):
        with patch.object(self.module, 'ThunderClient') as client:
            self.plugin.init_plugin({'enabled': False})
            self.plugin.transfer_complete(self.event())
            self.plugin.init_plugin({'enabled': True})
            self.plugin.transfer_complete(self.event(success=False))
            self.plugin.transfer_complete(self.event(storage='u115'))
            self.plugin.transfer_complete(self.event(files=[str(self.root / 'movie.strm')]))
            self.plugin.transfer_complete(None)
            self.plugin.transfer_complete(types.SimpleNamespace(event_data={}))
            self.plugin._jobs.join()
            client.assert_not_called()

    def test_dictionary_events_and_target_item_fallback(self):
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': True})
            event = types.SimpleNamespace(event_data={'transferinfo': {'success': True, 'target_item': {'type': 'file', 'storage': 'local', 'path': str(self.video)}}})
            self.plugin.transfer_complete(event); self.plugin._jobs.join()
        self.assertTrue(self.video.with_suffix('.zh.srt').exists())

    def test_prefix_mapping_requires_directory_boundary(self):
        mapped = self.module.mapped_path('/downloads/movie.mkv', '/downloads', '/media')
        self.assertEqual(mapped.as_posix(), '/media/movie.mkv')
        self.assertEqual(self.module.mapped_path('/downloads2/movie.mkv', '/downloads', '/media').as_posix(), '/downloads2/movie.mkv')
        self.assertEqual(self.module.mapped_path('Z:\\media\\movie.mkv', 'Z:\\media', '/media').as_posix(), '/media/movie.mkv')

    def test_transfer_uses_mapped_path(self):
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': True, 'source_prefix': '/event', 'local_prefix': str(self.root)})
            self.plugin.transfer_complete(self.event(files=['/event/movie.mkv']))
            self.plugin._jobs.join()
            self.assertEqual(client.return_value.find.call_args.args[0], self.video)

    def test_existing_subtitle_skips_network_and_never_overwrites(self):
        subtitle = self.video.with_suffix('.zh.srt'); subtitle.write_bytes(b'original')
        with patch.object(self.module, 'ThunderClient') as client:
            self.plugin.init_plugin({'enabled': True})
            self.plugin.transfer_complete(self.event()); self.plugin._jobs.join()
            client.assert_not_called()
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': True, 'skip_existing': False})
            self.plugin.transfer_complete(self.event()); self.plugin._jobs.join()
        self.assertEqual(subtitle.read_bytes(), b'original')
        self.assertEqual(self.plugin.get_data('history')[0]['status'], '跳过')

    def test_duplicate_events_are_coalesced_while_running(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def find(*args, **kwargs):
            started.set(); release.wait(3); return DATA
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.side_effect = find
            self.plugin.init_plugin({'enabled': True})
            self.plugin.transfer_complete(self.event())
            self.assertTrue(started.wait(2))
            for _ in range(10): self.plugin.transfer_complete(self.event())
            release.set(); self.plugin._jobs.join()
            self.assertEqual(client.return_value.find.call_count, 1)

    def test_disable_during_download_prevents_writes_and_queued_jobs(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def find(*args, **kwargs):
            started.set(); release.wait(5); return DATA
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.side_effect = find
            self.plugin.init_plugin({'enabled': True})
            self.plugin.transfer_complete(self.event()); self.assertTrue(started.wait(2))
            self.plugin._enqueue('file', str(self.root / 'other.mkv'))
            worker = self.plugin._worker
            self.plugin.init_plugin({'enabled': False})
            release.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(client.return_value.find.call_count, 1)
        self.assertFalse(self.video.with_suffix('.zh.srt').exists())

    def test_once_scan_does_not_require_enabled_and_resets_flag(self):
        (self.root / 'movie.strm').write_text('https://example.com/video')
        hidden = self.root / 'BDMV'; hidden.mkdir(); (hidden / 'disc.m2ts').write_bytes(b'video')
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': False, 'onlyonce': True, 'paths': str(self.root)})
            self.plugin._jobs.join()
            self.assertEqual(client.return_value.find.call_count, 1)
        self.assertFalse(self.plugin.get_state())
        self.assertFalse(self.plugin.saved_config['onlyonce'])
        self.assertTrue(self.video.with_suffix('.zh.srt').exists())

    def test_service_cron_and_no_paths(self):
        self.plugin.init_plugin({'enabled': True, 'cron': '0 */6 * * *'})
        self.assertEqual(self.plugin.get_service(), [])
        self.plugin.init_plugin({'enabled': True, 'cron': 'bad', 'paths': str(self.root)})
        self.assertEqual(self.plugin.get_service(), [])
        self.plugin.init_plugin({'enabled': True, 'cron': '0 */6 * * *', 'paths': str(self.root)})
        service = self.plugin.get_service()[0]
        self.assertEqual(service['id'], 'ThunderSubtitles.scan')
        self.assertEqual(service['func'], self.plugin.scan)

    def test_missing_file_can_retry_and_no_match_is_recorded(self):
        self.plugin.init_plugin({'enabled': True})
        path = self.root / 'not-yet.mkv'
        self.plugin.transfer_complete(self.event(files=[str(path)])); self.plugin._jobs.join()
        self.assertEqual(self.plugin.get_data('history')[0]['status'], '失败')
        path.write_bytes(b'video')
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = None
            self.plugin.transfer_complete(self.event(files=[str(path)])); self.plugin._jobs.join()
        self.assertEqual(self.plugin.get_data('history')[0]['status'], '未匹配')

    def test_original_release_name_survives_transfer_rename(self):
        original = 'Movie.1994.BluRay.x265-EDGE2020.mkv'
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = {**DATA, 'match': '片源类型一致'}
            self.plugin.init_plugin({'enabled': True})
            event = self.event()
            event.event_data['fileitem'] = {'path': '/downloads/' + original}
            event.event_data['transferinfo'].target_item = {'path': str(self.video)}
            self.plugin.transfer_complete(event); self.plugin._jobs.join()
            self.assertEqual(client.return_value.find.call_args.kwargs['source_name'], original)
        message = self.plugin.get_data('history')[0]['message']
        self.assertIn('来源：中文.srt', message)
        self.assertIn('未校验时间轴', message)
        self.assertIn(original, message)

    def test_batch_hint_only_applies_to_its_own_destination(self):
        other = self.root / 'other.mkv'; other.write_bytes(b'other')
        with patch.object(self.module, 'ThunderClient') as client:
            client.return_value.find.return_value = DATA
            self.plugin.init_plugin({'enabled': True})
            event = self.event(files=[str(self.video), str(other)])
            event.event_data['fileitem'] = {'path': '/downloads/Movie.BluRay.mkv'}
            event.event_data['transferinfo'].target_item = {'path': str(self.video)}
            self.plugin.transfer_complete(event); self.plugin._jobs.join()
            hints = {call.args[0].name: call.kwargs['source_name'] for call in client.return_value.find.call_args_list}
            self.assertEqual(hints, {'movie.mkv': 'Movie.BluRay.mkv', 'other.mkv': ''})


if __name__ == '__main__': unittest.main()
