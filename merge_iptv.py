#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 合并器
- 只保留：央视、卫视
- 不修改、不裁剪原始 URL 的 query 参数
- 检查 HTTP/HTTPS 地址是否返回非空视频数据，M3U8 继续抽查视频分片，不解码试播
- M3U8 只有在确认是 Master Playlist 时才展开
- Media Playlist / 普通直播 URL 保留原始 URL
- Master Playlist 递归展开，最终只写最终 Media Playlist URL
- 同频道允许保留多个不同播放线路
- 同 URL 去重
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from urllib.parse import urljoin

import requests
from urllib3.exceptions import HTTPError as TransportError

SOURCES = [
    'https://raw.githubusercontent.com/zhgnk/tv/refs/heads/main/live.m3u',
    'https://live.hacks.tools/tv/ipv4/categories/央视频道.m3u',
    'https://live.hacks.tools/tv/ipv4/categories/卫视频道.m3u',
    'https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.m3u',
    'https://raw.githubusercontent.com/Guovin/TV/gd/output/result.m3u',
    'https://iptv-org.github.io/iptv/languages/zho.m3u',
    'https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_all.m3u8'
]

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/152.0.0.0 Safari/537.36')

GROUP_ORDER = ['央视', '卫视']
TIMEOUT = (6, 12)
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
READ_BUDGET = 18  # 两次读取之间检查；单次阻塞仍受 TIMEOUT 的读取超时约束。
_HTTP_LOCAL = threading.local()


