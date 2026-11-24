"""HomeTube: YouTube for the TV, served by a small board on your home network.

One program does it all: the TV page, the API behind it and a stream relay. Videos play as H.264 HLS
(what every TV browser decodes), fetched by this board and handed to the TV from the same origin,
so the page never needs YouTube's servers to allow it. Subscriptions, history and Watch Later live
on the board. yt-dlp (with deno for YouTube's JavaScript checks) does the lookups and is updated
daily, because YouTube changes often. No Shorts anywhere.

  python3 hometube.py serve              # the service, started by systemd
  python3 hometube.py install-units      # root: account, optional VPN routing rule, service, daily yt-dlp update
  python3 hometube.py cookies FILE       # root: connect a YouTube account (cookies.txt); --clear to disconnect

Settings: /etc/hometube/config.json (or $HOMETUBE_CONFIG), all optional:
  bind, port        where the page and API listen (127.0.0.1:8891; 0.0.0.0 to serve the TV directly)
  route_table       a policy-routing table for the service's own traffic, e.g. your VPN tunnel's
  route_dev         the tunnel interface that traffic must leave by (e.g. wg0): checked before every
                    request to YouTube, and HomeTube refuses to go out any other way (fails closed)
  after_unit        a unit to start after, e.g. wg-quick@wg0.service
  label             shown after "HomeTube" in the page title
  rows              the home page rows: [["Title", "search words"], ...]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get('HOMETUBE_CONFIG', '/etc/hometube/config.json'))
DEFAULT_ROWS = [['Music', 'official music video'], ['Live now', 'live stream'], ['Chill', 'lofi hip hop'],
                ['Tech', 'tech review'], ['News', 'world news today'], ['Comedy', 'stand up comedy'], ['Science', 'science documentary']]
DEFAULTS: dict[str, Any] = {'bind': '127.0.0.1', 'port': 8891, 'app_dir': '/opt/hometube', 'state_dir': '/var/lib/hometube',
                            'user': 'hometube', 'route_table': None, 'route_pref': 91, 'route_dev': '', 'after_unit': '', 'label': '',
                            'rows': DEFAULT_ROWS}
_UNIT_NAME = re.compile(r'[A-Za-z0-9@._-]{1,120}')
_ACCOUNT = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
_IFACE = re.compile(r'[A-Za-z0-9_.-]{1,15}')


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """The settings file merged over the defaults; every value is checked, a bad one is refused."""
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return cfg
    if not isinstance(data, dict):
        raise ValueError(f'{path}: expected a JSON object')
    for key, value in data.items():
        if key not in DEFAULTS:
            raise ValueError(f'{path}: unknown setting {key!r}')
        cfg[key] = value
    ipaddress.ip_address(cfg['bind'])
    if not isinstance(cfg['port'], int) or not 1 <= cfg['port'] <= 65535:
        raise ValueError('port must be 1-65535')
    for key in ('app_dir', 'state_dir'):
        if not str(cfg[key]).startswith('/') or '..' in str(cfg[key]):
            raise ValueError(f'{key} must be an absolute path')
    if not _ACCOUNT.fullmatch(str(cfg['user'])):
        raise ValueError('user must be a plain account name')
    if cfg['route_table'] is not None and (not isinstance(cfg['route_table'], int) or not 1 <= cfg['route_table'] < 2**31):
        raise ValueError('route_table must be a table number or null')
    if not isinstance(cfg['route_pref'], int) or not 1 <= cfg['route_pref'] < 32766:
        raise ValueError('route_pref must be 1-32765')
    if cfg['route_dev'] and not _IFACE.fullmatch(str(cfg['route_dev'])):
        raise ValueError('route_dev must be an interface name')
    if cfg['after_unit'] and not _UNIT_NAME.fullmatch(cfg['after_unit']):
        raise ValueError('after_unit must be a unit name')
    if not isinstance(cfg['label'], str) or len(cfg['label']) > 80 or any(c in cfg['label'] for c in '<>&"\n'):
        raise ValueError('label must be plain text, 80 characters at most')
    rows = cfg['rows']
    if not isinstance(rows, list) or len(rows) > 20 or not all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) and 0 < len(x) <= 80 for x in r) for r in rows):
        raise ValueError('rows must be up to 20 [title, search words] pairs')
    return cfg


CONFIG = load_config()
APP_DIR = Path(CONFIG['app_dir'])
WEB_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / 'web'
YTDLP = APP_DIR / 'venv' / 'bin' / 'yt-dlp'
DENO = APP_DIR / 'deno'
STATE = Path(CONFIG['state_dir'])
USER = CONFIG['user']
UNIT = 'hometube'
PORT = CONFIG['port']
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}')
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_making: dict[str, threading.Event] = {}
_slots = threading.BoundedSemaphore(3)   # at most three extractions at once on a small board
_background = threading.BoundedSemaphore(2)   # home-page topic rows use two of them at most: one stays free for you
_caption_urls: dict[str, dict[tuple[str, bool], str]] = {}
COOKIES = STATE / 'cookies.txt'
ACCOUNT_FEEDS = {'subs': ':ytsubs', 'rec': ':ytrec', 'later': ':ytwatchlater', 'history': ':ythistory'}
COOKIE_DOMAINS = ('.youtube.com', 'youtube.com', '.google.com', 'accounts.google.com', '.googleapis.com')
LIST_LIMIT = 300


def _cached(key: str, ttl: float, make):
    """A value from the cache, made once: a second request for a key that is being made (a lookup
    started in the background, then OK pressed on the same video) waits for that result."""
    while True:
        with _lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            making = _making.get(key)
            if making is None:
                making = _making[key] = threading.Event()
                break
        making.wait(90)   # then look again: if that attempt failed, this request makes it itself
    try:
        value = make()
    finally:
        with _lock:
            _making.pop(key, None)
        making.set()
    with _lock:
        _cache[key] = (time.time(), value)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
    return value


def _ytdlp(args: list[str], timeout: float = 40, nice: int = 0) -> dict[str, Any]:
    _require_route()
    if not _slots.acquire(timeout=20):
        raise RuntimeError('busy: try again in a moment')
    try:
        account = ['--cookies', str(COOKIES)] if COOKIES.exists() else []
        ipv4 = ['--force-ipv4'] if CONFIG['route_table'] is not None else []
        cp = subprocess.run([str(YTDLP), '--no-cache-dir', '--no-warnings', '--js-runtimes', f'deno:{DENO}', *ipv4, *account, '-J', *args],
                            capture_output=True, text=True, timeout=timeout, env={'HOME': str(STATE), 'PATH': '/usr/bin:/bin'},
                            preexec_fn=(lambda: os.nice(nice)) if nice else None)
    finally:
        _slots.release()
    if cp.returncode != 0:
        why = (cp.stderr.strip().splitlines() or ['extraction failed'])[-1][:300]
        if YOUTUBE_REFUSED.search(why):   # YouTube asks this address to sign in: worth moving the tunnel
            _youtube['refused_at'] = int(time.time())
        raise RuntimeError(why)
    _youtube['ok_at'] = int(time.time())
    return json.loads(cp.stdout)


YOUTUBE_REFUSED = re.compile(r'confirm you.re not a bot|Sign in to confirm', re.I)
_youtube = {'ok_at': 0, 'refused_at': 0}
PROBE_VIDEO = 'jNQXAC9IVRw'   # a short, old, public video that is always there


def probe() -> dict[str, Any]:
    """A fresh lookup (never from the cache): can YouTube be reached through the way out right now?
    For the board (a VPN server picker asks this after moving HomeTube's tunnel)."""
    try:
        d = _ytdlp([f'https://www.youtube.com/watch?v={PROBE_VIDEO}'], timeout=90)
    except Exception as exc:
        return {'ok': True, 'lookup': 'refused' if YOUTUBE_REFUSED.search(str(exc)) else 'failed', 'error': str(exc)[:200]}
    for f in d.get('formats') or []:
        if f.get('manifest_url') or f.get('url'):
            _note_exit(f.get('manifest_url') or f.get('url'))
            break
    return {'ok': True, 'lookup': 'ok', 'youtube_saw': _exit_seen['ip']}


_route = {'at': 0.0, 'ok': True, 'why': ''}
_route_lock = threading.Lock()
_exit_seen = {'ip': None, 'at': 0}
ROUTE_PROBE = '142.250.0.1'   # any public address: the question is which way this account's traffic leaves


def route_status(force: bool = False) -> tuple[bool, str]:
    """With route_dev set: does this account's traffic leave by that interface right now? Asked of
    the kernel (ip route get ... uid), cached for 20 s. Without route_dev there is nothing to check."""
    dev = CONFIG['route_dev']
    if not dev:
        return True, ''
    with _route_lock:
        if not force and time.time() - _route['at'] < 20:
            return _route['ok'], _route['why']
        try:
            out = subprocess.run(['ip', '-4', 'route', 'get', ROUTE_PROBE, 'uid', str(os.getuid())],
                                 capture_output=True, text=True, timeout=5)
            words = out.stdout.split()
            via = words[words.index('dev') + 1] if 'dev' in words else None
            ok = via == dev
            why = '' if ok else f'traffic would leave by {via or "no route (" + (out.stderr.strip() or "unreachable") + ")"}, not {dev}'
        except Exception as exc:
            ok, why = False, f'route check failed: {exc}'
        _route.update(at=time.time(), ok=ok, why=why)
        return ok, why


def _require_route() -> None:
    """Fail closed: nothing goes to YouTube unless it leaves by the tunnel."""
    ok, why = route_status()
    if not ok:
        raise RuntimeError(f'HomeTube is not going out through {CONFIG["route_dev"]}: {why}. Nothing was sent.')


def _note_exit(stream_url: str) -> None:
    """The address YouTube saw, from the stream address it handed out (/ip/<address>/)."""
    m = re.search(r'/ip/([0-9a-fA-F.:]{3,45})/', str(stream_url or ''))
    if m:
        _exit_seen.update(ip=m.group(1), at=int(time.time()))


def _durable_write(path: Path, text: str) -> None:
    """Write, flush to the flash and rename: a power cut leaves the old file or the new one, never an empty one."""
    tmp = path.with_name('.' + path.name)
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'
INNERTUBE_CLIENT = {'clientName': 'WEB', 'clientVersion': '2.20251120.01.00', 'hl': 'en', 'gl': 'US'}
_api_idle: dict[str, list[http.client.HTTPSConnection]] = {}
_api_lock = threading.Lock()


def _https(host: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 15) -> bytes:
    """Kept-open HTTPS connections, up to four per host used side by side: through a far VPN exit
    each new TLS handshake costs several round trips, and the home rows load at the same time."""
    _require_route()
    for attempt in (1, 2):
        with _api_lock:
            pool = _api_idle.get(host) or []
            conn = pool.pop() if pool else None
        reused = conn is not None
        conn = conn or http.client.HTTPSConnection(host, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers={'User-Agent': UA, **(headers or {})})
            resp = conn.getresponse()
            data = resp.read(4_000_000)
        except (http.client.HTTPException, OSError):
            conn.close()
            if not reused or attempt == 2:   # a kept connection may have timed out: one retry on a new one
                raise
            continue
        if resp.will_close:
            conn.close()
        else:
            with _api_lock:
                pool = _api_idle.setdefault(host, [])
                if len(pool) < 4:
                    pool.append(conn)
                else:
                    conn.close()
        if resp.status >= 400:
            raise RuntimeError(f'{host} answered {resp.status}')
        return data
    raise RuntimeError('unreachable')


def _walk(node: Any, key: str, out: list, limit: int = 80) -> None:
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for v in node.values():
            _walk(v, key, out, limit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, out, limit)


def _text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ''
    if 'simpleText' in obj:
        return str(obj['simpleText'])
    return ''.join(str(r.get('text', '')) for r in obj.get('runs', []) if isinstance(r, dict))


def _duration_seconds(text: str) -> int | None:
    try:
        parts = [int(p) for p in text.split(':')]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _is_short(url: str) -> bool:
    """Shorts are left out of every list."""
    return '/shorts/' in str(url or '')


def _thumb(video_id: str) -> str:
    return f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'


def search_direct(query: str, count: int) -> list[dict[str, Any]]:
    """YouTube's own search endpoint (the same one its web page uses): about a second, versus
    several for a yt-dlp subprocess. Only video results are kept."""
    body = json.dumps({'context': {'client': INNERTUBE_CLIENT}, 'query': query}).encode()
    data = json.loads(_https('www.youtube.com', 'POST', '/youtubei/v1/search?prettyPrint=false',
                             body, {'Content-Type': 'application/json'}))
    found: list = []
    _walk(data, 'videoRenderer', found, limit=count * 2)
    out = []
    for v in found:
        vid = v.get('videoId') or ''
        nav = v.get('navigationEndpoint') or {}
        if not VIDEO_ID.fullmatch(vid) or 'reelWatchEndpoint' in nav or _is_short(((nav.get('commandMetadata') or {}).get('webCommandMetadata') or {}).get('url')):
            continue
        owner = (v.get('ownerText') or v.get('longBylineText') or {}).get('runs') or [{}]
        cid = ((owner[0].get('navigationEndpoint') or {}).get('browseEndpoint') or {}).get('browseId')
        length = _text(v.get('lengthText'))
        out.append({'id': vid, 'title': _text(v.get('title'))[:200],
                    'channel': str(owner[0].get('text') or '')[:80],
                    'channel_id': cid if CHANNEL_ID.fullmatch(str(cid or '')) else None,
                    'duration': _duration_seconds(length) if length else None,
                    'views': _text(v.get('viewCountText'))[:40],
                    'thumb': _thumb(vid)})
        if len(out) >= count:
            break
    return out


def search(query: str, count: int = 24, ttl: float = 600, background: bool = False, force: bool = False) -> list[dict[str, Any]]:
    query = query.strip()[:120]
    if not query:
        return []
    def make():
        try:   # YouTube's search endpoint first: about a second
            direct = search_direct(query, count)
            if direct:
                return direct
        except Exception:
            pass   # fall back to yt-dlp, slower but maintained against YouTube's changes
        if background:
            with _background:
                data = _ytdlp([f'ytsearch{count}:{query}', '--flat-playlist'])
        else:
            data = _ytdlp([f'ytsearch{count}:{query}', '--flat-playlist'])
        out = []
        for e in data.get('entries') or []:
            vid = e.get('id') or ''
            if VIDEO_ID.fullmatch(vid) and not _is_short(e.get('url')):
                out.append({'id': vid, 'title': str(e.get('title') or '')[:200], 'channel': str(e.get('channel') or e.get('uploader') or '')[:80],
                            'channel_id': e.get('channel_id') if CHANNEL_ID.fullmatch(str(e.get('channel_id') or '')) else None,
                            'duration': e.get('duration'), 'views': e.get('view_count'), 'thumb': _thumb(vid)})
        return out
    if force:   # refresh: the cached rows stay in use until the new ones are in
        value = make()
        with _lock:
            _cache[f's:{query}:{count}'] = (time.time(), value)
        return value
    return _cached(f's:{query}:{count}', ttl, make)


def video(video_id: str, background: bool = False) -> dict[str, Any]:
    if not VIDEO_ID.fullmatch(video_id or ''):
        raise ValueError('not a video id')
    def make():
        if background:   # a lookup ahead of time: one slot always stays free for the video you open
            with _background:
                d = _ytdlp([f'https://www.youtube.com/watch?v={video_id}'], nice=10)
        else:
            d = _ytdlp([f'https://www.youtube.com/watch?v={video_id}'])
        formats = d.get('formats') or []
        hls = [f for f in formats if str(f.get('protocol', '')).startswith('m3u8')]
        master = next((f.get('manifest_url') for f in hls if f.get('manifest_url')), None)
        best = max(hls, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0), default=None)
        subs = []
        for source, auto in ((d.get('subtitles') or {}, False), (d.get('automatic_captions') or {}, True)):
            for lang, tracks in source.items():
                if auto and lang not in ('en', d.get('language') or ''):
                    continue   # automatic captions: English and the video's own language only
                if any(t.get('ext') == 'vtt' for t in tracks) and len(subs) < 40:
                    subs.append({'lang': lang, 'name': (tracks[0].get('name') or lang)[:40] + (' (auto)' if auto else ''), 'auto': auto})
        _caption_urls[video_id] = {(s['lang'], s['auto']): next(t['url'] for t in (d.get('automatic_captions' if s['auto'] else 'subtitles') or {})[s['lang']] if t.get('ext') == 'vtt') for s in subs}
        chapters = [{'start': c.get('start_time'), 'title': str(c.get('title') or '')[:80]} for c in (d.get('chapters') or [])][:100]
        _note_exit(master or (best or {}).get('url'))
        return {'id': video_id, 'title': d.get('title'), 'channel': d.get('channel') or d.get('uploader'),
                'subtitles': subs, 'chapters': chapters, 'views': d.get('view_count'), 'upload_date': d.get('upload_date'),
                'channel_id': d.get('channel_id'), 'duration': d.get('duration'), 'thumb': _thumb(video_id),
                'hls': master or (best or {}).get('url'), 'live': bool(d.get('is_live')),
                'description': str(d.get('description') or '')[:600]}
    # stream addresses stay valid for a few hours; keep them two
    return _cached(f'v:{video_id}', 7200, make)


