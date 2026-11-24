"""HomeTube's own VPN is optional: off by default, and when someone imports their provider's WireGuard
configs (.conf or .zip, any provider) HomeTube gets a tunnel of its own on the fastest server that works."""
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import nonet  # noqa: F401  (no network in the tests, see nonet.py)
# the tests run on the defaults, never on the settings of the machine they run on
os.environ['HOMETUBE_CONFIG'] = '/nonexistent/hometube-tests.json'
import hometube as ht

PRIV = 'cGVlcmtleWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE='
PUB = 'cGVlcmtleWJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI='
PSK = 'cGVlcmtleWNjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M='


def conf(endpoint='185.213.154.68:51820', dns='DNS = 10.2.0.1', extra=''):
    return (f'[Interface]\n# a provider comment\nPrivateKey = {PRIV}\nAddress = 10.2.0.2/32, fd00::2/128\n{dns}\n\n'
            f'[Peer]\nPublicKey = {PUB}\n{extra}AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {endpoint}\n')


class ParseTests(unittest.TestCase):
    def test_any_providers_config(self):
        c = ht.parse_wg(conf())
        self.assertEqual((c['address'], c['dns'], c['host'], c['port'], c['psk']), ('10.2.0.2/32', ['10.2.0.1'], '185.213.154.68', 51820, ''))
        self.assertEqual(ht.parse_wg(conf(extra=f'PresharedKey = {PSK}\n'))['psk'], PSK)   # own servers often use one
        self.assertEqual(ht.parse_wg(conf(endpoint='ch-12.vpn.example.net:51820'))['host'], 'ch-12.vpn.example.net')
        self.assertEqual(ht.parse_wg(conf(dns=''))['dns'], [])

    def test_unusable_configs_are_refused(self):
        for bad in ('', 'not a config', conf().replace(PRIV, 'short'), conf(endpoint='nowhere'),
                    conf() + f'\n[Peer]\nPublicKey = {PUB}\nEndpoint = 1.2.3.4:1\n', conf(extra='PresharedKey = bad\n')):
            self.assertIsNone(ht.parse_wg(bad), bad[:40])

    def test_a_zip_of_many_servers_and_files_that_are_not(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('se-sto-wg-001.conf', conf())
            z.writestr('named/ch-zrh.conf', conf(endpoint='zrh.vpn.example.net:51820'))
            z.writestr('readme.txt', 'hello')
            z.writestr('broken.conf', 'nope')
        with mock.patch.object(ht, '_resolve', side_effect=lambda h: '198.51.100.9' if 'example' in h else h):
            servers, skipped = ht.vpn_read_configs(buf.getvalue(), 'mullvad_wireguard_linux.zip')
        self.assertEqual(sorted(s['name'] for s in servers), ['ch-zrh', 'se-sto-wg-001'])
        self.assertEqual(skipped, 1)                      # broken.conf; readme.txt is not a .conf at all
        self.assertEqual(next(s for s in servers if s['name'] == 'ch-zrh')['ip'], '198.51.100.9')
        with mock.patch.object(ht, '_resolve', return_value='185.213.154.68'):
            one, _ = ht.vpn_read_configs(conf().encode(), '/home/me/my server.conf')
        self.assertEqual(one[0]['name'], 'my-server')


class TunnelTests(unittest.TestCase):
    def test_the_tunnel_only_routes_its_own_table_and_fails_closed(self):
        server = dict(ht.parse_wg(conf(extra=f'PresharedKey = {PSK}\n')), name='x', ip='185.213.154.68')
        text = ht.vpn_conf(server)
        self.assertIn('Table = off', text)
        self.assertIn(f'default dev %i table {ht.VPN_TABLE}', text)
        self.assertIn(f'unreachable default metric 4096 table {ht.VPN_TABLE}', text)
        self.assertIn(f'PresharedKey = {PSK}', text)
        self.assertNotIn('DNS =', text)                   # never changes the whole board's resolver
        self.assertIn('Endpoint = 185.213.154.68:51820', text)

    def test_with_its_own_vpn_lookups_use_the_tunnels_resolvers(self):
        with mock.patch.dict(ht.CONFIG, {'route_table': ht.VPN_TABLE, 'route_dev': ht.VPN_IFACE, 'resolvers': ['10.2.0.1'], 'after_unit': ''}):
            svc = ht.unit_files(990)['hometube.service']
        self.assertIn(f'BindReadOnlyPaths={ht.VPN_RESOLV}:/etc/resolv.conf {ht.VPN_NSSWITCH}:/etc/nsswitch.conf', svc)
        self.assertIn(f'lookup {ht.VPN_TABLE}', svc)
        with mock.patch.dict(ht.CONFIG, {'route_table': None, 'route_dev': '', 'resolvers': []}):
            self.assertNotIn('BindReadOnlyPaths', ht.unit_files(990)['hometube.service'])   # off: nothing changes

    def test_names_are_looked_up_by_dns_only_inside(self):
        with mock.patch.object(Path, 'read_text', return_value='passwd: files systemd\nhosts: files mdns4_minimal resolve [!UNAVAIL=return] dns\n'):
            out = ht._nsswitch_files_dns()
        self.assertIn('passwd: files systemd', out)
        self.assertIn('hosts: files dns', out)
        self.assertNotIn('resolve', out)

    def test_settings_accept_resolvers_and_refuse_nonsense(self):
        path = Path(tempfile.mkdtemp()) / 'c.json'
        try:
            path.write_text(json.dumps({'resolvers': ['10.2.0.1', '1.1.1.1']}))
            self.assertEqual(ht.load_config(path)['resolvers'], ['10.2.0.1', '1.1.1.1'])
            for bad in (['example.com'], ['1.1.1.1'] * 4, 'x'):
                path.write_text(json.dumps({'resolvers': bad}))
                with self.assertRaises(ValueError):
                    ht.load_config(path)
            self.assertEqual(ht.load_config(Path('/nonexistent/x.json'))['resolvers'], [])   # off by default
        finally:
            shutil.rmtree(path.parent)


class PickerTests(unittest.TestCase):
    """The fastest server that works; a working one moves only for a clear gain; one that stops moves."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(ht, 'VPN_DIR', self.tmp), mock.patch.object(ht, 'VPN_STATE', self.tmp / 'state.json'),
                        mock.patch.object(ht, 'VPN_SERVERS', self.tmp / 'servers.json'), mock.patch.object(ht, 'VPN_CONF', self.tmp / 'htvpn.conf')]
        for p in self.patches:
            p.start()
        self.servers = [{'name': n, 'ip': '185.213.154.68', 'port': 51820, 'dns': ['10.2.0.1']} for n in ('a', 'b', 'c')]
        self.latency = {'a': 50.0, 'b': 60.0, 'c': 200.0}
        self.up = []
        self.dead = set()
        self.fakes = [mock.patch.object(ht, 'vpn_ranked', side_effect=lambda servers, skip: sorted(
                          ((self.latency[s['name']], s) for s in servers if s['name'] not in skip), key=lambda x: x[0])),
                      mock.patch.object(ht, '_vpn_up', side_effect=lambda s: (self.up.append(s['name']) or s['name'] not in self.dead))]
        for f in self.fakes:
            f.start()

    def tearDown(self):
        for f in self.fakes + self.patches:
            f.stop()
        shutil.rmtree(self.tmp)

    def test_import_takes_the_fastest_that_answers(self):
        self.dead = {'a'}
        moved = ht.vpn_move(self.servers, None, 'import', must_move=True)
        self.assertEqual((moved['server'], self.up), ('b', ['a', 'b']))
        self.assertIn('a', ht._read_vpn(ht.VPN_STATE, {})['avoid'])

    def test_a_working_tunnel_moves_only_for_a_clear_gain(self):
        self.assertIsNone(ht.vpn_move(self.servers, 'b', 'faster', must_move=False))   # 50 vs 60: not worth it
        self.assertEqual(self.up, [])
        self.assertEqual(ht.vpn_move(self.servers, 'c', 'faster', must_move=False)['server'], 'a')   # 200 -> 50

    def test_a_dead_one_moves_to_another(self):
        moved = ht.vpn_move(self.servers, 'a', 'dead', must_move=True)
        self.assertEqual(moved['server'], 'b')

    def test_youtube_refusing_a_server_tries_the_next(self):
        answers = iter([{'lookup': 'refused'}, {'lookup': 'ok'}])
        with mock.patch.object(ht.urllib.request, 'urlopen', side_effect=lambda url, timeout=0: mock.Mock(read=lambda: json.dumps(next(answers)).encode())):
            moved = ht.vpn_move(self.servers, 'c', 'youtube', must_move=True)
        self.assertEqual(moved['server'], 'b')
        self.assertIn('a', ht._read_vpn(ht.VPN_STATE, {})['youtube_avoid'])

    def test_nothing_answering_keeps_it_closed_not_around(self):
        self.dead = {'a', 'b', 'c'}
        self.assertIsNone(ht.vpn_move(self.servers, 'c', 'dead', must_move=True))

    def test_the_minute_check_moves_after_three_misses(self):
        ht.VPN_CONF.write_text('x')
        ht._write_vpn(ht.VPN_SERVERS, self.servers)
        ht._write_vpn(ht.VPN_STATE, {'current': 'a', 'measured_at': int(time.time())})
        with mock.patch.dict(ht.CONFIG, {'route_dev': ht.VPN_IFACE, 'resolvers': ['10.2.0.1']}), \
             mock.patch.object(ht, 'vpn_answers', return_value=False), \
             mock.patch.object(ht.urllib.request, 'urlopen', side_effect=OSError('not running')), \
             mock.patch.object(ht, '_set_vpn_settings') as settings:
            self.assertIsNone(ht.vpn_check()); self.assertIsNone(ht.vpn_check())
            self.assertIn('moved to b', ht.vpn_check())
        settings.assert_not_called()                        # same resolvers: HomeTube is not restarted

    def test_off_means_nothing_runs(self):
        with mock.patch.dict(ht.CONFIG, {'route_dev': ''}), mock.patch.object(ht, 'vpn_answers', side_effect=AssertionError('no probing when off')):
            self.assertIsNone(ht.vpn_check())


if __name__ == '__main__':
    unittest.main()
