"""The web side and the safety rules: settings, the page and its files, who may change what,
rate limits, no Shorts, the systemd units and the tunnel route guard (fails closed)."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import nonet  # noqa: F401  (no network in the tests, see nonet.py)
# the tests run on the defaults, never on the settings of the machine they run on
os.environ['HOMETUBE_CONFIG'] = '/nonexistent/hometube-tests.json'
import hometube as ht


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(ht, 'STATE', self.tmp), mock.patch.object(ht, 'COOKIES', self.tmp / 'cookies.txt')]
        for p in self.patches:
            p.start()
        ht._cache.clear()
        ht._hits.clear()
        self.server = ht.Server(('127.0.0.1', 0), ht.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def get(self, path, headers=None):
        return opener().open(urllib.request.Request(self.base + path, headers=headers or {}), timeout=10)

    def post(self, path, payload, headers=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        return opener().open(urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), headers=h, method='POST'), timeout=10)

    def status_of(self, call):
        try:
            return call().status
        except urllib.error.HTTPError as exc:
            return exc.code


class SettingsTests(unittest.TestCase):
    def load(self, data):
        path = Path(tempfile.mkdtemp()) / 'config.json'
        path.write_text(json.dumps(data))
        try:
            return ht.load_config(path)
        finally:
            shutil.rmtree(path.parent)

    def test_defaults_without_a_file(self):
        cfg = ht.load_config(Path('/nonexistent/hometube.json'))
        self.assertEqual((cfg['bind'], cfg['port'], cfg['route_table'], cfg['route_dev']), ('127.0.0.1', 8891, None, ''))
        self.assertTrue(cfg['rows'])

    def test_good_settings_load(self):
        cfg = self.load({'bind': '0.0.0.0', 'port': 8890, 'route_table': 200, 'route_dev': 'wg0', 'after_unit': 'wg-quick@wg0.service',
                         'label': 'Living room', 'rows': [['Jazz', 'jazz live']]})
        self.assertEqual((cfg['route_table'], cfg['route_dev'], cfg['rows'][0][0]), (200, 'wg0', 'Jazz'))

    def test_bad_settings_are_refused(self):
        for bad in ({'bind': 'everywhere'}, {'port': 0}, {'port': '8890'}, {'route_table': 'vpn'}, {'route_dev': 'wg0; rm -rf /'},
                    {'after_unit': 'x y'}, {'label': '<script>'}, {'rows': [['only title']]}, {'user': 'Root User'},
                    {'state_dir': 'relative/path'}, {'nonsense': 1}):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)


class PageTests(ServerCase):
    def test_page_title_label_and_policy(self):
        with mock.patch.dict(ht.CONFIG, {'label': 'Living room'}):
            r = self.get('/tv')
            body = r.read().decode()
        self.assertIn('<title>HomeTube · Living room</title>', body)
        self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertIn('/assets/hometube/hometube.js', body)
        self.assertEqual(self.get('/').status, 200)

    def test_files_have_versioned_addresses_cached_for_a_year(self):
        body = self.get('/tv').read().decode()
        v = ht.ASSET_VERSIONS['hometube.js']
        self.assertIn(f'/assets/hometube/hometube.js?v={v}"', body)
        self.assertIn(f'data-hls="/assets/hometube/hls.min.js?v={ht.ASSET_VERSIONS["hls.min.js"]}"', body)   # loaded on first play
        self.assertIn('immutable', self.get(f'/assets/hometube/hometube.js?v={v}').headers['Cache-Control'])
        self.assertEqual(self.get('/assets/hometube/hometube.js?v=old').headers['Cache-Control'], 'public, max-age=300')

    def test_only_its_own_files_are_served(self):
        r = self.get('/assets/hometube/hometube.css')
        self.assertTrue(r.headers['Content-Type'].startswith('text/css'))
        for path in ('/assets/hometube/../hometube.py', '/assets/hometube/index.html', '/assets/hometube/hls.LICENSE',
                     '/etc/passwd', '/api/tv/nothing', '/api/tv/diag'):
            self.assertEqual(self.status_of(lambda: self.get(path)), 404, path)

    def test_config_lists_the_rows(self):
        with mock.patch.dict(ht.CONFIG, {'rows': [['Chill', 'lofi']]}):
            d = json.loads(self.get('/api/tv/config').read())
        self.assertEqual(d['rows'], [['Chill', 'lofi']])

    def test_bad_input_is_a_bad_request(self):
        self.assertEqual(self.status_of(lambda: self.get('/api/tv/video?id=../../etc')), 400)
        self.assertEqual(self.status_of(lambda: self.get('/api/tv/playlist?list=nope')), 400)


class WhoMayChangeWhatTests(ServerCase):
    def test_cookies_only_from_this_board(self):
        good = '\n'.join(['# Netscape HTTP Cookie File', '.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc'])
        with mock.patch.object(ht.Handler, 'local', return_value=False):
            self.assertEqual(self.status_of(lambda: self.post('/api/tv/cookies', {'text': good})), 403)
        self.assertFalse((self.tmp / 'cookies.txt').exists())
        self.assertEqual(self.post('/api/tv/cookies', {'text': good}).status, 200)   # the portal on this board, after its sign-in
        self.assertTrue((self.tmp / 'cookies.txt').exists())
        self.assertEqual((self.tmp / 'cookies.txt').stat().st_mode & 0o777, 0o600)

    def test_status_only_from_this_board(self):
        with mock.patch.object(ht.Handler, 'local', return_value=False):
            self.assertEqual(self.status_of(lambda: self.get('/api/tv/status')), 403)
        self.assertEqual(json.loads(self.get('/api/tv/status').read())['route_ok'], True)

    def test_probe_and_refusals_are_for_this_board(self):
        with mock.patch.object(ht.Handler, 'local', return_value=False):
            self.assertEqual(self.status_of(lambda: self.get('/api/tv/probe')), 403)
        refused = RuntimeError('ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot. Use --cookies')
        with mock.patch.object(ht, '_ytdlp', side_effect=refused):
            self.assertEqual(json.loads(self.get('/api/tv/probe').read())['lookup'], 'refused')
        with mock.patch.object(ht, '_ytdlp', return_value={'formats': [{'manifest_url': 'https://manifest.googlevideo.com/api/manifest/hls_variant/ip/198.51.100.9/id/x'}]}):
            d = json.loads(self.get('/api/tv/probe').read())
        self.assertEqual((d['lookup'], d['youtube_saw']), ('ok', '198.51.100.9'))

    def test_a_refusal_from_youtube_is_recorded(self):
        ht._youtube.update(ok_at=0, refused_at=0)
        bad = subprocess.CompletedProcess([], 1, '', 'ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot.')
        with mock.patch.object(ht.subprocess, 'run', return_value=bad):
            with self.assertRaises(RuntimeError):
                ht._ytdlp(['x'])
        self.assertTrue(ht._youtube['refused_at'])
        status = json.loads(self.get('/api/tv/status').read())
        self.assertEqual(status['youtube_refused_at'], ht._youtube['refused_at'])

    def test_cross_origin_and_non_json_posts_are_refused(self):
        item = {'item': {'id': 'jNQXAC9IVRw', 'title': 'zoo'}}
        self.assertEqual(self.status_of(lambda: self.post('/api/tv/later', item, {'Origin': 'http://evil.example'})), 403)
        req = urllib.request.Request(self.base + '/api/tv/later', data=b'id=x', headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        self.assertEqual(self.status_of(lambda: opener().open(req, timeout=10)), 415)
        host = self.base.split('//')[1]
        self.assertEqual(self.post('/api/tv/later', item, {'Origin': f'http://{host}'}).status, 200)
        self.assertEqual(ht.get_list('later')[0]['id'], 'jNQXAC9IVRw')

    def test_rate_limit_for_the_network_not_for_this_board(self):
        with mock.patch.object(ht, 'API_PER_MINUTE', 3):
            for _ in range(5):
                self.assertEqual(self.get('/api/tv/config').status, 200)   # from this board: no limit
            with mock.patch.object(ht.Handler, 'local', return_value=False):
                codes = [self.status_of(lambda: self.get('/api/tv/config')) for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])


class NoShortsTests(unittest.TestCase):
    def setUp(self):
        ht._cache.clear()

    def test_search_leaves_shorts_out(self):
        def vr(vid, **nav):
            return {'videoRenderer': {'videoId': vid, 'title': {'runs': [{'text': vid}]}, 'navigationEndpoint': nav,
                                      'ownerText': {'runs': [{'text': 'c'}]}}}
        page = {'contents': [vr('aaaaaaaaaaa', commandMetadata={'webCommandMetadata': {'url': '/watch?v=aaaaaaaaaaa'}}),
                             vr('bbbbbbbbbbb', reelWatchEndpoint={'videoId': 'bbbbbbbbbbb'}),
                             vr('ccccccccccc', commandMetadata={'webCommandMetadata': {'url': '/shorts/ccccccccccc'}})]}
        with mock.patch.object(ht, '_https', return_value=json.dumps(page).encode()):
            self.assertEqual([v['id'] for v in ht.search_direct('x', 10)], ['aaaaaaaaaaa'])

    def test_channel_feed_leaves_shorts_out(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><title>Chan</title>
          <entry><yt:videoId>aaaaaaaaaaa</yt:videoId><title>long one</title><link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/></entry>
          <entry><yt:videoId>bbbbbbbbbbb</yt:videoId><title>a short</title><link rel="alternate" href="https://www.youtube.com/shorts/bbbbbbbbbbb"/></entry></feed>'''
        with mock.patch.object(ht.urllib.request, 'urlopen', return_value=mock.Mock(read=lambda n: feed)):
            self.assertEqual([v['id'] for v in ht.channel('UC4QobU6STFB0P71PMvOGN5A')], ['aaaaaaaaaaa'])


class UnitTests(unittest.TestCase):
    def test_plain_install_has_no_routing_rule(self):
        with mock.patch.dict(ht.CONFIG, {'route_table': None, 'route_dev': '', 'after_unit': ''}):
            svc = ht.unit_files(990)['hometube.service']
        self.assertNotIn('ip -4 rule', svc)
        self.assertIn('RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n', svc)
        self.assertIn('ProtectSystem=strict', svc)
        self.assertIn('/hometube.py serve', svc)

    def test_tunnel_install_pins_the_account_and_drops_ipv6(self):
        with mock.patch.dict(ht.CONFIG, {'route_table': 200, 'route_pref': 91, 'route_dev': 'wg0', 'after_unit': 'wg-quick@wg0.service'}):
            units = ht.unit_files(990)
        svc = units['hometube.service']
        self.assertIn('ip -4 rule add pref 91 uidrange 990-990 lookup 200', svc)
        self.assertIn('RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK\n', svc)   # no IPv6: the rule is IPv4
        self.assertIn('After=network-online.target wg-quick@wg0.service', svc)
        self.assertIn('Persistent=true', units['hometube-update.timer'])


class RouteGuardTests(unittest.TestCase):
    """With route_dev set, nothing goes to YouTube unless the kernel says this account's traffic
    leaves by that interface."""
    def setUp(self):
        ht._route.update(at=0.0, ok=True, why='')
        self.p = mock.patch.dict(ht.CONFIG, {'route_dev': 'wg0'})
        self.p.start()

    def tearDown(self):
        self.p.stop()
        ht._route.update(at=0.0, ok=True, why='')

    def route(self, stdout, stderr='', code=0):
        return mock.patch.object(ht.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, stderr))

    def test_through_the_tunnel_is_allowed(self):
        with self.route('142.250.0.1 dev wg0 table 200 src 10.64.0.2 uid 990\n    cache\n') as run:
            ht._require_route()
        self.assertIn('uid', run.call_args[0][0])

    def test_any_other_way_out_is_refused_before_sending(self):
        with self.route('142.250.0.1 via 192.168.1.1 dev eth0 src 192.168.1.5 uid 990\n'):
            with self.assertRaises(RuntimeError) as err, mock.patch.object(ht.http.client, 'HTTPSConnection') as conn:
                ht._https('www.youtube.com', 'GET', '/')
        self.assertIn('eth0', str(err.exception))
        conn.assert_not_called()   # nothing was sent

    def test_no_route_at_all_is_refused(self):
        ht._route.update(at=0.0)
        with self.route('', 'RTNETLINK answers: Network is unreachable', 2):
            ok, why = ht.route_status(force=True)
        self.assertFalse(ok)
        self.assertIn('unreachable', why)

    def test_relay_and_lookups_are_guarded_too(self):
        with self.route('142.250.0.1 via 192.168.1.1 dev eth0\n'):
            for call in (lambda: ht._stream_get('https://rr1---sn-x.googlevideo.com/videoplayback?x=1'),
                         lambda: ht._ytdlp(['https://www.youtube.com/watch?v=jNQXAC9IVRw'])):
                with self.assertRaises(RuntimeError):
                    call()

    def test_the_address_youtube_saw_is_kept(self):
        ht._note_exit('https://manifest.googlevideo.com/api/manifest/hls_variant/expire/1/ip/198.51.100.7/id/x')
        self.assertEqual(ht._exit_seen['ip'], '198.51.100.7')


class WarmRowsTests(unittest.TestCase):
    def test_rows_are_refreshed_without_waiting_for_the_cache_to_run_out(self):
        ht._cache.clear()
        ht._cache['s:lofi:24'] = (0, ['old'])
        calls = []
        def fake_make(query, count):
            calls.append(query); return [{'id': 'aaaaaaaaaaa'}]
        class Stop(Exception):
            pass
        with mock.patch.dict(ht.CONFIG, {'rows': [['Chill', 'lofi']]}), mock.patch.object(ht, 'search_direct', side_effect=fake_make), \
             mock.patch.object(ht.time, 'sleep', side_effect=[None, Stop()]):
            with self.assertRaises(Stop):
                ht.keep_rows_warm()
        self.assertEqual(calls, ['lofi'])
        self.assertEqual(ht._cache['s:lofi:24'][1], [{'id': 'aaaaaaaaaaa'}])   # replaced, fresh


if __name__ == '__main__':
    unittest.main()