_warm_next: list[str] = []
_warm_busy = False
_warm_lock = threading.Lock()


def warm(video_id: str) -> str:
    """Start looking a video up before it is opened (the lookup takes 15 to 18 s on this board).
    One at a time, newest first: a card focused and left again is dropped from the queue."""
    global _warm_busy
    if not VIDEO_ID.fullmatch(video_id or ''):
        raise ValueError('not a video id')
    with _lock:
        hit = _cache.get(f'v:{video_id}')
        if hit and time.time() - hit[0] < 7200:
            return 'ready'
        if f'v:{video_id}' in _making:
            return 'busy'
    with _warm_lock:
        _warm_next[:] = [video_id]
        if _warm_busy:
            return 'queued'
        _warm_busy = True
    threading.Thread(target=_warm_worker, daemon=True).start()
    return 'started'


def _warm_worker() -> None:
    global _warm_busy
    while True:
        with _warm_lock:
            if not _warm_next:
                _warm_busy = False
                return
            vid = _warm_next.pop()
        try:
            video(vid, background=True)
        except Exception:
            pass   # opening the video will try again and show the error


def _lockup_video(lv: dict[str, Any]) -> dict[str, Any] | None:
    """One related video from YouTube's lockup layout (title, channel, length)."""
    vid = str(lv.get('contentId') or '')
    if lv.get('contentType') != 'LOCKUP_CONTENT_TYPE_VIDEO' or not VIDEO_ID.fullmatch(vid):
        return None
    meta = ((lv.get('metadata') or {}).get('lockupMetadataViewModel') or {})
    title = str(((meta.get('title') or {}).get('content')) or '')
    rows = (((meta.get('metadata') or {}).get('contentMetadataViewModel') or {}).get('metadataRows') or [])
    channel = ''
    if rows and (rows[0].get('metadataParts') or []):
        channel = str(((rows[0]['metadataParts'][0].get('text') or {}).get('content')) or '')
    badges: list = []
    _walk(lv.get('contentImage') or {}, 'thumbnailBadgeViewModel', badges, 4)
    duration, live = None, False
    for b in badges:
        text = str((b or {}).get('text') or '')
        if re.fullmatch(r'\d{1,2}(:\d{2}){1,2}', text):
            duration = _duration_seconds(text)
        elif text.upper() == 'LIVE':
            live = True
    if not title:
        return None
    return {'id': vid, 'title': title[:200], 'channel': channel[:80], 'channel_id': None,
            'duration': duration, 'live': live, 'thumb': _thumb(vid)}


