"""V3 host contracts, cancellation and retained V2 configuration."""
import threading
from unittest.mock import Mock

import pytest

from app.plugins import thundersubtitles as adapter
from app.schemas.event import TransferResultContractData
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType
from app.sdk.events import Event

DATA = {'data': '1\n00:00:01,000 --> 00:00:03,000\n中文字幕 English\n'.encode(),
        'extension': '.srt', 'name': 'Movie.双语.srt', 'origin': 'CID+名称', 'match': '片源一致'}


def transfer_event(video, source='Movie.1994.BluRay-GROUP.mkv', storage='local', **changes):
    """Build a typed event using the real V3 transfer contract."""
    item = FileItem(path=str(video), type='file', storage=storage)
    info = TransferInfo(success=True, target_item=item, target_diritem=item,
                        file_list_new=[str(video)])
    for key, value in changes.items():
        setattr(info, key, value)
    return Event(EventType.TransferComplete, TransferResultContractData(
        transferinfo=info, fileitem=FileItem(path='/downloads/' + source)))


def drain(plugin):
    """Run the captured host callback synchronously for deterministic assertions."""
    plugin._drain(plugin._state)


def test_real_sdk_load_and_v2_configuration(plugin, scheduled):
    old = {'enabled': True, 'preference': 'traditional', 'paths': '/media',
           'cron': '0 */6 * * *', 'skip_existing': False, 'utf8': False,
           'source_prefix': '/old', 'local_prefix': '/new', 'custom': 'retained'}
    plugin.init_plugin(old)
    assert plugin.plugin_version == '2.0.0'
    assert plugin._config == {**adapter.DEFAULTS, **old}
    assert plugin.get_state()
    assert plugin.get_api() == plugin.get_command() == []
    assert plugin.get_form()[1]['preference'] == 'bilingual'
    assert plugin.get_service()[0]['id'] == 'ThunderSubtitles.scan'
    assert plugin.get_page()[0]['component'] == 'VAlert'


@pytest.mark.parametrize('as_dict', [False, True])
def test_typed_and_dictionary_transfer_saves_sidecar(plugin, scheduled, tmp_path, monkeypatch, as_dict):
    video = tmp_path / 'Movie (1994).mkv'
    video.write_bytes(b'video')
    find = Mock(return_value=DATA)
    monkeypatch.setattr(adapter.ThunderClient, 'find', find)
    plugin.init_plugin({'enabled': True})
    event = transfer_event(video)
    if as_dict:
        event = Event(EventType.TransferComplete, event.event_data.model_dump(mode='python'))
    assert event.snapshot().valid
    plugin.transfer_complete(event)
    assert len(scheduled[0]) == 1
    assert scheduled[0][0][0] == 'ThunderSubtitles'
    drain(plugin)
    assert video.with_suffix('.zh.srt').read_bytes() == DATA['data']
    assert find.call_args.kwargs['source_name'] == 'Movie.1994.BluRay-GROUP.mkv'
    assert find.call_args.args[1] == 'bilingual'
    assert plugin.get_data('history')[0]['status'] == '成功'
    assert plugin.get_page()[0]['component'] == 'VList'


def test_repeated_initialization_cancels_old_generation(plugin, scheduled, tmp_path, monkeypatch):
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    find = Mock(return_value=DATA)
    monkeypatch.setattr(adapter.ThunderClient, 'find', find)
    plugin.init_plugin({'enabled': True})
    plugin.transfer_complete(transfer_event(video))
    old = plugin._state
    plugin.init_plugin({'enabled': True})
    assert old.stop.is_set() and old.jobs.empty()
    assert old.job_id != plugin._state.job_id
    plugin._drain(old)
    find.assert_not_called()
    plugin.transfer_complete(transfer_event(video))
    drain(plugin)
    assert find.call_count == 1


