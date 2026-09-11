#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 合并器
- 只保留：央视、卫视
- 不修改、不裁剪原始 URL 的 query 参数
- 不因为 HTTP HEAD/GET 的偶发 403/超时就武断删除可播放源
- M3U8 只有在确认是 Master Playlist 时才展开
- Media Playlist / 普通直播 URL 保留原始 URL
- Master Playlist 递归展开，最终只写最终 Media Playlist URL
- 同频道允许保留多个不同播放线路
- 同 URL 去重
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests

SOURCES = [
    'https://raw.githubusercontent.com/zhgnk/tv/refs/heads/main/live.m3u',
    'https://live.hacks.tools/tv/ipv4/categories/央视频道.m3u',
    'https://live.hacks.tools/tv/ipv4/categories/卫视频道.m3u',
    'https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.m3u'
]

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/152.0.0.0 Safari/537.36')

GROUP_ORDER = ['央视', '卫视']
TIMEOUT = (6, 12)


def session_for():
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept': '*/*',
        'Connection': 'keep-alive',
    })
    return s


def get_text(url):
    s = session_for()
    r = s.get(url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != 'iso-8859-1' else (r.apparent_encoding or 'utf-8')
    return r.text


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
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    variants = []
    for i, line in enumerate(lines):
        if not line.upper().startswith('#EXT-X-STREAM-INF:'):
            continue
        bw = 0
        m = re.search(r'(?:AVERAGE-)?BANDWIDTH=(\d+)', line, re.I)
        if m:
            bw = int(m.group(1))
        for j in range(i + 1, len(lines)):
            if lines[j].startswith('#'):
                continue
            child = urljoin(base_url, lines[j])
            if re.match(r'^(?:https?|rtmp)://', child, re.I):
                variants.append((bw, child))
            break
    # 某些 master 不规范，只列出 m3u8 地址
    if not variants:
        for line in lines:
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


def fetch_playlist(url, options=None):
    s = session_for()
    headers = {}
    options = options or {}
    ua = options.get('http-user-agent') or options.get('user-agent')
    ref = options.get('http-referrer') or options.get('http-referer') or options.get('referer')
    if ua:
        headers['User-Agent'] = ua
    if ref:
        headers['Referer'] = ref
    r = s.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get('Content-Type', '')
    text = r.content.decode('utf-8-sig', errors='ignore')
    return text, ctype, r.url


def flatten(url, options=None, depth=0, visited=None):
    """只有确认 Master 才替换 URL；普通 Media Playlist 原 URL 原样返回。"""
    if visited is None:
        visited = set()
    if depth > 8 or url in visited:
        return None
    visited.add(url)

    try:
        text, ctype, response_url = fetch_playlist(url, options)
    except Exception:
        return None

    if not is_hls(text, ctype, response_url):
        # 普通直链：请求成功就保留原始 URL，而不是重写成 redirect 后的 URL
        return url

    if is_master_playlist(text):
        variants = parse_master(text, response_url)
        for _, child in variants:
            got = flatten(child, options, depth + 1, visited.copy())
            if got:
                return got
        return None

    if is_media_playlist(text):
        # 已经是最终 Media Playlist：保留入口 URL，尤其不能丢 query 参数
        return url

    # 不确定格式：不要把 master.m3u8 当最终地址，但也不轻易删掉
    # 只有明确存在 EXT-X-STREAM-INF 才会进入 master 分支。
    return url


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
    ap.add_argument('-w', '--workers', type=int, default=28)
    ap.add_argument('--no-check', action='store_true', help='不检测播放地址，只做合并/分类/去重')
    args = ap.parse_args()

    all_items = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        fs = [ex.submit(load_source, x) for x in SOURCES]
        for f in as_completed(fs):
            all_items.extend(f.result())

    # 第一轮：URL 原样去重，不改参数
    by_url = {}
    for item in all_items:
        by_url.setdefault(item['url'], item)
    items = list(by_url.values())

    # 只保留央视/卫视
    items = [x for x in items if classify(x['name'], x['group']) in GROUP_ORDER]
    print(f'抓取 {len(all_items)} 条；URL 去重 {len(by_url)} 条；央视/卫视 {len(items)} 条')

    if not args.no_check:
        alive = []
        print(f'检测播放地址并展开 Master M3U8，workers={args.workers}')
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(flatten, x['url'], x.get('options')): x for x in items}
            done = 0
            for f in as_completed(futs):
                done += 1
                x = futs[f]
                try:
                    final = f.result()
                except Exception:
                    final = None
                if final:
                    y = dict(x)
                    y['url'] = final
                    alive.append(y)
                if done % 100 == 0 or done == len(futs):
                    print(f'  {done}/{len(futs)}，保留 {len(alive)}')
        items = alive

    # 第二轮：最终 URL 去重；同频道允许多个不同线路
    final_map = {}
    for x in items:
        final_map.setdefault(x['url'], x)
    items = list(final_map.values())

    groups = {g: [] for g in GROUP_ORDER}
    pair_seen = set()
    for x in items:
        group = classify(x['name'], x['group'])
        if not group:
            continue
        name = normalize_name(x['name'])
        key = (name.casefold(), x['url'])
        if key in pair_seen:
            continue
        pair_seen.add(key)
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
                f.write(x['url'] + '\n')

    print('\n完成:', args.output)
    print('央视:', len(groups['央视']))
    print('卫视:', len(groups['卫视']))
    print('合计:', sum(len(v) for v in groups.values()))


if __name__ == '__main__':
    main()