def related(video_id: str) -> list[dict[str, Any]]:
    """What YouTube puts next to a video (its watch-next list), without ads or playlists."""
    if not VIDEO_ID.fullmatch(video_id or ''):
        raise ValueError('not a video id')
    def make():
        body = json.dumps({'context': {'client': INNERTUBE_CLIENT}, 'videoId': video_id}).encode()
        data = json.loads(_https('www.youtube.com', 'POST', '/youtubei/v1/next?prettyPrint=false', body,
                                 {'Content-Type': 'application/json'}))
        found: list = []
        _walk(data, 'lockupViewModel', found, 60)
        out, seen = [], {video_id}
        for lv in found:
            item = _lockup_video(lv) if isinstance(lv, dict) else None
            if item and item['id'] not in seen:
                seen.add(item['id'])
                out.append(item)
        return out[:24]
    return _cached(f'r:{video_id}', 1800, make)


LIST_ID = re.compile(r'(?:RD|PL|OL|UU|FL|LL)[A-Za-z0-9_-]{8,64}')


def playlist(list_id: str, video_id: str = '') -> dict[str, Any]:
    """A mix or playlist as a queue: YouTube's watch-next panel for that list (about 25 videos).
    A mix (RD + a video id) is YouTube's endless radio built from that video."""
    if not LIST_ID.fullmatch(list_id or '') or (video_id and not VIDEO_ID.fullmatch(video_id)):
        raise ValueError('not a playlist')
    if not video_id and list_id.startswith('RD') and VIDEO_ID.fullmatch(list_id[2:]):
        video_id = list_id[2:]
    def make():
        req: dict[str, Any] = {'context': {'client': INNERTUBE_CLIENT}, 'playlistId': list_id}
        if video_id:
            req['videoId'] = video_id
        data = json.loads(_https('www.youtube.com', 'POST', '/youtubei/v1/next?prettyPrint=false', json.dumps(req).encode(),
                                 {'Content-Type': 'application/json'}))
        panel = ((((data.get('contents') or {}).get('twoColumnWatchNextResults') or {}).get('playlist') or {}).get('playlist') or {})
        out, seen = [], set()
        for entry in panel.get('contents') or []:
            r = (entry or {}).get('playlistPanelVideoRenderer') or {}
            vid = str(r.get('videoId') or '')
            if not VIDEO_ID.fullmatch(vid) or vid in seen:
                continue
            seen.add(vid)
            length = _text(r.get('lengthText'))
            out.append({'id': vid, 'title': _text(r.get('title'))[:200], 'channel': _text(r.get('shortBylineText'))[:80],
                        'channel_id': None, 'duration': _duration_seconds(length) if length else None, 'thumb': _thumb(vid)})
        return {'id': list_id, 'title': _text(panel.get('title')) or str(panel.get('title') or '')[:120] or 'Queue', 'items': out[:200]}
    return _cached(f'p:{list_id}:{video_id}', 1800, make)


