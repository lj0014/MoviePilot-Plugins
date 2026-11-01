"""MoviePilot V3 transfer event adapter for Thunder subtitles."""
from collections.abc import Mapping
from dataclasses import dataclass, field as state_field
from datetime import datetime
import os
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import uuid
from typing import Any, Dict, List, Tuple

from app.sdk.config import settings
from app.sdk.events import eventmanager, Event
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk import scheduler as scheduler_sdk
from app.schemas.types import EventType

from .thunder import Cancelled, SubtitleError, ThunderClient, VIDEO_EXTENSIONS, has_subtitle, save_subtitle


def field(value, name, default=None):
    """Read a field from a mapping or a typed event snapshot."""
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def mapped_path(value, source='', destination=''):
    """Replace a directory prefix, never a substring of a sibling directory."""
    value = str(value).replace('\\', '/')
    if source and destination:
        source = source.replace('\\', '/').rstrip('/')
        destination = destination.replace('\\', '/').rstrip('/')
        if value == source or value.startswith(source + '/'):
            value = destination + value[len(source):]
    return Path(value)


DEFAULTS = {'enabled': False, 'onlyonce': False, 'paths': '', 'cron': '',
            'source_prefix': '', 'local_prefix': '', 'preference': 'bilingual',
            'skip_existing': True, 'utf8': True}


@dataclass
class RunState:
    """Keep queued work and cancellation tied to one configuration generation."""

    config: dict
    jobs: Queue = state_field(default_factory=lambda: Queue(maxsize=200))
    pending: set = state_field(default_factory=set)
    stop: threading.Event = state_field(default_factory=threading.Event)
    scheduled: bool = False
    job_id: str = state_field(default_factory=lambda: 'download_queue_' + uuid.uuid4().hex)


