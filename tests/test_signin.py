"""Videos YouTube shows only signed in (age-restricted, members only, private) are looked up with the
account, by the account's own way out (home by default), and everything of that video (playlists,
segments, captions) is fetched the same way: YouTube binds those addresses to whoever looked it up.
Nothing else ever uses the account; the "not a bot" refusal is not a reason to."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import nonet  # noqa: F401  (no network in the tests, see nonet.py)
os.environ['HOMETUBE_CONFIG'] = '/nonexistent/hometube-tests.json'
import hometube as ht

AGE = "ERROR: [youtube] aaaaaaaaaaa: Sign in to confirm your age. This video may be inappropriate for some users. Use --cookies"
BOT = "ERROR: [youtube] aaaaaaaaaaa: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
YT = {'title': 'Restricted', 'channel': 'Chan', 'formats': [{'protocol': 'm3u8_native', 'height': 1080, 'manifest_url': 'https://manifest.googlevideo.com/api/manifest/hls_variant/x'}],
      'subtitles': {'en': [{'ext': 'vtt', 'url': 'https://www.youtube.com/api/timedtext?v=aaaaaaaaaaa&lang=en', 'name': 'English'}]}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        acct = self.tmp / 'acct'
        self.patches = [mock.patch.object(ht, 'STATE', self.tmp), mock.patch.object(ht, 'ACCOUNT_DIR', acct),
                        mock.patch.object(ht, 'COOKIES', acct / 'cookies.txt'), mock.patch.object(ht, 'ACCOUNT_STATE', acct / 'account.json'),
                        mock.patch.object(ht, 'ACCOUNT_SOCK', self.tmp / 'a.sock'), mock.patch.dict(ht.CONFIG, {'route_dev': ''})]
        for p in self.patches:
            p.start()
        ht._cache.clear(); ht._account_videos.clear(); ht._caption_urls.clear()
        ht._youtube.update(ok_at=0, refused_at=0)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def serve(self):
        threading.Thread(target=ht.account_serve, args=(self.tmp / 'a.sock',), daemon=True).start()
        for _ in range(100):
            if (self.tmp / 'a.sock').exists():
                return
            time.sleep(0.02)


class LookupTests(Case):
    def anonymous_fails(self, why):
        return mock.patch.object(ht, '_ytdlp', side_effect=RuntimeError(why))

    def test_a_video_that_needs_sign_in_is_looked_up_with_the_account(self):
        answer = {'ok': True, 'video': ht._video_from(YT, 'aaaaaaaaaaa')[0], 'captions': [['en', False, YT['subtitles']['en'][0]['url']]]}
        with self.anonymous_fails(AGE), mock.patch.object(ht, 'account_call', return_value=answer) as call:
            v = ht.video('aaaaaaaaaaa')
        call.assert_called_once_with({'op': 'video', 'id': 'aaaaaaaaaaa'}, timeout=150)
        self.assertTrue(v['signed_in'])
        self.assertIn('aaaaaaaaaaa', ht._account_videos)
        self.assertEqual(ht._caption_urls['aaaaaaaaaaa'][('en', False)], YT['subtitles']['en'][0]['url'])
        self.assertEqual(ht._youtube['refused_at'], 0)            # an age check is about the video, not the server

    def test_never_ahead_of_time_and_never_for_the_bot_refusal(self):
        with self.anonymous_fails(AGE), mock.patch.object(ht, 'account_call', side_effect=AssertionError('no account for a card in focus')):
            with self.assertRaises(RuntimeError):
                ht.video('aaaaaaaaaaa', background=True)
        ht._cache.clear()
        with mock.patch.object(ht, 'account_call', side_effect=AssertionError('no account for the bot check')), \
             mock.patch.object(ht.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', BOT)):
            with self.assertRaisesRegex(RuntimeError, 'not a bot'):
                ht.video('aaaaaaaaaaa')
        self.assertGreater(ht._youtube['refused_at'], 0)          # that one is the server: the picker may move

    def test_without_an_account_it_says_what_to_do(self):
        with self.anonymous_fails(AGE), mock.patch.object(ht, 'account_call', return_value={'ok': False, 'error': 'No YouTube account is connected.'}):
            with self.assertRaisesRegex(RuntimeError, 'Connect a YouTube account on the board'):
                ht.video('aaaaaaaaaaa')

    def test_the_account_service_side_uses_the_cookies_and_its_own_way_out(self):
        ht.account_set('\n'.join(['# Netscape HTTP Cookie File', '.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tabc']))
        with mock.patch.dict(ht.CONFIG, {'route_table': 201, 'route_dev': 'mullvad2'}), mock.patch.object(ht, 'youtube_sees', return_value='198.51.100.7'), \
             mock.patch.object(ht, '_ytdlp', return_value=YT) as run:
            out = ht.account_video('aaaaaaaaaaa')
        self.assertEqual(run.call_args.kwargs['cookies'], ht.COOKIES)
        self.assertIn('youtube:player_client=web_safari', run.call_args.args[0])   # the client that gives HLS signed in
        self.assertFalse(run.call_args.kwargs['route'])          # home: not the tunnel
        self.assertEqual(out['video']['hls'], YT['formats'][0]['manifest_url'])
        with mock.patch.object(ht, 'youtube_sees', return_value='198.51.100.7'), mock.patch.object(ht, '_ytdlp', return_value={'formats': []}):
            with self.assertRaisesRegex(ValueError, 'no stream'):
                ht.account_video('aaaaaaaaaaa')                   # nothing playable: an error, not a cached dead video
        tunnel = {'route_table': 201, 'route_dev': 'mullvad2', 'account_via': 'tunnel'}
        ht._save_account_state({'pin': '203.0.113.10'})
        with mock.patch.dict(ht.CONFIG, tunnel), mock.patch.object(ht, 'youtube_sees', return_value='203.0.113.20'), \
             mock.patch.object(ht, 'route_status', return_value=(True, '')), mock.patch.object(ht, '_ytdlp', side_effect=AssertionError('paused')):
            with self.assertRaisesRegex(RuntimeError, 'paused'):
                ht.account_video('aaaaaaaaaaa')


class RelayTests(Case):
    def test_account_links_are_signed_with_their_way(self):
        url = 'https://rr1---sn-x.googlevideo.com/videoplayback?id=1'
        link = ht.relay_link(url, account=True)
        token, sig = link.split('u=')[1].split('&s=')
        self.assertEqual(ht.relay_route(token, sig), (url, True))
        self.assertEqual(ht.relay_route(*ht.relay_link(url).split('u=')[1].split('&s=')), (url, False))
        plain_token = ht.relay_link(url).split('u=')[1].split('&s=')[0]
        with self.assertRaises(ValueError):
            ht.relay_route(plain_token, sig)                      # the way cannot be swapped on a signature

    def test_a_signed_in_videos_playlists_are_fetched_by_the_account_service(self):
        ht._account_videos.add('aaaaaaaaaaa')
        ht._cache['v:aaaaaaaaaaa'] = (time.time(), {'hls': 'https://manifest.googlevideo.com/api/manifest/hls_variant/x'})
        master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.4d401f"\nhttps://manifest.googlevideo.com/hls_playlist/a.m3u8\n'
        with mock.patch.object(ht, 'account_stream', return_value=(200, {}, master.encode(), 'https://manifest.googlevideo.com/x')) as fetch, \
             mock.patch.object(ht, '_stream_get', side_effect=AssertionError('not through the tunnel')):
            out = ht.stream_master('aaaaaaaaaaa')
        fetch.assert_called_once()
        token, sig = out.splitlines()[-1].split('u=')[1].split('&s=')
        self.assertEqual(ht.relay_route(token, sig)[1], True)    # the variants are account links too

    def test_stream_parts_travel_over_the_socket(self):
        self.serve()
        body = b'\x47' * 188 * 50
        with mock.patch.object(ht, '_stream_get', return_value=(200, {'Content-Type': 'video/mp2t'}, body, 'https://x.googlevideo.com/v')) as get:
            status, headers, got, _final = ht.account_stream('https://x.googlevideo.com/videoplayback?a=1', 'bytes=0-99')
        self.assertEqual((status, got, headers['Content-Type']), (200, body, 'video/mp2t'))
        self.assertEqual(get.call_args.args[1], 'bytes=0-99')
        self.assertFalse(get.call_args.kwargs['route'])          # home way: no tunnel check
        with self.assertRaisesRegex(RuntimeError, 'googlevideo'):
            ht.account_stream('https://example.com/steal')         # nothing but YouTube's stream and caption hosts


if __name__ == '__main__':
    unittest.main()
