"""Load the real V3 SDK against the host's isolated test database."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOST = Path(os.environ.get('MOVIEPILOT_PATH', ROOT.parent / 'MoviePilot')).resolve()
if not (HOST / 'app/testing/bootstrap.py').is_file():
    raise RuntimeError('Set MOVIEPILOT_PATH to a MoviePilot V3 source checkout')

# Never reuse a developer's live CONFIG_DIR or database.
_config_directory = tempfile.TemporaryDirectory(prefix='mp-thunder-v3-', ignore_cleanup_errors=True)
os.environ['CONFIG_DIR'] = _config_directory.name
sys.path.insert(0, str(HOST))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(ROOT)

# The host's composition fixture supplies the actual Chain/plugin data ports;
# the plugin base class, event schemas and SDK are never replaced with stubs.
spec = importlib.util.spec_from_file_location('thunder_host_fixtures', HOST / 'tests/conftest.py')
host_fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_fixtures)
configure_plugin_system_services = host_fixtures.configure_plugin_system_services

from app.testing.network import block_real_network


def pytest_sessionfinish(session, exitstatus):
    """Release the isolated SQLite pool before deleting its temporary files."""
    from app.db.engine import peek_sync_engine

    engine = peek_sync_engine()
    if engine is not None:
        engine.dispose()
    _config_directory.cleanup()


@pytest.fixture
def plugin(configure_plugin_system_services, monkeypatch):
    """Create a real plugin instance with empty, isolated history."""
    from app.plugins.thundersubtitles import ThunderSubtitles
    from app.plugins import thundersubtitles as adapter

    monkeypatch.setattr(adapter, 'probe_media', lambda *args: {'streams': [], 'duration': None, 'error': ''})

    instance = ThunderSubtitles()
    instance.del_data('history')
    yield instance
    instance.stop_service()


@pytest.fixture
def scheduled(monkeypatch):
    """Capture host SDK submissions for deterministic lifecycle tests."""
    from app.plugins import thundersubtitles as adapter

    calls = []
    removals = []

    def add(plugin_id, job_id, func, name, **kwargs):
        calls.append((plugin_id, job_id, func, kwargs))
        return True

    monkeypatch.setattr(adapter.scheduler_sdk, 'add_plugin_once_job', add)
    monkeypatch.setattr(adapter.scheduler_sdk, 'remove_plugin_once_job', lambda *args: removals.append(args))
    return calls, removals
