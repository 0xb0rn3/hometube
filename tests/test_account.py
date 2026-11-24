"""The YouTube account can be flagged or locked when Google sees it from a new country every time a VPN
moves. So HomeTube's own lookups (videos, search, rows) never carry it; only the account service holds the
cookies, and it fetches the account's lists by one fixed way out: the board's own connection (default),
or the tunnel pinned to one server address, checked with YouTube's own report before every request."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import nonet  # noqa: F401  (no network in the tests, see nonet.py)
os.environ['HOMETUBE_CONFIG'] = '/nonexistent/hometube-tests.json'
import hometube as ht

GOOD = '\n'.join(['# Netscape HTTP Cookie File', '.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc'])
FEED = json.dumps({'entries': [{'id': 'jNQXAC9IVRw', 'title': 'Me at the zoo', 'channel': 'jawed', 'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw'}]})


class AccountCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        acct = self.tmp / 'acct'
        self.patches = [mock.patch.object(ht, 'STATE', self.tmp), mock.patch.object(ht, 'ACCOUNT_DIR', acct),
                        mock.patch.object(ht, 'COOKIES', acct / 'cookies.txt'), mock.patch.object(ht, 'ACCOUNT_STATE', acct / 'account.json')]
        for p in self.patches:
            p.start()
        self.calls = []

        def run(cmd, **kw):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, FEED, '')
        self.run = mock.patch.object(ht.subprocess, 'run', side_effect=run)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def fetch(self, sees, cfg):
        with mock.patch.dict(ht.CONFIG, cfg), mock.patch.object(ht, 'youtube_sees', return_value=sees), self.run, \
             mock.patch.object(ht, 'route_status', return_value=(True, '')):
            return ht.account_fetch('subs')


class NeverOnVideosTests(AccountCase):
    def test_hometubes_own_lookups_never_carry_the_account(self):
        ht.account_set(GOOD)
        self.assertTrue(ht.COOKIES.exists())
        with self.run, mock.patch.dict(ht.CONFIG, {'route_dev': ''}):
            ht._cache.clear()
            ht.search('zoo')
        self.assertTrue(self.calls)
        self.assertFalse(any('--cookies' in c for c in self.calls))
        self.assertNotIn('cookies', ht.unit_files(990, 991)['hometube.service'])


class WayOutTests(AccountCase):
    TUNNEL = {'account_via': 'tunnel', 'route_table': 201, 'route_dev': 'mullvad2'}

    def test_home_is_the_default_and_goes_without_the_tunnel(self):
        ht.account_set(GOOD)
        with mock.patch.dict(ht.CONFIG, {'route_table': 201, 'route_dev': 'mullvad2'}):
            self.assertEqual(ht.account_via(), 'home')
            with mock.patch.object(ht, '_require_route', side_effect=AssertionError('home does not use the tunnel check')), \
                 mock.patch.object(ht, 'youtube_sees', return_value='198.51.100.7'), self.run:
                items = ht.account_fetch('subs')
        self.assertEqual(items[0]['id'], 'jNQXAC9IVRw')
        self.assertIn('--cookies', self.calls[-1])
        self.assertEqual(ht.account_status()['saw'], '198.51.100.7')

    def test_tunnel_pins_the_first_address_and_pauses_on_another(self):
        ht.account_set(GOOD)
        self.fetch('203.0.113.10', self.TUNNEL)
        self.assertEqual(ht.account_status()['pin'], '203.0.113.10')
        self.fetch('203.0.113.10', self.TUNNEL)                       # same server: fine
        n = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, 'paused'):
            self.fetch('203.0.113.20', self.TUNNEL)                    # the VPN moved: nothing signed in goes out
        self.assertEqual(len(self.calls), n)
        self.assertIn('another server', ht.account_status()['paused'])
        with self.assertRaisesRegex(RuntimeError, 'paused'):
            self.fetch(None, self.TUNNEL)                              # cannot confirm the address: paused too
        self.fetch('203.0.113.10', self.TUNNEL)                       # back on its server: it resumes
        self.assertIsNone(ht.account_status()['paused'])

    def test_tunnel_needs_a_tunnel(self):
        with mock.patch.dict(ht.CONFIG, {'account_via': 'tunnel', 'route_table': None}):
            self.assertEqual(ht.account_via(), 'home')

    def test_a_new_account_gets_a_new_pin(self):
        ht.account_set(GOOD)
        self.fetch('203.0.113.10', self.TUNNEL)
        ht.account_set(GOOD)
        self.assertIsNone(ht.account_status()['pin'])

    def test_youtubes_own_report_is_read_strictly(self):
        def answer(body):
            r = mock.MagicMock()
            r.__enter__.return_value.read.return_value = body
            return mock.Mock(open=mock.Mock(return_value=r))
        for body, want in ((b'203.0.113.10 => lhr48s31 (203.0.113.0/24)\n\nDebug Info:', '203.0.113.10'),
                           (b'<html>error</html>', None), (b'', None), (b'1.2.3.4 is fine', None)):
            with mock.patch.object(ht.urllib.request, 'build_opener', return_value=answer(body)):
                self.assertEqual(ht.youtube_sees(), want, body)


class UnitTests(unittest.TestCase):
    def test_home_has_no_rule_of_its_own(self):
        with mock.patch.dict(ht.CONFIG, {'route_table': 201, 'route_dev': 'mullvad2', 'account_via': 'home', 'after_unit': ''}):
            unit = ht.unit_files(990, 991)['hometube-account.service']
        self.assertIn('User=hometube-account', unit)
        self.assertIn('Group=hometube', unit)
        self.assertNotIn('ip -4 rule', unit)
        self.assertIn('hometube.py account-serve', unit)
        self.assertIn('UMask=0077', unit)

    def test_tunnel_pins_the_account_service_like_hometube(self):
        with mock.patch.dict(ht.CONFIG, {'route_table': 201, 'route_pref': 91, 'route_dev': 'mullvad2', 'account_via': 'tunnel',
                                         'after_unit': 'wg-quick@mullvad2.service', 'resolvers': ['10.64.0.1']}):
            unit = ht.unit_files(990, 991)['hometube-account.service']
        self.assertIn('ip -4 rule add pref 91 uidrange 991-991 lookup 201', unit)
        self.assertIn('RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK\n', unit)
        self.assertIn('BindReadOnlyPaths=', unit)
        self.assertIn('After=network-online.target wg-quick@mullvad2.service', unit)

    def test_the_setting_is_checked(self):
        path = Path(tempfile.mkdtemp()) / 'c.json'
        try:
            path.write_text(json.dumps({'account_via': 'tunnel'}))
            self.assertEqual(ht.load_config(path)['account_via'], 'tunnel')
            path.write_text(json.dumps({'account_via': 'anywhere'}))
            with self.assertRaises(ValueError):
                ht.load_config(path)
            self.assertEqual(ht.load_config(Path('/nonexistent/x.json'))['account_via'], 'home')
        finally:
            shutil.rmtree(path.parent)


if __name__ == '__main__':
    unittest.main()