@contextmanager
def session_for():
    cached = getattr(_HTTP_LOCAL, 'session', None)
    if cached is not None:
        yield cached
        return
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept': '*/*',
        'Connection': 'keep-alive',
    })
    try:
        yield s
    finally:
        s.close()


@contextmanager
def http_executor(workers):
    sessions = []

    def initialize():
        manager = session_for()
        _HTTP_LOCAL.session = manager.__enter__()
        sessions.append(manager)

    try:
        with ThreadPoolExecutor(max_workers=workers, initializer=initialize) as executor:
            yield executor
    finally:
        for manager in sessions:
            manager.__exit__(None, None, None)


def read_response(response, limit, probe=False):
    """限量读取；read1 每次读取可用数据，避免持续直播流填满缓冲才返回。"""
    body = bytearray()
    deadline = time.monotonic() + READ_BUDGET
    while True:
        if time.monotonic() >= deadline:
            raise requests.Timeout('响应读取超过时间预算')
        chunk = response.raw.read1(min(4096 if probe else 65536, limit + 1 - len(body)), decode_content=True)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            raise requests.RequestException(f'响应超过 {limit} 字节限制')
        if probe:
            prefix = bytes(body[:128]).decode('utf-8-sig', errors='ignore').lstrip()
            if prefix and not '#EXTM3U'.startswith(prefix) and not prefix.startswith('#EXTM3U'):
                if len(body) >= 512 or response.headers.get('Content-Type', '').lower().startswith('video/'):
                    return bytes(body)


def get_text(url):
    with session_for() as s:
        with s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as r:
            r.raise_for_status()
            body = read_response(r, MAX_SOURCE_BYTES)
            encoding = r.encoding
            if not encoding or encoding.lower() == 'iso-8859-1':
                try:
                    return body.decode('utf-8-sig')
                except UnicodeDecodeError:
                    encoding = requests.compat.chardet.detect(body).get('encoding') or 'utf-8'
            return body.decode(encoding, errors='replace')


def parse_m3u(text):
    items = []
    extinf = None
    pending = {}
    for raw in text.replace('\ufeff', '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            extinf = line
            pending = {}
            continue
        if line.startswith('#EXTVLCOPT:'):
            # 保留这类播放参数，尤其是 http-referrer/http-user-agent
            m = re.match(r'#EXTVLCOPT:([^=]+)=(.*)$', line, re.I)
            if m:
                pending[m.group(1).strip().lower()] = m.group(2).strip()
            continue
        if line.startswith('#'):
            continue
        if extinf and re.match(r'^(?:https?|rtsp|rtmp|udp|rtp)://', line, re.I):
            name = extinf.rsplit(',', 1)[-1].strip() if ',' in extinf else '未命名'
            gm = re.search(r'group-title="([^"]*)"', extinf, re.I)
            old_group = gm.group(1).strip() if gm else ''
            items.append({
                'name': name,
                'group': old_group,
                'extinf': extinf,
                'url': line,              # 原样保留，包括所有 query 参数
                'options': pending.copy(),
            })
            extinf = None
            pending = {}
    return items


CCTV_RE = re.compile(r'(?i)(?:^|[^A-Z0-9])(?:CCTV|CGTN)[-+\s]?\d*|央视|中央电视')
SAT_RE = re.compile(r'卫视')
PROV_RE = re.compile(
    r'(?:北京|东方|湖南|浙江|江苏|安徽|山东|河南|河北|湖北|广东|广西|深圳|四川|重庆|贵州|云南|辽宁|吉林|黑龙江|江西|福建|东南|陕西|甘肃|青海|宁夏|新疆|西藏|内蒙古|海南|山西|天津)卫视'
)


def classify(name, old_group=''):
    s = f'{name} {old_group}'
    if CCTV_RE.search(s):
        return '央视'
    if SAT_RE.search(s) or PROV_RE.search(s):
        return '卫视'
    return None


def normalize_name(name):
    # 只清理显示名称，不碰 URL
    n = re.sub(r'(?i)\b(?:HD|FHD|UHD|4K|8K|HEVC|H\.265|H265|H\.264|H264)\b', '', name)
    n = re.sub(r'\s+', ' ', n).strip(' -_')
    return n or name.strip()


def attrs_from_extinf(extinf):
    return {k.lower(): v for k, v in re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def make_extinf(item, group, name):
    attrs = attrs_from_extinf(item['extinf'])
    keep = []
    # 不丢原始 M3U 的主要频道元数据
    for k in ('tvg-id', 'tvg-name', 'tvg-logo', 'tvg-chno', 'radio'):
        if attrs.get(k) is not None:
            keep.append(f'{k}="{attrs[k].replace(chr(34), "")}"')
    keep.append(f'group-title="{group}"')
    return '#EXTINF:-1 ' + ' '.join(keep) + ',' + name


def parse_master(text, base_url):
    variants = []
    bw = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith('#EXT-X-STREAM-INF:'):
            m = re.search(r'(?:AVERAGE-)?BANDWIDTH=(\d+)', line, re.I)
            bw = int(m.group(1)) if m else 0
        elif not line.startswith('#') and bw is not None:
            child = urljoin(base_url, line)
            if re.match(r'^(?:https?|rtmp)://', child, re.I):
                variants.append((bw, child))
            bw = None
    # 某些 master 不规范，只列出 m3u8 地址
    if not variants:
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith('#'):
                continue
            if re.search(r'\.m3u8(?:[?#]|$)', line, re.I):
                variants.append((0, urljoin(base_url, line)))
    seen = set()
    out = []
    for bw, u in sorted(variants, key=lambda x: x[0], reverse=True):
        if u not in seen:
            seen.add(u)
            out.append((bw, u))
    return out


def is_hls(text, ctype, url):
    return ('#EXTM3U' in text[:8192] or
            'mpegurl' in ctype.lower() or
            '.m3u8' in url.lower())


def is_master_playlist(text):
    # 必须有 STREAM-INF 才认定为 Master，避免误判普通 Media Playlist
    return bool(re.search(r'^\s*#EXT-X-STREAM-INF:', text, re.I | re.M))


def is_media_playlist(text):
    return bool(re.search(r'^\s*#EXTINF:', text, re.I | re.M) or
                re.search(r'^\s*#EXT-X-TARGETDURATION:', text, re.I | re.M) or
                re.search(r'^\s*#EXT-X-MEDIA-SEQUENCE:', text, re.I | re.M) or
                re.search(r'^\s*#EXT-X-PART:', text, re.I | re.M))


def fetch_playlist(url, options=None, timeout=None):
    headers = {}
    options = options or {}
    ua = options.get('http-user-agent') or options.get('user-agent')
    ref = options.get('http-referrer') or options.get('http-referer') or options.get('referer')
    if ua:
        headers['User-Agent'] = ua
    if ref:
        headers['Referer'] = ref
    with session_for() as s:
        request_timeout = TIMEOUT if timeout is None else (min(6, timeout), timeout)
        with s.get(url, headers=headers, timeout=request_timeout, allow_redirects=True, stream=True) as r:
            r.raise_for_status()
            ctype = r.headers.get('Content-Type', '')
            media_type = ctype.lower().split(';', 1)[0].strip()
            body = read_response(r, MAX_PLAYLIST_BYTES, probe=True)
            prefix = body[:128].decode('utf-8-sig', errors='ignore').lstrip()
            if prefix.startswith('#EXTM3U'):
                return body.decode('utf-8-sig', errors='ignore'), ctype, r.url, False
            # 只判断返回的数据类型和常见容器特征，不验证编码或实际可播放性。
            rejected = (not body or prefix.startswith(('<', '{', '[')) or
                        media_type.startswith(('text/', 'audio/')) or
                        'json' in media_type or 'xml' in media_type or 'mpegurl' in media_type)
            known_video = (body.startswith(b'FLV') or
                           (len(body) >= 377 and body[0] == body[188] == body[376] == 0x47) or
                           body[4:8] in (b'ftyp', b'styp', b'moof', b'mdat'))
            binary = any(byte < 9 or 13 < byte < 32 for byte in body[:512])
            stream_ok = not rejected and (known_video or media_type.startswith('video/') or
                                          (media_type in ('', 'application/octet-stream') and binary))
            return '', ctype, r.url, stream_ok


def flatten(url, options=None, depth=0, visited=None, timeout=None):
    """展开 Master，确认返回流数据后保留最终线路的原始 URL。"""
    if visited is None:
        visited = set()
    if depth > 8 or url in visited:
        return None
    visited.add(url)
    if not re.match(r'^https?://', url, re.I):
        return None

    try:
        text, ctype, response_url, stream_ok = fetch_playlist(url, options, timeout)
    except (requests.RequestException, TransportError, OSError):
        return None

    if stream_ok:
        return url
    if not is_hls(text, ctype, response_url):
        return None

    if is_master_playlist(text):
        variants = parse_master(text, response_url)
        for _, child in variants:
            got = flatten(child, options, depth + 1, visited.copy(), timeout)
            if got:
                return got
        return None

    if is_media_playlist(text):
        segments = []
        parts = []
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith('#'):
                segments.append(urljoin(response_url, line))
            elif line.upper().startswith('#EXT-X-PART:'):
                match = re.search(r'\bURI="([^"]+)"', line)
                if match:
                    parts.append(urljoin(response_url, match.group(1)))
        # 优先最近的完整分片；最多尝试三个，避免过期分片导致整条线路误删。
        for segment in list(dict.fromkeys(segments or parts))[-3:][::-1]:
            if not re.match(r'^https?://', segment, re.I):
                continue
            try:
                if fetch_playlist(segment, options, timeout)[3]:
                    return url
            except (requests.RequestException, TransportError, OSError):
                continue
        return None

    # 声称是 HLS 却没有有效播放列表结构，按检测失败处理。
    return None


def load_source(source):
    try:
        text = get_text(source)
        rows = parse_m3u(text)
        print(f'[源] {len(rows):5d} 条  {source}')
        return rows
    except Exception as e:
        print(f'[失败] {source} -> {e}', file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-o', '--output', default='merged_iptv.m3u')
    ap.add_argument('-w', '--workers', '--probe-workers', type=int, default=28, help='网络检测并发数，默认 28')
    ap.add_argument('--timeout', '--probe-timeout', type=float, default=12, help='单次网络读取超时秒数，默认 12')
    ap.add_argument('--no-check', action='store_true', help='不检测播放地址，只做合并/分类/去重')
    args = ap.parse_args()
    if args.workers < 1:
        ap.error('--workers 必须大于 0')
    if not 0 < args.timeout < float('inf'):
        ap.error('--timeout 必须是有效正数')

    all_items = []
    with http_executor(min(5, args.workers)) as ex:
        for rows in ex.map(load_source, SOURCES):
            all_items.extend(rows)
    if not all_items:
        raise SystemExit('未读取到任何频道，保留已有输出文件。')

    # 第一轮：URL 原样去重，不改参数
    by_url = {}
    for item in all_items:
        if classify(item['name'], item['group']) in GROUP_ORDER:
            by_url.setdefault(item['url'], item)
    items = list(by_url.values())

    # 只保留央视/卫视
    print(f'抓取 {len(all_items)} 条；央视/卫视筛选及 URL 去重后 {len(items)} 条')

    if not args.no_check:
        alive = [None] * len(items)
        retained = 0
        unsupported = sum(not re.match(r'^https?://', x['url'], re.I) for x in items)
        print(f'检测 HTTP/HTTPS 视频流并展开 Master：并发 {args.workers}，读取超时 {args.timeout:g} 秒')
        if unsupported:
            print(f'  非 HTTP/HTTPS 线路 {unsupported} 条，当前检测模式不支持，将排除')
        with http_executor(args.workers) as ex:
            futs = {ex.submit(flatten, x['url'], x.get('options'), timeout=args.timeout): i for i, x in enumerate(items)}
            done = 0
            for f in as_completed(futs):
                done += 1
                index = futs[f]
                x = items[index]
                final = f.result()
                if final:
                    y = dict(x)
                    y['url'] = final
                    alive[index] = y
                    retained += 1
                if done % 100 == 0 or done == len(futs):
                    print(f'  {done}/{len(futs)}，返回流数据 {retained}，剔除 {done - retained}')
        items = [x for x in alive if x is not None]

    # 第二轮：最终 URL 去重；同频道允许多个不同线路
    final_map = {}
    for x in items:
        final_map.setdefault(x['url'], x)
    items = list(final_map.values())

    groups = {g: [] for g in GROUP_ORDER}
    for x in items:
        group = classify(x['name'], x['group'])
        if not group:
            continue
        name = normalize_name(x['name'])
        y = dict(x)
        y['name'] = name
        groups[group].append(y)

    def sortkey(x):
        n = x['name']
        m = re.search(r'(?i)(?:CCTV|CGTN)\s*[-+]?\s*(\d+)', n)
        if m:
            return (0, int(m.group(1)), n)
        return (1, n.casefold())

    with open(args.output, 'w', encoding='utf-8', newline='\n') as f:
        f.write('#EXTM3U\n')
        for group in GROUP_ORDER:
            groups[group].sort(key=sortkey)
            for x in groups[group]:
                f.write(make_extinf(x, group, x['name']) + '\n')
                for key, value in x.get('options', {}).items():
                    f.write(f'#EXTVLCOPT:{key}={value}\n')
                f.write(x['url'] + '\n')

    print('\n完成:', args.output)
    print('央视:', len(groups['央视']))
    print('卫视:', len(groups['卫视']))
    print('合计:', sum(len(v) for v in groups.values()))


if __name__ == '__main__':
    main()
