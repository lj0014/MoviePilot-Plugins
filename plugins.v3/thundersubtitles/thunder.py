"""CID matching and Chinese subtitle downloads, independent of MoviePilot."""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit

import chardet
import requests

from .name_search import NameSearchError, SearchCancelled, combined_search

API = 'http://sub.xmp.sandai.net:8000/subxl/{}.json'
MAX_BYTES = 20 * 1024 * 1024
TEXT_EXTENSIONS = {'.srt', '.ass', '.ssa', '.smi', '.vtt'}
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.wmv', '.flv', '.mpg', '.mpeg', '.rmvb'}
DOWNLOAD_HOSTS = {'subtitle.v.geilijiasu.com', 'subtitle.xmp.sandai.net'}


class SubtitleError(Exception):
    pass


class Cancelled(SubtitleError):
    pass


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise Cancelled('任务已停止')


def cid_hash(path, stop=None):
    """Thunder SHA-1 of the whole small file or three 20 KiB samples."""
    with Path(path).open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        if size <= 0:
            raise SubtitleError('视频文件为空')
        digest = hashlib.sha1()
        for offset in ([0] if size < 0xF000 else [0, size // 3, size - 0x5000]):
            check_stop(stop)
            stream.seek(offset)
            length = size if size < 0xF000 else 0x5000
            remaining = length
            while remaining:
                check_stop(stop)
                data = stream.read(remaining)
                if not data:
                    raise SubtitleError('视频文件读取不完整')
                digest.update(data)
                remaining -= len(data)
        return digest.hexdigest().upper()


def clean_name(value):
    if not isinstance(value, str):
        return ''
    try:
        raw = value.encode('utf-8', errors='surrogateescape')
    except UnicodeEncodeError:
        raw = value.encode('utf-8', errors='replace')
    if re.search(rb'%[a-fA-F0-9]{2}', raw):
        raw = unquote_to_bytes(raw)
    for encoding in ('utf-8', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode('utf-8', errors='replace')
    text = text.replace('\\', '/').rsplit('/', 1)[-1]
    for pattern in (r'\[[^\[\]]*\]', r'\([^()]*\)', r'【[^【】]*】', r'（[^（）]*）'):
        text = re.sub(pattern, lambda m: '' if '\ufffd' in m.group() else m.group(), text)
    text = re.sub(r'[?\ufffd]*\ufffd[?\ufffd]*', '', text)
    return ''.join(c for c in text if c.isprintable()).strip()


def download_url(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or parts.hostname not in DOWNLOAD_HOSTS or parts.username or parts.password:
            return None
        if parts.port not in (None, 80, 443):
            return None
        return parts._replace(scheme='https', netloc=parts.hostname).geturl()
    except ValueError:
        return None


def read_response(response, limit, stop=None):
    deadline = time.monotonic() + 30
    result = bytearray()
    for chunk in response.iter_content(65536):
        check_stop(stop)
        if time.monotonic() > deadline:
            raise SubtitleError('接口响应超时')
        result.extend(chunk)
        if len(result) > limit:
            raise SubtitleError('字幕或接口响应超过大小限制')
    return bytes(result)


def number(value):
    try:
        value = float(value)
        return value if 0 <= value < 1e12 else 0
    except (ValueError, TypeError):
        return 0


def rank(candidate, preference):
    text = (candidate.get('language', '') + ' ' + candidate.get('name', '')).lower()
    simplified = bool(re.search(r'简体|簡體|简中|chs|zh[-_.]?cn|sc(?:\.|\b)', text))
    traditional = bool(re.search(r'繁体|繁體|繁中|cht|big5|zh[-_.]?tw', text))
    chinese = simplified or traditional or bool(re.search(r'中文|双语|雙語|中英|chi|zho|\bzh\b', text))
    bilingual = chinese and bool(re.search(r'英语|英語|english|eng|双语|雙語|中英', text))
    preferred = {'simplified': simplified, 'traditional': traditional, 'bilingual': bilingual}.get(preference, False)
    return (int(preferred), int(chinese), number(candidate.get('rate')), number(candidate.get('votes')))


def compatible(name, video):
    year_pattern = r'(?<![A-Za-z0-9])((?:19|20)\d{2})(?![A-Za-z0-9])'
    subtitle_year = re.search(year_pattern, name)
    video_year = re.search(year_pattern, Path(video).stem)
    if subtitle_year and video_year and subtitle_year.group(1) != video_year.group(1):
        return False
    for pattern in (r's(\d{1,2})[ ._-]*e(\d{1,3})', r'(?:cd|disc|disk)[ ._-]*(\d+)'):
        sub = re.search(pattern, name, re.I)
        movie = re.search(pattern, Path(video).stem, re.I)
        if sub and (not movie or tuple(map(int, sub.groups())) != tuple(map(int, movie.groups()))):
            return False
    return True


def release_features(name):
    """Use release labels as hints, never as proof of synchronization."""
    text = clean_name(str(name)).lower()
    source = set()
    for label, pattern in (
        ('bluray', r'blu[ ._-]?ray|b[dr][ ._-]?rip|bdremux|蓝光|藍光'),
        ('web', r'web[ ._-]?(?:dl|rip)|\bwebrip\b'),
        ('dvd', r'dvd[ ._-]?(?:rip|scr)?|dvd9|dvd5'),
        ('hdtv', r'hdtv|pdtv'),
    ):
        if re.search(pattern, text):
            source.add(label)
    cuts = set()
    for label, pattern in (
        ('extended', r'(?:^|[ ._-])ext(?:ended)?(?:[ ._-]|$)|加长|加長'),
        ('director', r"director[ ._'’-]*s?[ ._-]*cut|导演剪辑|導演剪輯"),
        ('theatrical', r'\btheatrical\b|院线版|院線版|公映版'),
        ('unrated', r'\bunrated\b|未分级|未分級'),
    ):
        if re.search(pattern, text):
            cuts.add(label)
    # A group is useful only when explicitly present at the end of a release
    # name, e.g. x265-EDGE2020. Do not infer it from the movie's title.
    stem = re.sub(r'\.(?:mkv|mp4|avi|srt|ass|ssa|zip|vtt)$', '', text)
    stem = re.sub(r'(?:[ ._-](?:chs|cht|eng|zh|en|中英|双语|简体|繁体))+$', '', stem)
    group = re.search(r'-([a-z][a-z0-9]{1,24})$', stem)
    group = group.group(1) if group and re.search(r'\b(?:19|20)\d{2}\b', text) else ''
    if group in {'dl', 'rip', 'ray', 'cn', 'tw', 'chs', 'cht', 'eng'}:
        group = ''
    return {'source': source, 'cuts': cuts, 'group': group,
            'remaster': bool(re.search(r'\bremaster(?:ed)?\b|重制|重製|修复|修復', text))}


def release_match(name, video_name):
    sub, video = release_features(name), release_features(video_name)
    score, reasons = 0, []
    if sub['cuts'] and video['cuts']:
        if sub['cuts'] != video['cuts']:
            return None, ['剪辑版本不同']
        score += 60
        reasons.append('剪辑版一致')
    elif sub['cuts']:
        # An unlabeled movie may be theatrical or extended; do not assume
        # either. Prefer an ordinary candidate over a specially labeled cut.
        score -= 15
        reasons.append('字幕含特殊剪辑标记，视频未标明')
    if sub['source'] and video['source']:
        if sub['source'] & video['source']:
            score += 40
            reasons.append('片源类型一致')
        else:
            score -= 40
            reasons.append('片源类型不同')
    if sub['group'] and sub['group'] == video['group']:
        score += 25
        reasons.append('发布组一致')
    if sub['remaster'] and video['remaster']:
        score += 10
        reasons.append('均标记重制版')
    return score, reasons


def timing_penalty(end, duration):
    """Only penalize gross outliers; credits/silence make exact gaps ambiguous."""
    duration, end = number(duration), number(end)
    if not duration or not end:
        return 0, ''
    if end > duration + max(60, duration * 0.03):
        return -120, '字幕结束时间明显超出视频'
    if duration >= 600 and end < duration * 0.65:
        return -100, '字幕时间轴明显偏短，可能为片段或分碟'
    return 0, ''


def subtitle_end(text, extension):
    """Read cue end times, never infer the film runtime from the final cue."""
    def timestamp(value):
        match = re.fullmatch(r'(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})', value.strip())
        if not match:
            return None
        hours, minutes, seconds, fraction = match.groups()
        if int(minutes) >= 60 or int(seconds) >= 60:
            return None
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, '0')) / 1000

    ends = []
    if extension in ('.srt', '.vtt'):
        for line in text.splitlines():
            match = re.match(r'\s*(\S+)\s+-->\s+(\S+)', line)
            if match:
                start, end = map(timestamp, match.groups())
                if start is not None and end is not None and end >= start:
                    ends.append(end)
    elif extension in ('.ass', '.ssa'):
        fields = ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr', 'marginv', 'effect', 'text']
        events = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('['):
                events = line.lower() == '[events]'
            elif events and line.lower().startswith('format:'):
                fields = [v.strip().lower() for v in line.split(':', 1)[1].split(',')]
            elif events and line.lower().startswith('dialogue:') and 'start' in fields and 'end' in fields:
                values = line.split(':', 1)[1].split(',', len(fields) - 1)
                if len(values) == len(fields):
                    start, end = timestamp(values[fields.index('start')]), timestamp(values[fields.index('end')])
                    if start is not None and end is not None and end >= start:
                        ends.append(end)
    # SMI SYNC gives starts only. Do not present the last start as an end.
    return max(ends) if ends else None


def selection_rank(candidate, preference, video_name, duration=None):
    score, _ = release_match(candidate.get('name', ''), video_name)
    penalty, _ = timing_penalty(number(candidate.get('duration_ms')) / 1000, duration)
    return ((score + penalty) if score is not None else -1000,) + rank(candidate, preference)


def decode_text(data):
    if not data:
        raise SubtitleError('字幕为空')
    encodings = ['utf-8-sig']
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        encodings.insert(0, 'utf-32')
    elif data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings.insert(0, 'utf-16')
    detected = chardet.detect(data).get('encoding')
    if detected:
        encodings.append('gb18030' if detected.lower() in ('gb2312', 'gbk') else detected)
    encodings += ['gb18030', 'big5']
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeError, LookupError):
            continue
    raise SubtitleError('无法识别字幕编码')


def validate_subtitle(data, extension):
    text = decode_text(data)
    lower = text.lstrip().lower()
    if '<html' in lower[:256] or '<!doctype html' in lower[:256]:
        raise SubtitleError('下载结果是网页')
    if '\x00' in text or not any(mark in lower for mark in ('-->', '[events]', '<sami', '<sync')):
        raise SubtitleError('下载结果不是可识别的文本字幕')
    # Check dialogue, rather than a Chinese filename or an ASS title comment.
    if extension in ('.ass', '.ssa'):
        dialogue = '\n'.join(line.split(',', 9)[-1] for line in text.splitlines() if line.lower().startswith('dialogue:'))
    else:
        dialogue = re.sub(r'<[^>]*>', '', text)
    if len(re.findall(r'[\u3400-\u9fff]', dialogue)) < 2:
        raise SubtitleError('字幕正文未找到中文')
    return text


class ThunderClient:
    def search(self, cid, preference='simplified', stop=None, names=None):
        if names is not None:
            try:
                records, failures = combined_search(
                    (lambda: self.search(cid, preference, stop)) if cid else None,
                    names, clean_name, stop.is_set if stop else None)
                if not records and failures:
                    raise SubtitleError('部分字幕接口查询失败，请稍后重试')
                return sorted(records, key=lambda x: rank(x, preference), reverse=True)
            except SearchCancelled:
                raise Cancelled('任务已停止') from None
            except NameSearchError:
                raise SubtitleError('迅雷字幕查询失败') from None
        if not re.fullmatch(r'[0-9A-Fa-f]{40}', cid):
            raise SubtitleError('视频 CID 无效')
        for attempt in range(3):
            check_stop(stop)
            try:
                with requests.get(API.format(cid), timeout=(5, 10), stream=True) as response:
                    response.raise_for_status()
                    payload = json.loads(read_response(response, 2 * 1024 * 1024, stop).decode('utf-8', errors='surrogateescape'))
                if not isinstance(payload, dict) or not isinstance(payload.get('sublist'), list):
                    raise ValueError('invalid schema')
                found, seen = [], set()
                for entry in payload['sublist']:
                    if not isinstance(entry, dict):
                        continue
                    url = download_url(entry.get('surl'))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    found.append({'name': clean_name(entry.get('sname')), 'url': url,
                                  'language': clean_name(entry.get('language')),
                                  'rate': entry.get('rate'), 'votes': entry.get('svote')})
                return sorted(found, key=lambda x: rank(x, preference), reverse=True)
            except Cancelled:
                raise
            except (requests.RequestException, SubtitleError, ValueError, UnicodeError):
                if attempt == 2:
                    raise SubtitleError('迅雷字幕查询失败') from None
                if stop is not None and stop.wait(0.5 * (attempt + 1)):
                    raise Cancelled('任务已停止')
        return []

    def fetch(self, url, stop=None):
        for _ in range(4):
            check_stop(stop)
            url = download_url(url)
            if not url:
                raise SubtitleError('字幕下载地址不受支持')
            with requests.get(url, timeout=(5, 10), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                response.raise_for_status()
                return read_response(response, MAX_BYTES, stop)
        raise SubtitleError('字幕下载重定向过多')

    def choices(self, data, candidate, video, stop=None):
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in TEXT_EXTENSIONS]
                if len(members) > 100 or sum(m.file_size for m in members) > MAX_BYTES:
                    raise SubtitleError('ZIP 解压大小超过限制')
                for member in members:
                    check_stop(stop)
                    name = clean_name(member.filename)
                    if compatible(name, video):
                        yield archive.read(member), Path(member.filename).suffix.lower(), name
        else:
            extension = Path(unquote(urlsplit(candidate['url']).path)).suffix.lower()
            if extension not in TEXT_EXTENSIONS:
                extension = Path(candidate['name']).suffix.lower()
            if extension not in TEXT_EXTENSIONS:
                extension = '.srt'
            yield data, extension, candidate['name']

    def find(self, video, preference='simplified', utf8=True, stop=None, source_name='', video_duration=None):
        video_duration = number(video_duration) or None
        if Path(video).stat().st_size == 0:
            raise SubtitleError('视频文件为空')
        reference = source_name or str(video)
        identity = str(video) + ' ' + reference
        try:
            cid = cid_hash(video, stop)
        except Cancelled:
            raise
        except (OSError, SubtitleError):
            cid = ''
        candidates = [item for item in self.search(cid, preference, stop, names=[reference, str(video)])
                      if compatible(item['name'], identity) and release_match(item['name'], reference)[0] is not None
                      and not re.fullmatch(r'(英语|英語|english|eng|en|日语|日語|jpn|ja)', item['language'].lower())]
        candidates.sort(key=lambda item: selection_rank(item, preference, reference, video_duration), reverse=True)
        candidates = candidates[:10]
        best, best_key, best_penalty = None, None, None
        for index, candidate in enumerate(candidates):
            check_stop(stop)
            try:
                data = self.fetch(candidate['url'], stop)
                choices = list(self.choices(data, candidate, identity, stop))
                choices.sort(key=lambda item: selection_rank({'name': item[2]}, preference, reference), reverse=True)
                for content, extension, name in choices:
                    if not compatible(name, identity):
                        continue
                    score, reasons = release_match(name, reference)
                    if score is None:
                        continue
                    try:
                        text = validate_subtitle(content, extension)
                    except SubtitleError:
                        continue
                    end = subtitle_end(text, extension) if video_duration else None
                    penalty, timing_reason = timing_penalty(end, video_duration)
                    # API metadata only reorders downloads; reject an overrun
                    # only after checking the downloaded text itself.
                    if penalty == -120:
                        continue
                    actual = dict(candidate, name=name, duration_ms=(end or 0) * 1000)
                    key = selection_rank(actual, preference, reference, video_duration)
                    result = {'data': text.encode('utf-8') if utf8 else content,
                            'extension': extension, 'name': name,
                            'source_name': clean_name(reference),
                            'origin': candidate.get('origin', 'CID'),
                            'match': '；'.join(reasons) if reasons else '无版本匹配线索',
                            'match_score': score, 'timing': timing_reason or (
                                '视频 %.1f 分钟；字幕结束 %.1f 分钟（仅排查明显时长异常）' % (video_duration / 60, end / 60)
                                if video_duration and end else '未取得可比较的时间信息')}
                    if not video_duration:
                        return result
                    if best_key is None or key > best_key:
                        best, best_key, best_penalty = result, key, penalty
            except Cancelled:
                raise
            except (requests.RequestException, SubtitleError, zipfile.BadZipFile, RuntimeError, NotImplementedError, UnicodeError):
                continue
            # Stop once no remaining candidate can improve the verified result.
            # Unknown/API-only timings get an optimistic bound, so a bogus API
            # duration cannot hide a better release. Keep the 10-download cap.
            if best is not None and best_penalty == 0:
                upper = [selection_rank(item, preference, reference) for item in candidates[index + 1:]]
                if not upper or max(upper) <= best_key:
                    break
        return best


def has_subtitle(video):
    video = Path(video)
    for item in video.parent.iterdir():
        if not item.is_file() or item.suffix.lower() not in TEXT_EXTENSIONS | {'.sub', '.idx', '.sup'}:
            continue
        if item.stem.casefold() == video.stem.casefold():
            return True
        prefix = video.stem.casefold() + '.'
        if item.stem.casefold().startswith(prefix):
            tag = item.stem[len(prefix):].lower()
            if re.search(r'(^|[._-])(zh|zho|chi|chs|cht|cn|sc|tc)([._-]|$)|中文|中英|简体|繁体', tag):
                return True
    return False


def save_subtitle(video, subtitle, stop=None):
    video = Path(video)
    extension = subtitle['extension']
    if extension not in TEXT_EXTENSIONS:
        raise SubtitleError('字幕格式不支持')
    target = video.with_name(video.stem + '.zh' + extension)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.thunder-', suffix='.part', dir=video.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(subtitle['data'])
            handle.flush()
            os.fsync(handle.fileno())
        check_stop(stop)
        try:
            os.link(temporary, target)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS):
                raise
            # Some NAS shares do not support hard links. Exclusive creation
            # still prevents replacing an existing user subtitle.
            created = False
            try:
                with target.open('xb') as handle:
                    created = True
                    handle.write(subtitle['data'])
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                if created:
                    target.unlink(missing_ok=True)
                raise
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