def test_stop_during_request_cannot_save_or_record(plugin, scheduled, tmp_path, monkeypatch):
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    started, release = threading.Event(), threading.Event()

    def slow_find(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return DATA

    monkeypatch.setattr(adapter.ThunderClient, 'find', slow_find)
    plugin.init_plugin({'enabled': True})
    plugin.transfer_complete(transfer_event(video))
    state = plugin._state
    thread = threading.Thread(target=plugin._drain, args=(state,))
    thread.start()
    try:
        assert started.wait(3)
        plugin.stop_service()
        assert not plugin.get_state()
        assert scheduled[1][-1] == ('ThunderSubtitles', state.job_id)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not video.with_suffix('.zh.srt').exists()
    assert not plugin.get_data('history')
    plugin.stop_service()


def test_duplicate_events_share_one_host_job(plugin, scheduled, tmp_path):
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    plugin.init_plugin({'enabled': True})
    for _ in range(10):
        plugin.transfer_complete(transfer_event(video))
    assert plugin._state.jobs.qsize() == 1
    assert len(scheduled[0]) == 1


def test_new_batch_does_not_reuse_finishing_host_job(plugin, scheduled, tmp_path, monkeypatch):
    """A finished drain can still be settling in the host when new work arrives."""
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    monkeypatch.setattr(adapter.ThunderClient, 'find', Mock(return_value=None))
    plugin.init_plugin({'enabled': True})
    plugin.transfer_complete(transfer_event(video))
    first_id = plugin._state.job_id
    drain(plugin)
    plugin.transfer_complete(transfer_event(video))
    assert plugin._state.job_id != first_id
    assert len(scheduled[0]) == 2


def test_once_scan_works_when_automatic_download_is_disabled(plugin, scheduled, tmp_path, monkeypatch):
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    monkeypatch.setattr(adapter.ThunderClient, 'find', Mock(return_value=DATA))
    plugin.init_plugin({'enabled': False, 'onlyonce': True, 'paths': str(tmp_path)})
    assert plugin.get_config()['onlyonce'] is False
    assert not plugin.get_state()
    drain(plugin)
    assert video.with_suffix('.zh.srt').exists()


def test_scheduler_not_ready_retains_manual_request(plugin, scheduled, monkeypatch):
    monkeypatch.setattr(adapter.scheduler_sdk, 'add_plugin_once_job', lambda *a, **kw: False)
    plugin.init_plugin({'enabled': False, 'onlyonce': True})
    assert plugin._config['onlyonce'] is True
    assert not plugin._state.scheduled
    assert plugin.get_data('history')[0]['status'] == '未执行'


def test_failed_cloud_strm_and_invalid_events_are_ignored(plugin, scheduled, tmp_path):
    video = tmp_path / 'Movie.mkv'
    plugin.init_plugin({'enabled': True})
    for event in [None, transfer_event(video, success=False), transfer_event(video, storage='u115'),
                  transfer_event(tmp_path / 'Movie.strm'), Event(EventType.TransferComplete, {'transferinfo': []})]:
        plugin.transfer_complete(event)
    assert plugin._state.jobs.empty()


def test_existing_subtitles_are_kept(plugin, scheduled, tmp_path, monkeypatch):
    video = tmp_path / 'Movie.mkv'
    video.write_bytes(b'video')
    sidecar = video.with_suffix('.zh.srt')
    sidecar.write_bytes(b'original')
    find = Mock(return_value=DATA)
    monkeypatch.setattr(adapter.ThunderClient, 'find', find)
    plugin.init_plugin({'enabled': True})
    plugin.transfer_complete(transfer_event(video))
    drain(plugin)
    find.assert_not_called()
    plugin.init_plugin({'enabled': True, 'skip_existing': False})
    plugin.transfer_complete(transfer_event(video))
    drain(plugin)
    assert sidecar.read_bytes() == b'original'


def test_history_and_configuration_are_instance_scoped(plugin, scheduled):
    plugin.save_data('history', [{'status': '成功', 'path': '/old/movie.mkv', 'time': '2026-10-31', 'message': '旧记录'}])
    plugin.update_config({'enabled': True, 'preference': 'bilingual'})
    plugin.init_plugin(plugin.get_config())
    assert plugin.get_data('history')[0]['message'] == '旧记录'
    clone_class = type('ThunderSubtitlesOffice', (adapter.ThunderSubtitles,), {})
    clone = clone_class()
    try:
        clone.init_plugin({'enabled': True})
        assert clone.get_data('history') is None
        clone._enqueue('scan', 'configured-directories')
        assert scheduled[0][-1][0] == 'ThunderSubtitlesOffice'
        assert clone._state is not plugin._state
    finally:
        clone.stop_service()


def test_queue_is_bounded_and_paths_use_directory_boundaries(plugin, scheduled):
    plugin.init_plugin({'enabled': True})
    for i in range(200):
        assert plugin._enqueue('file', '/media/%s.mkv' % i)
    assert not plugin._enqueue('file', '/media/overflow.mkv')
    assert plugin._state.jobs.qsize() == 200
    assert adapter.mapped_path('/media2/a.mkv', '/media', '/movies').as_posix() == '/media2/a.mkv'
    assert adapter.mapped_path('/media/a.mkv', '/media', '/movies').as_posix() == '/movies/a.mkv'
