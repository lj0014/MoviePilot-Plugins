"""Bounded local media metadata probing; no frame scan or audio alignment."""
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time


def positive_seconds(value):
    """Ignore absent, non-finite and implausible media durations."""
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 < value < 7 * 86400 else None
    except (ValueError, TypeError):
        return None


def probe_media(video, executable='', stop=None, timeout=8):
    """Read subtitle tags and container duration once, with timeout/cancellation."""
    result = {'streams': [], 'duration': None, 'error': ''}
    if stop is not None and stop.is_set():
        return result
    if executable:
        path = Path(executable).expanduser()
        command = str(path) if path.is_absolute() and path.is_file() else None
    else:
        command = shutil.which('ffprobe')
    if not command:
        return dict(result, error='未找到 ffprobe')
    args = [command, '-v', 'error', '-protocol_whitelist', 'file',
            '-probesize', '2097152', '-analyzeduration', '1000000',
            '-select_streams', 's', '-show_entries',
            'stream=index,codec_name:stream_tags=language,title:stream_disposition=forced:format=duration',
            '-of', 'json', '-i', str(Path(video).resolve())]
    process = None
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop.is_set():
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(result, error='媒体信息读取超时')
            try:
                output, _ = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode or len(output) > 1024 * 1024:
            return dict(result, error='无法读取媒体信息')
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError('Invalid metadata')
        streams = payload.get('streams', [])
        result['streams'] = [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []
        container = payload.get('format', {})
        result['duration'] = positive_seconds(container.get('duration')) if isinstance(container, dict) else None
        return result
    except (OSError, ValueError, TypeError):
        return dict(result, error='无法读取媒体信息')
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()


def embedded_subtitle(streams, policy='chinese'):
    """Trust explicit language labels, excluding known partial/forced tracks."""
    if policy not in ('chinese', 'bilingual'):
        return ''
    for stream in streams:
        tags = stream.get('tags') or {}
        disposition = stream.get('disposition') or {}
        if not isinstance(tags, dict) or not isinstance(disposition, dict):
            continue
        title = re.sub(r'[_.-]+', ' ', str(tags.get('title') or '').lower())
        if str(disposition.get('forced', 0)).lower() in ('1', 'true') or re.search(
                r'forced|signs?|songs?|commentary|特效|路牌|招牌|仅.*(?:外语|外語)|强制|強制|注释|註釋', title):
            continue
        language = str(tags.get('language') or '').lower().replace('_', '-')
        chinese = language.split('-')[0] in {'zh', 'zho', 'chi', 'cmn', 'yue'} or bool(re.search(
            r'中文|简体|簡體|繁体|繁體|简中|繁中|中英|chinese|\b(?:chs|cht|zho|chi|zh)(?:\b|_)', title))
        bilingual = chinese and bool(re.search(r'中英|英中|\benglish\b|\beng\b|\ben\b', title))
        if chinese and (policy == 'chinese' or bilingual):
            return '已有明确标注的内置中英字幕' if bilingual else '已有明确标注的内置中文字幕'
    return ''