class ThunderSubtitles(_PluginBase):
    """Download Chinese subtitle sidecars using the V3 SDK and host scheduler."""
    plugin_name = '迅雷字幕'
    plugin_desc = '整理入库后合并 CID 和名称搜索，下载中文字幕。'
    plugin_icon = 'https://raw.githubusercontent.com/lj0014/MoviePilot-Plugins/main/icons/thundersubtitles.svg'
    plugin_version = '2.0.0'
    plugin_author = 'lijin'
    author_url = 'https://github.com/lj0014'
    plugin_config_prefix = 'thundersubtitles_'
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        """Create isolated state and commit barriers for this plugin instance."""
        super().__init__()
        self._config = dict(DEFAULTS)
        self._state = None
        self._active = False
        self._lifecycle_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._history_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """Cancel the previous generation and preserve the existing configuration keys."""
        with self._lifecycle_lock:
            self.stop_service()
            self._config = {**DEFAULTS, **(config or {})}
            once = bool(self._config.get('onlyonce'))
            self._active = bool(self._config.get('enabled') or once)
            if not self._active:
                return
            self._state = RunState(config=dict(self._config))
            if once:
                if self._enqueue('scan', 'configured-directories'):
                    self._config['onlyonce'] = False
                    self.update_config(dict(self._config))
                else:
                    self._record('', '未执行', '宿主调度器尚未就绪，请稍后重新保存配置', self._state.stop)

    def get_state(self) -> bool:
        """Report enabled status, including an explicit service stop."""
        return self._active and bool(self._config.get('enabled'))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """This plugin does not expose remote commands."""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """This plugin does not register HTTP routes."""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """Declare the optional recurring scan using the host service scheduler."""
        cron = str(self._config.get('cron') or '').strip()
        if not self.get_state() or not cron or not self._roots(self._config):
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(cron, timezone=settings.TZ)
        except (ValueError, TypeError):
            logger.error('迅雷字幕：扫描周期格式错误，请使用五段 cron 表达式')
            return []
        return [{'id': self.__class__.__name__ + '.scan', 'name': '迅雷字幕补全扫描',
                 'trigger': trigger, 'func': self.scan, 'kwargs': {}}]

    def scan(self):
        """Queue a scan when automatic processing is enabled."""
        if self.get_state():
            self._enqueue('scan', 'configured-directories')

    def stop_service(self):
        """Cancel pending host work and prevent stopped generations from saving data."""
        with self._lifecycle_lock:
            self._active = False
            state = self._state
            if state is None:
                return
            state.stop.set()
            # Finish a sidecar/history commit before returning. In-flight HTTP
            # work is owned by the host and cannot commit after cancellation.
            with self._write_lock, self._history_lock:
                pass
            try:
                scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, state.job_id)
            except RuntimeError:
                logger.debug('迅雷字幕：宿主调度器尚未初始化')
            while True:
                try:
                    state.jobs.get_nowait()
                    state.jobs.task_done()
                except Empty:
                    break
            state.pending.clear()
            state.scheduled = False

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
        """Read the validated V3 transfer snapshot and queue local video targets."""
        if not self.get_state() or not event or event.event_type != EventType.TransferComplete:
            return
        snapshot = event.snapshot()
        if not snapshot.valid or snapshot.payload is None:
            logger.warning('迅雷字幕：整理事件数据不完整，未创建下载任务')
            return
        payload = snapshot.payload
        transfer = payload.transferinfo
        if not transfer or not field(transfer, 'success', True):
            return
        target = field(transfer, 'target_diritem') or field(transfer, 'target_item')
        storage = field(target, 'storage', 'local')
        if getattr(storage, 'value', storage) not in (None, '', 'local'):
            logger.info('迅雷字幕：跳过未挂载到本地文件系统的云盘文件')
            return
        files = field(transfer, 'file_list_new', []) or []
        if not files:
            item = field(transfer, 'target_item')
            if item and field(item, 'type', 'file') == 'file':
                files = [field(item, 'path')]
        # TransferComplete includes the original file and its individual target.
        # Bind the hint to that target; never apply a movie/episode's filename
        # indiscriminately to a batch of other destination files.
        original = field(payload, 'fileitem') or field(transfer, 'fileitem')
        original_path = field(original, 'path') or field(original, 'name') or ''
        source_name = str(original_path).replace('\\', '/').rsplit('/', 1)[-1]
        target_path = field(field(transfer, 'target_item'), 'path')
        video_files = [v for v in files if Path(str(v if isinstance(v, (str, Path)) else field(v, 'path', ''))).suffix.lower() in VIDEO_EXTENSIONS]
        for value in files:
            value = value if isinstance(value, (str, Path)) else field(value, 'path')
            if not value:
                continue
            path = mapped_path(value, self._config.get('source_prefix') or '', self._config.get('local_prefix') or '')
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                hint = source_name if (str(value) == str(target_path) or (not target_path and len(video_files) == 1)) else ''
                self._enqueue('file', str(path), hint)

    def _enqueue(self, kind, value, source_name=''):
        """Coalesce duplicate work and schedule one drain for this generation."""
        with self._lifecycle_lock:
            state = self._state
            if not self._active or state is None or state.stop.is_set():
                return False
            key = (kind, os.path.normcase(value))
            if key not in state.pending:
                try:
                    state.jobs.put_nowait((kind, value, key, source_name))
                    state.pending.add(key)
                except Full:
                    logger.warning('迅雷字幕：任务队列已满，可稍后扫描补全')
                    return False
            if state.scheduled:
                return True
            # One host job drains the bounded queue. No plugin-owned scheduler
            # or permanent worker thread is created, including for manual scans.
            # A new ID also avoids the host skipping a newly queued batch while
            # the previous callback is still completing its scheduler bookkeeping.
            state.job_id = 'download_queue_' + uuid.uuid4().hex
            state.scheduled = True
            try:
                accepted = scheduler_sdk.add_plugin_once_job(
                    self.__class__.__name__, state.job_id, self._drain,
                    '迅雷字幕下载', delay_seconds=1, func_kwargs={'state': state})
            except RuntimeError:
                accepted = False
            if not accepted:
                state.scheduled = False
                logger.warning('迅雷字幕：宿主调度器尚未就绪，待下次事件或扫描重试')
            return bool(accepted)

    @staticmethod
    def _roots(config):
        """Read configured scan roots without inspecting host media tables."""
        return [Path(line.strip()).expanduser() for line in str(config.get('paths') or '').splitlines() if line.strip()]

    def _drain(self, state):
        """Process queued work serially on a host-owned background task."""
        with self._execution_lock:
            while not state.stop.is_set():
                with self._lifecycle_lock:
                    try:
                        kind, value, key, source_name = state.jobs.get_nowait()
                    except Empty:
                        state.scheduled = False
                        return
                try:
                    if kind == 'scan':
                        self._scan(state.config, state.stop)
                    else:
                        self._process(Path(value), state.config, state.stop, source_name)
                except Cancelled:
                    pass
                except Exception as exc:
                    logger.error('迅雷字幕：后台任务异常 (%s)' % type(exc).__name__)
                finally:
                    with self._lifecycle_lock:
                        state.pending.discard(key)
                    state.jobs.task_done()

    def _scan(self, config, stop):
        """Walk configured local video directories and honour cancellation."""
        roots = self._roots(config)
        if not roots:
            self._record('', '跳过', '未配置扫描目录', stop)
        for root in roots:
            if stop.is_set():
                raise Cancelled('任务已停止')
            if not root.is_absolute() or not root.is_dir():
                self._record(str(root), '失败', '扫描目录不存在或不是绝对路径', stop)
                continue
            for folder, dirs, files in os.walk(root, followlinks=False):
                if stop.is_set():
                    raise Cancelled('任务已停止')
                dirs[:] = [d for d in dirs if not d.startswith('.') and d.upper() not in ('BDMV', 'VIDEO_TS', '@RECYCLE', '#RECYCLE')]
                for name in sorted(files):
                    if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                        self._process(Path(folder) / name, config, stop)

    def _process(self, video, config, stop, source_name=''):
        """Match and save one subtitle while preserving all existing sidecars."""
        if stop.is_set():
            raise Cancelled('任务已停止')
        if not video.is_absolute() or not video.is_file():
            self._record(str(video), '失败', '视频不存在，检查 MoviePilot 容器挂载和路径映射', stop)
            return
        try:
            if config.get('skip_existing') and has_subtitle(video):
                self._record(str(video), '跳过', '已有外置中文字幕', stop)
                return
            subtitle = ThunderClient().find(video, config.get('preference', 'simplified'), bool(config.get('utf8')), stop,
                                            source_name=source_name)
            if subtitle is None:
                self._record(str(video), '未匹配', '未找到可用的中文字幕', stop)
                return
            with self._write_lock:
                if stop.is_set():
                    raise Cancelled('任务已停止')
                if config.get('skip_existing') and has_subtitle(video):
                    self._record(str(video), '跳过', '已有外置中文字幕', stop)
                    return
                target = save_subtitle(video, subtitle, stop)
            detail = '%s；来源：%s；查询：%s；版本线索：%s（未校验时间轴）' % (
                target.name, subtitle.get('name', '未知'), subtitle.get('origin', 'CID'),
                subtitle.get('match', '无版本匹配线索'))
            if source_name:
                detail += '；原视频：' + source_name
            self._record(str(video), '成功', detail, stop)
            logger.info('迅雷字幕：%s' % detail)
        except Cancelled:
            raise
        except FileExistsError:
            self._record(str(video), '跳过', '目标字幕已存在，保留原文件', stop)
        except SubtitleError as exc:
            self._record(str(video), '失败', str(exc), stop)
        except OSError as exc:
            self._record(str(video), '失败', '文件读写失败 (%s)' % type(exc).__name__, stop)

    def _record(self, path, status, message, stop):
        """Store at most 50 results using the current instance data namespace."""
        with self._history_lock:
            if stop.is_set():
                return
            history = self.get_data('history') or []
            history.insert(0, {'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                               'path': path, 'status': status, 'message': message})
            self.save_data('history', history[:50])

    def get_page(self) -> List[dict]:
        """Display existing V2-compatible processing history."""
        with self._history_lock:
            history = list(self.get_data('history') or [])
        if not history:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无字幕下载记录'}}]
        return [{'component': 'VList', 'content': [
            {'component': 'VListItem', 'props': {'title': '%s · %s' % (item['status'], Path(item['path']).name or '目录扫描'),
                                              'subtitle': '%s · %s · %s' % (item['time'], item['message'], item['path'])}}
            for item in history]}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Expose the existing configuration fields and language preferences."""
        def control(component, model, label, **props):
            """Build one configuration control in a responsive column."""
            return {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                {'component': component, 'props': {'model': model, 'label': label, **props}}]}
        rows = [
            [control('VSwitch', 'enabled', '启用插件'), control('VSwitch', 'onlyonce', '保存后扫描一次')],
            [control('VSwitch', 'skip_existing', '已有外置中文字幕时跳过'), control('VSwitch', 'utf8', '字幕保存为 UTF-8')],
            [control('VSelect', 'preference', '字幕偏好', items=[{'title': '简体优先', 'value': 'simplified'},
                 {'title': '繁体优先', 'value': 'traditional'}, {'title': '中英双语优先', 'value': 'bilingual'}]),
             control('VTextField', 'cron', '扫描周期（五段 cron，留空不定时扫描）', placeholder='0 */6 * * *')],
            [control('VTextarea', 'paths', '扫描目录（MoviePilot 容器内绝对路径，每行一个）', rows=3)],
            [control('VTextField', 'source_prefix', '事件路径前缀（可选）'), control('VTextField', 'local_prefix', '容器内路径前缀（可选）')]
        ]
        return [{'component': 'VForm', 'content': [
            {'component': 'VRow', 'content': row} for row in rows] + [
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
             'text': '自动处理整理入库文件，无需额外字幕服务器。只读取本地或已挂载文件；字幕保存为影片名.zh.srt/ass 等，不覆盖原文件。无语言标签的同名字幕也按已有字幕处理。仅检测外置字幕，不检测内嵌字幕。'}}]}], dict(DEFAULTS)