# ---- stream relay
# googlevideo sends no CORS headers, so a page served by this board cannot read the playlists or
# segments itself (hls.js then shows a black picture). The portal relays them instead: every address
# handed to the TV is a signed /api/tv/hls link, fetched here through the tunnel (the IP the stream
# is bound to), and nothing else can be fetched through it.
RELAY_PATH = '/api/tv/hls'
RELAY_HOST = re.compile(r'(?:[a-z0-9-]+\.)+googlevideo\.com')
_URI_ATTR = re.compile(r'URI="([^"]+)"')
_relay_key: bytes | None = None


def _key() -> bytes:
    """Signing key for relay links, kept across restarts so a playing video survives one."""
    global _relay_key
    if _relay_key is None:
        path = STATE / 'relay.key'
        try:
            key = path.read_bytes()
        except OSError:
            key = b''
        if len(key) != 32:
            key = secrets.token_bytes(32)
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name('.relay.key')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        _relay_key = key
    return _relay_key


def _sign(url: str) -> str:
    return hmac.new(_key(), url.encode(), hashlib.sha256).hexdigest()[:32]


def relay_link(url: str) -> str:
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    return f'{RELAY_PATH}?u={token}&s={_sign(url)}'


def relay_target(token: str, signature: str) -> str:
    """The stream address behind a relay link, only if this service signed it and it is googlevideo."""
    try:
        url = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError('bad relay link') from None
    if not hmac.compare_digest(_sign(url), signature or ''):
        raise ValueError('bad relay link')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not RELAY_HOST.fullmatch(parts.hostname or ''):
        raise ValueError('not a stream address')
    return url


def rewrite_playlist(text: str, base: str, h264_only: bool = False) -> str:
    """Point every address in an HLS playlist at the relay. With h264_only, a master playlist keeps
    only its H.264 variants (when it has any): smart-TV browsers often cannot decode VP9."""
    lines = [line.strip() for line in text.splitlines()]
    keep_avc = h264_only and any(l.startswith('#EXT-X-STREAM-INF') and 'avc1' in l for l in lines)
    out: list[str] = []
    skip_uri = False
    for line in lines:
        if not line:
            continue
        if line.startswith('#'):
            if keep_avc and line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-I-FRAME-STREAM-INF')) and 'avc1' not in line:
                skip_uri = line.startswith('#EXT-X-STREAM-INF')
                continue
            out.append(_URI_ATTR.sub(lambda m: f'URI="{relay_link(urllib.parse.urljoin(base, m.group(1)))}"', line))
        elif skip_uri:
            skip_uri = False
        else:
            out.append(relay_link(urllib.parse.urljoin(base, line)))
    return '\n'.join(out) + '\n'


SEG_CACHE_BYTES = 96_000_000   # about 20 segments at 1080p: what is fetched ahead and not yet played
SEG_CACHE_TTL = 180
PREFETCH_AHEAD = 2             # segments fetched while the TV plays the current one
_REDIRECTS = (301, 302, 303, 307, 308)
_idle: dict[str, list[http.client.HTTPSConnection]] = {}
_idle_lock = threading.Lock()
_seg_cache: dict[str, tuple[float, int, dict[str, str], bytes]] = {}
_seg_bytes = 0
_inflight: dict[str, threading.Event] = {}
_next_seg: dict[str, str] = {}
_seg_lock = threading.Lock()


def _stream_get(url: str, range_header: str | None = None) -> tuple[int, dict[str, str], bytes, str]:
    """GET a googlevideo address over a kept-open connection: through a far tunnel exit a new
    TCP+TLS connection costs about a second, and a warm one also runs about twice as fast.
    Returns status, the headers worth passing on, the body, and the final address."""
    _require_route()
    headers = {'User-Agent': UA}
    if range_header and re.fullmatch(r'bytes=\d{0,15}-\d{0,15}', range_header):
        headers['Range'] = range_header
    for _hop in range(4):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not RELAY_HOST.fullmatch(parts.hostname or ''):
            raise ValueError('stream address off googlevideo')
        host, path = parts.hostname, parts.path + ('?' + parts.query if parts.query else '')
        for attempt in (1, 2):
            with _idle_lock:
                pool = _idle.get(host) or []
                conn = pool.pop() if pool else None
            reused = conn is not None
            conn = conn or http.client.HTTPSConnection(host, timeout=30)
            try:
                conn.request('GET', path, headers=headers)
                resp = conn.getresponse()
                if int(resp.getheader('Content-Length') or 0) > 64_000_000:
                    raise ValueError('stream part too large')
                body = resp.read()
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                if not reused or attempt == 2:   # a kept connection may have timed out: one retry on a new one
                    raise
        kept = {name: resp.getheader(name) for name in ('Content-Type', 'Content-Range', 'Accept-Ranges', 'Location') if resp.getheader(name)}
        if resp.will_close:
            conn.close()
        else:
            with _idle_lock:
                pool = _idle.setdefault(host, [])
                if len(pool) < 6:
                    pool.append(conn)
                else:
                    conn.close()
        if resp.status in _REDIRECTS and kept.get('Location'):
            url = urllib.parse.urljoin(url, kept['Location'])
            continue
        kept.pop('Location', None)
        return resp.status, kept, body, url
    raise ValueError('too many redirects')


