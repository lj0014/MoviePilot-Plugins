"""MoviePilot V2 transfer event adapter for Thunder subtitles."""
from datetime import datetime
import os
from pathlib import Path
from queue import Empty, Full, Queue
import threading
from typing import Any, Dict, List, Tuple

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .thunder import Cancelled, SubtitleError, ThunderClient, VIDEO_EXTENSIONS, has_subtitle, save_subtitle


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


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
            'source_prefix': '', 'local_prefix': '', 'preference': 'simplified',
            'skip_existing': True, 'utf8': True}


class ThunderSubtitles(_PluginBase):
    plugin_name = '迅雷字幕'
    plugin_desc = '整理入库后按视频 CID 下载迅雷中文字幕。'
    plugin_icon = 'https://raw.githubusercontent.com/lj0014/MoviePilot-Plugins/main/icons/thundersubtitles.svg'
    plugin_version = '1.0.1'
    plugin_author = 'lijin'
    author_url = 'https://github.com/lj0014'
    plugin_config_prefix = 'thundersubtitles_'
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._config = dict(DEFAULTS)
        self._worker = None
        self._stop = threading.Event()
        self._jobs = Queue(maxsize=200)
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._history_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        with self._lifecycle_lock:
            self.stop_service()
            self._config = {**DEFAULTS, **(config or {})}
            once = bool(self._config.get('onlyonce'))
            if once:
                self._config['onlyonce'] = False
                self.update_config(dict(self._config))
            if not self._config['enabled'] and not once:
                return
            self._stop = threading.Event()
            self._jobs = Queue(maxsize=200)
            self._pending = set()
            self._worker = threading.Thread(target=self._run,
                                            args=(self._jobs, self._pending, self._stop, dict(self._config)),
                                            name=self.__class__.__name__ + '-worker', daemon=True)
            self._worker.start()
            if once:
                self._enqueue('scan', 'configured-directories')

    def get_state(self) -> bool:
        return bool(self._config.get('enabled'))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
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
        if self.get_state():
            self._enqueue('scan', 'configured-directories')

    def stop_service(self):
        with self._lifecycle_lock:
            self._stop.set()
            # Serialize stop with the final sidecar commit. Network operations
            # finish with their timeout and check this generation's stop flag.
            with self._write_lock:
                pass
            if self._worker and self._worker is not threading.current_thread():
                self._worker.join(timeout=1)
            self._worker = None

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
        if not self.get_state() or not event or not event.event_data:
            return
        transfer = field(event.event_data, 'transferinfo')
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
        for value in files:
            value = value if isinstance(value, (str, Path)) else field(value, 'path')
            if not value:
                continue
            path = mapped_path(value, self._config.get('source_prefix') or '', self._config.get('local_prefix') or '')
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                self._enqueue('file', str(path))

    def _enqueue(self, kind, value):
        with self._lifecycle_lock:
            if not self._worker or not self._worker.is_alive() or self._stop.is_set():
                return False
            key = (kind, os.path.normcase(value))
            with self._pending_lock:
                if key in self._pending:
                    return False
                self._pending.add(key)
            try:
                self._jobs.put_nowait((kind, value, key))
                return True
            except Full:
                with self._pending_lock:
                    self._pending.discard(key)
                logger.warning('迅雷字幕：任务队列已满，可稍后扫描补全')
                return False

    @staticmethod
    def _roots(config):
        return [Path(line.strip()).expanduser() for line in str(config.get('paths') or '').splitlines() if line.strip()]

    def _run(self, jobs, pending, stop, config):
        while not stop.is_set():
            try:
                kind, value, key = jobs.get(timeout=0.25)
            except Empty:
                continue
            try:
                if kind == 'scan':
                    self._scan(config, stop)
                else:
                    self._process(Path(value), config, stop)
            except Cancelled:
                pass
            except Exception as exc:
                # HTTP exceptions can contain URLs; log only the error class.
                logger.error('迅雷字幕：后台任务异常 (%s)' % type(exc).__name__)
            finally:
                with self._pending_lock:
                    pending.discard(key)
                jobs.task_done()

    def _scan(self, config, stop):
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

    def _process(self, video, config, stop):
        if stop.is_set():
            raise Cancelled('任务已停止')
        if not video.is_absolute() or not video.is_file():
            self._record(str(video), '失败', '视频不存在，检查 MoviePilot 容器挂载和路径映射', stop)
            return
        try:
            if config.get('skip_existing') and has_subtitle(video):
                self._record(str(video), '跳过', '已有外置中文字幕', stop)
                return
            subtitle = ThunderClient().find(video, config.get('preference', 'simplified'), bool(config.get('utf8')), stop)
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
            self._record(str(video), '成功', target.name, stop)
            logger.info('迅雷字幕：已保存 %s' % target.name)
        except Cancelled:
            raise
        except FileExistsError:
            self._record(str(video), '跳过', '目标字幕已存在，保留原文件', stop)
        except SubtitleError as exc:
            self._record(str(video), '失败', str(exc), stop)
        except OSError as exc:
            self._record(str(video), '失败', '文件读写失败 (%s)' % type(exc).__name__, stop)

    def _record(self, path, status, message, stop):
        if stop.is_set():
            return
        with self._history_lock:
            history = self.get_data('history') or []
            history.insert(0, {'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                               'path': path, 'status': status, 'message': message})
            self.save_data('history', history[:50])

    def get_page(self) -> List[dict]:
        with self._history_lock:
            history = list(self.get_data('history') or [])
        if not history:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无字幕下载记录'}}]
        return [{'component': 'VList', 'content': [
            {'component': 'VListItem', 'props': {'title': '%s · %s' % (item['status'], Path(item['path']).name or '目录扫描'),
                                              'subtitle': '%s · %s · %s' % (item['time'], item['message'], item['path'])}}
            for item in history]}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def control(component, model, label, **props):
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
