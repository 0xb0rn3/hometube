"""Home is regular videos, the Music tab is music only, and both end with rows built from what was
watched lately. Music is told apart as YouTube marks it: official artist channels, "- Topic" and VEVO
channels, and titles that say so."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nonet  # noqa: F401  (no network in the tests, see nonet.py)
os.environ['HOMETUBE_CONFIG'] = '/nonexistent/hometube-tests.json'
import hometube as ht


def renderer(vid, title, channel, artist=False):
    badges = [{'metadataBadgeRenderer': {'style': 'BADGE_STYLE_TYPE_VERIFIED_ARTIST' if artist else 'BADGE_STYLE_TYPE_VERIFIED'}}]
    return {'videoRenderer': {'videoId': vid, 'title': {'runs': [{'text': title}]}, 'ownerBadges': badges,
                              'ownerText': {'runs': [{'text': channel}]}, 'lengthText': {'simpleText': '3:30'}}}


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ht, 'STATE', self.tmp)
        self.p.start()
        ht._cache.clear()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp)

    def test_what_counts_as_music(self):
        for title, channel, artist in (('Pawa', 'Mbosso', True), ('Some Song', 'Artist - Topic', False), ('Hello', 'AdeleVEVO', False),
                                       ('Mvua Ya Baraka (Official Music Video)', 'Zuchu', False), ('BONGO MIX 2026', 'DJ Ivan', False),
                                       ('Dharau (Official Lyrics Video)', 'Ibraah', False), ('Tumeachana ft. Harmonize', 'x', False)):
            self.assertTrue(ht._is_music(title, channel, artist), title)
        for title, channel in (('ABC World News Tonight Full Broadcast', 'ABC News'), ('Mixed reactions to the budget', 'BBC News'),
                               ('Phone review: the best camera', 'MKBHD'), ('Arsenal 2-1 Chelsea highlights', 'Sky Sports')):
            self.assertFalse(ht._is_music(title, channel), title)

    def test_search_marks_music_by_youtubes_artist_badge(self):
        page = {'contents': [renderer('aaaaaaaaaaa', 'Pawa', 'Mbosso', artist=True), renderer('bbbbbbbbbbb', 'World news today', 'ABC News')]}
        with mock.patch.object(ht, '_https', return_value=json.dumps(page).encode()):
            found = ht.search('anything')
        self.assertEqual([(i['id'], i['music']) for i in found], [('aaaaaaaaaaa', True), ('bbbbbbbbbbb', False)])
        self.assertEqual([i['id'] for i in ht.by_kind(found, 'music')], ['aaaaaaaaaaa'])
        self.assertEqual([i['id'] for i in ht.by_kind(found, 'media')], ['bbbbbbbbbbb'])
        self.assertEqual(len(ht.by_kind(found, '')), 2)

    def test_history_knows_music_and_the_feed_follows_each_kind(self):
        ht.put_item('history', {'id': 'aaaaaaaaaaa', 'title': 'Pawa', 'channel': 'Mbosso', 'music': True})
        ht.put_item('history', {'id': 'bbbbbbbbbbb', 'title': 'Match highlights', 'channel': 'Sky Sports'})
        near = {'aaaaaaaaaaa': [{'id': f'm{i:010d}', 'title': f'Song {i}', 'music': True} for i in range(6)] + [{'id': 'bbbbbbbbbbb', 'title': 'x', 'music': True}],
                'bbbbbbbbbbb': [{'id': f'v{i:010d}', 'title': f'Clip {i}', 'music': False} for i in range(6)] + [{'id': 'nnnnnnnnnnn', 'title': 'a song', 'music': True}]}
        with mock.patch.object(ht, 'related', side_effect=lambda vid: near[vid]):
            music, media = ht.because('music'), ht.because('media')
        self.assertEqual([r['title'] for r in music], ['Pawa'])
        self.assertTrue(all(v['music'] for v in music[0]['items']))
        self.assertNotIn('bbbbbbbbbbb', [v['id'] for v in music[0]['items']])   # already watched
        self.assertEqual([r['title'] for r in media], ['Match highlights'])
        self.assertNotIn('nnnnnnnnnnn', [v['id'] for v in media[0]['items']])   # music stays on the Music tab

    def test_old_history_gets_a_kind_too(self):
        (self.tmp / 'history.json').write_text(json.dumps([{'id': 'aaaaaaaaaaa', 'title': 'Song (Official Audio)', 'channel': 'x'},
                                                         {'id': 'bbbbbbbbbbb', 'title': 'Cooking pasta', 'channel': 'y'}]))
        self.assertEqual([i['music'] for i in ht.get_list('history')], [True, False])

    def test_music_rows_setting(self):
        self.assertTrue(ht.DEFAULT_MUSIC_ROWS)
        self.assertFalse([r for r in ht.DEFAULT_ROWS if ht._is_music(r[0] + ' ' + r[1])])   # home defaults are not music
        path = Path(tempfile.mkdtemp()) / 'c.json'
        try:
            path.write_text(json.dumps({'music_rows': [['Bongo Flava', 'bongo flava new songs']]}))
            self.assertEqual(ht.load_config(path)['music_rows'], [['Bongo Flava', 'bongo flava new songs']])
            path.write_text(json.dumps({'music_rows': [['only a title']]}))
            with self.assertRaises(ValueError):
                ht.load_config(path)
        finally:
            shutil.rmtree(path.parent)


if __name__ == '__main__':
    unittest.main()