def _remember_order(urls: list[str]) -> None:
    """A media playlist's segment order, so each request can start fetching the ones after it."""
    with _seg_lock:
        for a, b in zip(urls, urls[1:]):
            _next_seg[a] = b
        while len(_next_seg) > 20000:
            _next_seg.pop(next(iter(_next_seg)))


def _store(url: str, entry: tuple[float, int, dict[str, str], bytes]) -> None:
    global _seg_bytes
    with _seg_lock:
        old = _seg_cache.pop(url, None)
        if old:
            _seg_bytes -= len(old[3])
        _seg_cache[url] = entry
        _seg_bytes += len(entry[3])
        while _seg_bytes > SEG_CACHE_BYTES and _seg_cache:
            _seg_bytes -= len(_seg_cache.pop(next(iter(_seg_cache)))[3])


def _take(url: str) -> tuple[float, int, dict[str, str], bytes] | None:
    global _seg_bytes
    with _seg_lock:
        hit = _seg_cache.pop(url, None)   # each segment is played once: hand it over and free the memory
        if hit:
            _seg_bytes -= len(hit[3])
    return hit if hit and time.time() - hit[0] < SEG_CACHE_TTL else None


def _prefetch(url: str) -> None:
    with _seg_lock:
        if url in _seg_cache or url in _inflight:
            return
        done = _inflight[url] = threading.Event()
    def work():
        try:
            status, headers, body, _final = _stream_get(url)
            if status == 200:
                _store(url, (time.time(), status, headers, body))
        except Exception:
            pass   # the TV's own request will fetch it and report any error
        finally:
            with _seg_lock:
                _inflight.pop(url, None)
            done.set()
    threading.Thread(target=work, daemon=True).start()


def _prefetch_after(url: str, count: int = PREFETCH_AHEAD) -> None:
    with _seg_lock:
        ahead, cur = [], url
        for _ in range(count):
            cur = _next_seg.get(cur)
            if not cur:
                break
            ahead.append(cur)
    for nxt in ahead:
        _prefetch(nxt)


def segment(url: str, range_header: str | None = None) -> tuple[int, dict[str, str], bytes, str]:
    """A segment for the TV: from what was fetched ahead when possible, then start on the next ones."""
    if range_header:
        return _stream_get(url, range_header)
    with _seg_lock:
        waiting = _inflight.get(url)
    if waiting:
        waiting.wait(45)
    hit = _take(url)
    result = (hit[1], hit[2], hit[3], url) if hit else _stream_get(url)
    _prefetch_after(url)
    return result


def stream_master(video_id: str) -> str:
    """The video's HLS master playlist, H.264 only, every address through the relay."""
    master = video(video_id).get('hls')
    if not master:
        raise ValueError('No playable stream for this video.')
    status, _headers, body, final = _stream_get(master)
    if status != 200:
        raise ValueError(f'YouTube answered {status} for the stream list')
    return rewrite_playlist(body.decode('utf-8', 'replace'), final, h264_only=True)


def _is_playlist(headers: dict[str, str], url: str) -> bool:
    ctype = (headers.get('Content-Type') or '').lower()
    return 'mpegurl' in ctype or urllib.parse.urlsplit(url).path.endswith('.m3u8')



def channel(channel_id: str) -> list[dict[str, Any]]:
    """A channel's latest videos from its public feed (no API key)."""
    if not CHANNEL_ID.fullmatch(channel_id or ''):
        raise ValueError('not a channel id')
    def make():
        _require_route()
        url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = urllib.request.urlopen(req, timeout=20).read(2_000_000)
        ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015', 'media': 'http://search.yahoo.com/mrss/'}
        root = ET.fromstring(raw)
        name = root.findtext('a:title', default='', namespaces=ns)
        out = []
        for e in root.findall('a:entry', ns)[:15]:
            vid = e.findtext('yt:videoId', default='', namespaces=ns)
            link = (e.find('a:link', ns).get('href') if e.find('a:link', ns) is not None else '')
            if VIDEO_ID.fullmatch(vid) and not _is_short(link):
                out.append({'id': vid, 'title': e.findtext('a:title', default='', namespaces=ns)[:200], 'channel': name,
                            'channel_id': channel_id, 'published': e.findtext('a:published', default='', namespaces=ns),
                            'thumb': _thumb(vid)})
        return out
    return _cached(f'c:{channel_id}', 900, make)


def _timedtext_to_vtt(xml: bytes) -> str:
    """YouTube timedtext XML -> WebVTT. Both forms become cues: <text start dur> in seconds
    (the classic transcript) and <p t d> in milliseconds (timedtext format 3)."""
    import html
    cues = ['WEBVTT', '']
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return 'WEBVTT\n'
    def ts(s: float) -> str:
        h, s = divmod(s, 3600); m, s = divmod(s, 60)
        return f'{int(h):02d}:{int(m):02d}:{s:06.3f}'
    for t in root.iter():
        if t.tag == 'text':
            start_raw, dur_raw, scale = t.get('start'), t.get('dur'), 1.0
        elif t.tag == 'p':
            start_raw, dur_raw, scale = t.get('t'), t.get('d'), 1000.0
        else:
            continue
        try:
            start = float(start_raw or 0) / scale
            dur = float(dur_raw) / scale if dur_raw else 2.0
        except ValueError:
            continue
        text = html.unescape(''.join(t.itertext())).replace('\n', ' ').strip()
        if not text:
            continue
        cues.append(f'{ts(start)} --> {ts(start + dur)}')
        cues.append(text)
        cues.append('')
    return '\n'.join(cues)


def captions(video_id: str, lang: str, auto: bool) -> str:
    """A subtitle track as WebVTT, fetched through the tunnel (the page cannot reach it directly).
    YouTube serves captions as timedtext XML for TV clients, so we convert to VTT here."""
    if not VIDEO_ID.fullmatch(video_id or '') or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', lang or ''):
        raise ValueError('bad caption request')
    if video_id not in _caption_urls:
        video(video_id)
    url = _caption_urls.get(video_id, {}).get((lang, auto))
    if not url:
        raise ValueError('no such caption track')
    def make():
        _require_route()
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20).read(3_000_000)
        if raw.lstrip().startswith(b'WEBVTT'):
            return raw.decode('utf-8', 'replace')
        # timedtext XML: what the caption addresses of the TV player return
        if raw.lstrip().startswith(b'<'):
            return _timedtext_to_vtt(raw)
        return raw.decode('utf-8', 'replace')
    return _cached(f'cc:{video_id}:{lang}:{auto}', 7200, make)


