"""Exercise actual V3 event dispatch, scheduling and market selection."""
import threading
from unittest.mock import Mock

from apscheduler.schedulers.background import BackgroundScheduler

from app.plugins import thundersubtitles as adapter
from app.sdk.events import EventManager
from app.sdk.config import settings
from app.schemas.types import EventType

from test_plugin import DATA, transfer_event


def test_real_event_bus_and_host_once_job(plugin, tmp_path, monkeypatch):
    """Publish a typed transfer and let the host deliver and run the job."""
    from app.application.plugin.runtime import get_plugin_manager
    from app.application.scheduling import register_scheduler_class
    from app.runtime.event.binding import EventHandlerBinding
    from app.scheduler.facade import Scheduler

    manager = get_plugin_manager()
    manager.running_plugins['ThunderSubtitles'] = plugin
    scheduler = Scheduler()
    scheduler._scheduler = BackgroundScheduler(timezone=settings.TZ)
    scheduler._lifecycle_state = 'running'
    scheduler._scheduler.start()
    previous = register_scheduler_class(lambda: scheduler)
    bus = EventManager()
    bus.register_handler_instance_resolver('thunder-v3-integration', lambda cls: (
        EventHandlerBinding(instance=plugin, owner_name='ThunderSubtitles', run_sync_in_threadpool=True)
        if cls is adapter.ThunderSubtitles else None))
    bus.start()
    completed = threading.Event()
    original_record = plugin._record

    def record(*args):
        original_record(*args)
        completed.set()

    monkeypatch.setattr(plugin, '_record', record)
    monkeypatch.setattr(adapter.ThunderClient, 'find', Mock(return_value=DATA))
    video = tmp_path / 'Movie.1994.mkv'
    video.write_bytes(b'video')
    try:
        plugin.init_plugin({'enabled': True})
        event = transfer_event(video)
        bus.send_event(EventType.TransferComplete, event.event_data)
        assert completed.wait(10), 'The actual host event/scheduler flow did not complete'
        assert video.with_suffix('.zh.srt').read_bytes() == DATA['data']
        assert plugin.get_data('history')[0]['status'] == '成功'
        plugin.stop_service()
        assert not scheduler._scheduler.get_jobs()
    finally:
        plugin.stop_service()
        bus.unregister_handler_instance_resolver('thunder-v3-integration')
        bus.stop()
        scheduler.stop()
        register_scheduler_class(previous)
        manager.running_plugins.pop('ThunderSubtitles', None)


def test_host_market_selects_correct_generation(plugin, monkeypatch):
    """Use the V3 market client against the repository's actual indexes."""
    from pathlib import Path
    from app.adapters.external.plugin.client import PluginMarketClient

    root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(settings, 'PLUGIN_LOCAL_REPO_PATHS', str(root))
    monkeypatch.setattr(type(settings), 'VERSION_FLAG', property(lambda self: 'v3'))
    client = PluginMarketClient()
    v3 = client.get_local_candidate('ThunderSubtitles')
    assert v3['package_version'] == 'v3'
    assert v3['version'] == '2.1.0'
    assert v3.get('compatible', True)
    assert v3['path'] == root / 'plugins.v3/thundersubtitles'
    monkeypatch.setattr(type(settings), 'VERSION_FLAG', property(lambda self: 'v2'))
    v2 = client.get_local_candidate('ThunderSubtitles', strict_system_version=False)
    assert v2['package_version'] == 'v2'
    assert v2['version'] == '1.3.0'
    assert v2['path'] == root / 'plugins.v2/thundersubtitles'