def _list_path(name: str) -> Path:
    return STATE / f'{name}.json'


def get_list(name: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(_list_path(name).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def put_item(name: str, item: dict[str, Any], remove: bool = False) -> list[dict[str, Any]]:
    """history (newest first, with the position reached) and later (Watch Later)."""
    vid = str(item.get('id', ''))
    if name not in ('history', 'later') or not VIDEO_ID.fullmatch(vid):
        raise ValueError('bad list item')
    items = [i for i in get_list(name) if i.get('id') != vid]
    if not remove:
        clean = {'id': vid, 'title': str(item.get('title') or '')[:200], 'channel': str(item.get('channel') or '')[:80],
                 'channel_id': item.get('channel_id') if CHANNEL_ID.fullmatch(str(item.get('channel_id') or '')) else None,
                 'thumb': _thumb(vid), 'at': int(time.time())}
        for key in ('position', 'duration'):
            if isinstance(item.get(key), (int, float)) and 0 <= item[key] < 10**6:
                clean[key] = round(float(item[key]), 1)
        items.insert(0, clean)
    STATE.mkdir(parents=True, exist_ok=True)
    _durable_write(_list_path(name), json.dumps(items[:LIST_LIMIT]))
    return items[:LIST_LIMIT]


def import_takeout(csv_text: str) -> list[dict[str, str]]:
    """Subscriptions from a Google Takeout subscriptions.csv (Channel Id, Channel Url, Channel Title)."""
    added = 0
    for line in csv_text.splitlines()[1:2000]:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 3 and CHANNEL_ID.fullmatch(parts[0]):
            subscribe(parts[0], ','.join(parts[2:]), True)
            added += 1
    return subscriptions()


def set_cookies(text: str) -> dict[str, Any]:
    """A Netscape cookies.txt exported from a private window: only YouTube and Google cookies are kept."""
    kept = []
    for line in str(text).splitlines()[:5000]:
        fields = line.split('\t')
        if len(fields) == 7 and fields[0].removeprefix('#HttpOnly_').endswith(COOKIE_DOMAINS):
            kept.append(line)
    if not any('youtube.com' in l for l in kept) or not any(l.split('\t')[5] in ('SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO') for l in kept):
        raise ValueError('These are not signed-in YouTube cookies (export them from youtube.com while signed in).')
    STATE.mkdir(parents=True, exist_ok=True)
    _durable_write(COOKIES, '# Netscape HTTP Cookie File\n' + '\n'.join(kept) + '\n')
    os.chmod(COOKIES, 0o600)
    _cache.clear()
    return {'connected': True, 'cookies': len(kept)}


def clear_cookies() -> dict[str, Any]:
    COOKIES.unlink(missing_ok=True)
    _cache.clear()
    return {'connected': False}


def account_feed(name: str, count: int = 40) -> list[dict[str, Any]]:
    if name not in ACCOUNT_FEEDS:
        raise ValueError('unknown feed')
    if not COOKIES.exists():
        raise ValueError('No YouTube account is connected.')
    def make():
        data = _ytdlp([ACCOUNT_FEEDS[name], '--flat-playlist', '-I', f'1:{count}'], timeout=90)
        out = []
        for e in data.get('entries') or []:
            vid = e.get('id') or ''
            if VIDEO_ID.fullmatch(vid) and not _is_short(e.get('url')):
                out.append({'id': vid, 'title': str(e.get('title') or '')[:200], 'channel': str(e.get('channel') or e.get('uploader') or '')[:80],
                            'channel_id': e.get('channel_id') if CHANNEL_ID.fullmatch(str(e.get('channel_id') or '')) else None,
                            'duration': e.get('duration'), 'thumb': _thumb(vid)})
        return out
    return _cached(f'a:{name}', 900, make)


def subscriptions() -> list[dict[str, str]]:
    try:
        data = json.loads((STATE / 'channels.json').read_text())
        return [c for c in data if isinstance(c, dict) and CHANNEL_ID.fullmatch(str(c.get('id', '')))][:100]
    except (OSError, ValueError):
        return []


def subscribe(channel_id: str, name: str, add: bool) -> list[dict[str, str]]:
    if not CHANNEL_ID.fullmatch(channel_id or ''):
        raise ValueError('not a channel id')
    subs = [c for c in subscriptions() if c['id'] != channel_id]
    if add:
        subs.append({'id': channel_id, 'name': str(name)[:80]})
    STATE.mkdir(parents=True, exist_ok=True)
    _durable_write(STATE / 'channels.json', json.dumps(subs))
    return subs


def home() -> list[dict[str, Any]]:
    """The newest videos across subscribed channels."""
    items: list[dict[str, Any]] = []
    subs = subscriptions()[:30]
    def one(c):
        try:
            return channel(c['id'])[:6]
        except Exception:
            return []
    with __import__('concurrent.futures').futures.ThreadPoolExecutor(max_workers=6) as pool:   # feeds side by side
        for got in pool.map(one, subs):
            items += got
    items.sort(key=lambda v: v.get('published', ''), reverse=True)
    return items[:48]


# ---- the web side: the TV page, its files, the API and the relay, from one small server
API = '/api/tv/'
STATIC = {'hometube.js': 'text/javascript; charset=utf-8', 'hometube.css': 'text/css; charset=utf-8',
          'hls.min.js': 'text/javascript; charset=utf-8', 'mark.svg': 'image/svg+xml'}
PAGE_CSP = ("default-src 'self'; img-src 'self' data: https://i.ytimg.com; media-src 'self' blob:; connect-src 'self'; "
            "worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
API_PER_MINUTE = 120     # page, lists, lookups: per address on the network
HLS_PER_MINUTE = 1500    # playback asks every few seconds, faster while buffering
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def allowed(key: str, limit: int, window: float = 60.0) -> bool:
    """No sign-in on the TV page, so each address gets a bounded number of requests a minute."""
    now = time.time()
    with _hits_lock:
        hits = [t for t in _hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _hits[key] = hits
            return False
        hits.append(now)
        _hits[key] = hits
        if len(_hits) > 4000:
            _hits.clear()
        return True


ROW_COUNT = 16
ROW_REFRESH = 45 * 60   # the rows are kept an hour: refreshed before they run out, so home never waits


def keep_rows_warm() -> None:
    """Look the home rows up now and every 45 minutes, so opening home never waits on YouTube."""
    while True:
        for _title, words in CONFIG['rows']:
            try:
                search(words, count=24, ttl=3600, background=True, force=True)
            except Exception:
                pass   # a failed row is looked up again when the page asks for it
            time.sleep(2)
        time.sleep(ROW_REFRESH)


def _version(name: str) -> str:
    return hashlib.sha256((WEB_DIR / name).read_bytes()).hexdigest()[:12]


ASSET_VERSIONS = {name: _version(name) for name in STATIC if (WEB_DIR / name).exists()}


def page_html() -> bytes:
    title = 'HomeTube' + (' · ' + CONFIG['label'] if CONFIG['label'] else '')
    html = (WEB_DIR / 'index.html').read_bytes().replace(b'__HOMETUBE_TITLE__', title.encode())
    for name, version in ASSET_VERSIONS.items():   # versioned addresses: the files can be cached for a year
        html = html.replace(f'/assets/hometube/{name}"'.encode(), f'/assets/hometube/{name}?v={version}"'.encode())
    return html


class Handler(BaseHTTPRequestHandler):
    server_version = 'HomeTube'
    sys_version = ''

    def log_message(self, fmt, *args):  # quiet: no viewing history in the journal
        pass

    def local(self) -> bool:
        """The request comes from this board itself (a portal in front of HomeTube, or the shell)."""
        try:
            addr = ipaddress.ip_address(self.client_address[0])
            return addr.is_loopback or (getattr(addr, 'ipv4_mapped', None) or addr).is_loopback
        except ValueError:
            return False

    def head(self, status: int, ctype: str, length: int, cache: str = 'no-store', csp: str | None = None,
             extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', csp or "default-src 'none'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def reply(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.head(status, 'application/json', len(body))
        self.wfile.write(body)

    def send_bytes(self, status: int, body: bytes, ctype: str, cache: str = 'no-store', csp: str | None = None) -> None:
        self.head(status, ctype, len(body), cache, csp)
        self.wfile.write(body)

    def relay(self, target: str) -> None:
        """A playlist comes back rewritten (a media playlist also starts the first segments on their
        way); a segment comes from what was fetched ahead, or straight from YouTube."""
        if not _is_playlist({}, target) and '/videoplayback' in target:
            status, headers, body, final = segment(target, self.headers.get('Range'))
        else:
            status, headers, body, final = _stream_get(target, self.headers.get('Range'))
        if status == 200 and (_is_playlist(headers, final) or body.lstrip().startswith(b'#EXTM3U')):
            text = body.decode('utf-8', 'replace')
            if '#EXTINF' in text:
                urls = [urllib.parse.urljoin(final, l.strip()) for l in text.splitlines() if l.strip() and not l.startswith('#')]
                _remember_order(urls)
                for first in urls[:PREFETCH_AHEAD]:
                    _prefetch(first)
            self.send_bytes(200, rewrite_playlist(text, final).encode(), 'application/vnd.apple.mpegurl')
            return
        keep = {k: v for k, v in headers.items() if k in ('Content-Range', 'Accept-Ranges')}
        self.head(status, headers.get('Content-Type', 'application/octet-stream'), len(body), extra=keep)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the TV skipped ahead or closed the video

    def within_limit(self, kind: str) -> bool:
        if self.local() or allowed(f'{kind}:{self.client_address[0]}', HLS_PER_MINUTE if kind == 'hls' else API_PER_MINUTE):
            return True
        self.reply(429, {'ok': False, 'error': 'Too many requests; wait a minute.'})
        return False

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path in ('/', '/tv'):
            if self.within_limit('api'):
                self.send_bytes(200, page_html(), 'text/html; charset=utf-8', csp=PAGE_CSP)
            return
        if path.startswith('/assets/hometube/'):
            name = path[len('/assets/hometube/'):]
            if name not in STATIC:
                self.reply(404, {'ok': False, 'error': 'not found'})
                return
            q = urllib.parse.parse_qs(url.query)
            fixed = q.get('v', [''])[0] == ASSET_VERSIONS.get(name)
            self.send_bytes(200, (WEB_DIR / name).read_bytes(), STATIC[name],
                            cache='public, max-age=31536000, immutable' if fixed else 'public, max-age=300')
            return
        if not path.startswith(API):
            self.reply(404, {'ok': False, 'error': 'not found'})
            return
        name = path[len(API):]
        if not self.within_limit('hls' if name in ('stream', 'hls') else 'api'):
            return
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if name == 'search':
                # the home page's rows change slowly: keep them an hour
                self.reply(200, {'ok': True, 'items': search(q.get('q', ''), ttl=3600 if q.get('topic') == '1' else 600, background=q.get('topic') == '1')})
            elif name == 'status':
                if not self.local():
                    self.reply(403, {'ok': False, 'error': 'status is shown on the board only'})
                    return
                ok, why = route_status(force=True)
                self.reply(200, {'ok': True, 'route_dev': CONFIG['route_dev'] or None, 'route_ok': ok, 'route_problem': why or None,
                                 'youtube_saw': _exit_seen['ip'], 'youtube_saw_at': _exit_seen['at'] or None,
                                 'lookup_ok_at': _youtube['ok_at'] or None, 'youtube_refused_at': _youtube['refused_at'] or None})
            elif name == 'probe':
                if not self.local():
                    self.reply(403, {'ok': False, 'error': 'the probe runs on the board only'})
                    return
                self.reply(200, probe())
            elif name == 'config':
                self.reply(200, {'ok': True, 'label': CONFIG['label'], 'rows': CONFIG['rows']})
            elif name == 'video':
                v = video(q.get('id', ''))
                # the TV gets the relay's playlist, never the googlevideo address (which it cannot read)
                self.reply(200, {'ok': True, **v, 'hls': f"{API}stream?id={v['id']}" if v.get('hls') else None})
            elif name == 'warm':
                self.reply(200, {'ok': True, 'state': warm(q.get('id', ''))})
            elif name == 'playlist':
                self.reply(200, {'ok': True, **playlist(q.get('list', ''), q.get('v', ''))})
            elif name == 'related':
                self.reply(200, {'ok': True, 'items': related(q.get('id', ''))})
            elif name == 'stream':
                self.send_bytes(200, stream_master(q.get('id', '')).encode(), 'application/vnd.apple.mpegurl')
            elif name == 'hls':
                self.relay(relay_target(q.get('u', ''), q.get('s', '')))
            elif name == 'channel':
                self.reply(200, {'ok': True, 'items': channel(q.get('id', ''))})
            elif name == 'home':
                self.reply(200, {'ok': True, 'items': home(), 'subscriptions': subscriptions()})
            elif name in ('history', 'later'):
                self.reply(200, {'ok': True, 'items': get_list(name)})
            elif name == 'account':
                self.reply(200, {'ok': True, 'connected': COOKIES.exists()})
            elif name == 'feed':
                self.reply(200, {'ok': True, 'items': account_feed(q.get('name', ''))})
            elif name == 'captions':
                self.send_bytes(200, captions(q.get('id', ''), q.get('lang', ''), q.get('auto') == '1').encode(), 'text/vtt; charset=utf-8')
            else:
                self.reply(404, {'ok': False, 'error': 'not found'})
        except ValueError as exc:
            self.reply(400, {'ok': False, 'error': str(exc)[:300]})
        except Exception as exc:
            self.reply(502, {'ok': False, 'error': str(exc)[:300]})

    def do_POST(self) -> None:
        url = urllib.parse.urlparse(self.path)
        name = url.path[len(API):] if url.path.startswith(API) else ''
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host', ''):
            self.reply(403, {'ok': False, 'error': 'Cross-origin request rejected.'})
            return
        if not str(self.headers.get('Content-Type', '')).startswith('application/json'):
            self.reply(415, {'ok': False, 'error': 'Send JSON.'})
            return
        if not self.within_limit('api'):
            return
        try:
            length = min(int(self.headers.get('Content-Length', '0')), 512_000)
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('expected a JSON object')
            if name == 'subscribe':
                self.reply(200, {'ok': True, 'subscriptions': subscribe(data.get('id', ''), data.get('name', ''), bool(data.get('add', True)))})
            elif name in ('history', 'later'):
                self.reply(200, {'ok': True, 'items': put_item(name, data.get('item') or {}, bool(data.get('remove')))})
            elif name == 'takeout':
                self.reply(200, {'ok': True, 'subscriptions': import_takeout(str(data.get('csv', ''))[:200_000])})
            elif name == 'cookies':
                # the YouTube account: only from this board (a portal that checked its administrator, or the shell)
                if not self.local():
                    self.reply(403, {'ok': False, 'error': 'Connect the account on the board: sudo hometube cookies FILE'})
                    return
                self.reply(200, {'ok': True, **(clear_cookies() if data.get('clear') else set_cookies(str(data.get('text', ''))[:400_000]))})
            else:
                self.reply(404, {'ok': False, 'error': 'not found'})
        except Exception as exc:
            self.reply(400, {'ok': False, 'error': str(exc)[:300]})


class Server(ThreadingHTTPServer):
    """At most 64 requests at once, so a flood from the network cannot use up the board."""
    daemon_threads = True
    _slots = threading.BoundedSemaphore(64)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=keep_rows_warm, daemon=True).start()
    if ':' in CONFIG['bind']:
        Server.address_family = __import__('socket').AF_INET6
    Server((CONFIG['bind'], PORT), Handler).serve_forever()


def _write_unit(path: Path, text: str) -> None:
    tmp = path.with_name('.' + path.name + '.new')
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def unit_files(uid: int) -> dict[str, str]:
    """The service, its daily yt-dlp update and the timer, from the settings."""
    table, after = CONFIG['route_table'], CONFIG['after_unit']
    pin = ''
    if table is not None:
        pref = CONFIG['route_pref']
        pin = ('# the account\'s traffic may only use this table (give the table an unreachable fallback to fail closed)\n'
               f'ExecStartPre=+/bin/sh -c \'ip -4 rule show | grep -q "uidrange {uid}-{uid} lookup {table}" || ip -4 rule add pref {pref} uidrange {uid}-{uid} lookup {table}\'\n')
    # with a routing table the rules are IPv4: no IPv6 sockets at all, so nothing can go around the table
    families = 'AF_UNIX AF_INET' if table is not None else 'AF_UNIX AF_INET AF_INET6'
    if CONFIG['route_dev']:
        families += ' AF_NETLINK'   # the route check asks the kernel (ip route get)
    state = 'StateDirectory=hometube\n' if STATE == Path('/var/lib/hometube') else f'ReadWritePaths={STATE}\n'
    service = f"""[Unit]
Description=HomeTube: YouTube for the TV
After=network-online.target{(' ' + after) if after else ''}
Wants=network-online.target

[Service]
User={USER}
Group={USER}
{pin}Environment=HOMETUBE_CONFIG={CONFIG_PATH}
ExecStart={APP_DIR}/venv/bin/python -I {APP_DIR}/hometube.py serve
{state}Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies={families}
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryMax=768M
Nice=5

[Install]
WantedBy=multi-user.target
"""
    update = f"""[Unit]
Description=Update yt-dlp for HomeTube

[Service]
Type=oneshot
ExecStart={APP_DIR}/venv/bin/pip install --quiet --upgrade "yt-dlp[default]"
ExecStartPost=-/usr/bin/systemctl try-restart {UNIT}.service
Nice=15
"""
    timer = """[Unit]
Description=Daily yt-dlp update for HomeTube

[Timer]
OnCalendar=*-*-* 05:10
RandomizedDelaySec=40min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {f'{UNIT}.service': service, f'{UNIT}-update.service': update, f'{UNIT}-update.timer': timer}


def install_units() -> None:
    if os.geteuid() != 0:
        raise SystemExit('run as root')
    if subprocess.run(['getent', 'passwd', USER], capture_output=True).returncode != 0:
        subprocess.run(['useradd', '--system', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', USER], check=True)
    uid = int(subprocess.run(['id', '-u', USER], capture_output=True, text=True, check=True).stdout)
    if STATE != Path('/var/lib/hometube'):
        STATE.mkdir(parents=True, exist_ok=True)
        os.chown(STATE, uid, int(subprocess.run(['id', '-g', USER], capture_output=True, text=True, check=True).stdout))
    for name, text in unit_files(uid).items():
        _write_unit(Path('/etc/systemd/system') / name, text)
    os.sync()
    # a rule left from an earlier table would keep this account's traffic going that way: clear them
    # all; the service puts back the one it should have when it starts
    for _ in range(20):
        if subprocess.run(['ip', '-4', 'rule', 'del', 'uidrange', f'{uid}-{uid}'], capture_output=True).returncode != 0:
            break
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', f'{UNIT}.service', f'{UNIT}-update.timer'], check=True)
    subprocess.run(['systemctl', 'restart', f'{UNIT}.service'], check=True)
    subprocess.run(['systemctl', 'start', f'{UNIT}-update.timer'], check=True)
    where = f"http://{CONFIG['bind'] if CONFIG['bind'] not in ('0.0.0.0', '::') else '<this board>'}:{PORT}/"
    print(f'HomeTube running: {where}' + (f" (its traffic only through table {CONFIG['route_table']})" if CONFIG['route_table'] is not None else ''))


def cookies_command(args: list[str]) -> None:
    """Connect a YouTube account from a cookies.txt exported while signed in, or --clear it."""
    if os.geteuid() != 0:
        raise SystemExit('run as root')
    if args[:1] == ['--clear']:
        clear_cookies()
        print('YouTube account disconnected.')
        return
    if len(args) != 1:
        raise SystemExit('usage: hometube.py cookies FILE | --clear')
    result = set_cookies(Path(args[0]).read_text(errors='replace'))
    uid = int(subprocess.run(['id', '-u', USER], capture_output=True, text=True, check=True).stdout)
    gid = int(subprocess.run(['id', '-g', USER], capture_output=True, text=True, check=True).stdout)
    os.chown(COOKIES, uid, gid)
    subprocess.run(['systemctl', 'try-restart', f'{UNIT}.service'], check=False)
    print(f"YouTube account connected ({result['cookies']} cookies kept).")


if __name__ == '__main__':
    command, rest = (sys.argv[1:] or [''])[0], sys.argv[2:]
    if command == 'serve':
        serve()
    elif command == 'install-units':
        install_units()
    elif command == 'cookies':
        cookies_command(rest)
    else:
        raise SystemExit(__doc__)
