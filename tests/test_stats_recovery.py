import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from polybot.__main__ import main
from polybot.http import SourceError
from polybot.stats import POSITION_NAMES, StatMuse, parse_position, parse_usage, parse_espn_logs


def usage_html(name='Donovan Mitchell', season='2026-27', profile='donovan-mitchell-9592'):
    return (f'<table><thead><tr><th>NAME</th><th>USG%</th><th>SEASON</th></tr></thead><tbody>'
            f'<tr><td><a href="/nba/player/{profile}">{name}</a></td><td>28.1</td>'
            f'<td>{season}</td></tr></tbody></table>')


class PositionTests(unittest.TestCase):
    def test_exact_primary_positions_from_player_profile(self):
        for label, position in POSITION_NAMES.items():
            with self.subTest(position=position):
                html = f'<title>Donovan Mitchell - Cleveland Cavaliers {label} | StatMuse</title><h1>Donovan Mitchell</h1>'
                self.assertEqual(parse_position(html, 'Donovan Mitchell'), position)

    def test_wrong_player_and_generic_positions_are_not_guessed(self):
        with self.assertRaises(SourceError):
            parse_position('<title>Other Player - Cavaliers Shooting Guard | StatMuse</title>'
                           '<h1>Other Player</h1>', 'Donovan Mitchell')
        with self.assertRaises(SourceError):
            parse_position('<title>Donovan Mitchell - Cavaliers Guard | StatMuse</title>'
                           '<h1>Donovan Mitchell</h1><aside>Shooting Guard</aside>', 'Donovan Mitchell')

    def test_usage_rejects_other_season_or_player(self):
        with self.assertRaises(SourceError):
            parse_usage(usage_html(season='2025-26'), 'Donovan Mitchell', 2027)
        with self.assertRaises(SourceError):
            parse_usage(usage_html(profile='other-player-123'), 'Donovan Mitchell', 2027)

    def test_alternate_usage_query_and_success_cache(self):
        class Http:
            calls = []
            def get(self, url, ttl=0):
                self.calls.append(url)
                if 'usage-percentage' in url:
                    return '<html>Incomplete stats response</html>'
                if 'usage-rate' in url:
                    return usage_html()
                return ('<title>Donovan Mitchell - Cleveland Cavaliers Shooting Guard | StatMuse</title>'
                        '<h1>Donovan Mitchell</h1>')
        h = Http()
        muse = StatMuse(h)
        result = muse.position('Donovan Mitchell', date(2026, 10, 29))
        self.assertEqual(result, ('SG', 'https://www.statmuse.com/nba/player/donovan-mitchell-9592'))
        self.assertEqual(muse.position('Donovan Mitchell', date(2026, 10, 29)), result)
        self.assertAlmostEqual(muse.usage('Donovan Mitchell', 2027)[0], .281)
        self.assertEqual(len(h.calls), 3)

    def test_visible_answer_headline_works_when_detail_table_requires_login(self):
        current = datetime.now(ZoneInfo('America/New_York'))
        year = current.year + (current.month >= 10)
        html = ('<h1><a href="/nba/player/ja-morant-9976">Ja Morant</a> '
                'has a usage percentage of 21.1 this season.</h1>'
                '<div>Keep musing with a free StatMuse account. Sign in.</div>')
        self.assertAlmostEqual(parse_usage(html, 'Ja Morant', year)[0], .211)
        with self.assertRaises(SourceError):
            parse_usage(html, 'Ja Morant', year-1)
        prior = html.replace('this season', 'in 2025-26')
        self.assertAlmostEqual(parse_usage(prior, 'Ja Morant', 2026)[0], .211)


class ESPNFallbackTests(unittest.TestCase):
    def data(self):
        return {'labels': ['MIN', 'PTS'], 'events': {
                    '1': {'gameResult': 'W', 'gameDate': '2026-10-28T03:00:00Z',
                          'opponent': {'abbreviation': 'GS'}},
                    '2': {'gameResult': 'L', 'gameDate': '2026-10-28T23:00:00Z',
                          'opponent': {'abbreviation': 'BOS'}},
                    '3': {'gameResult': 'W', 'gameDate': '2026-10-27T23:00:00Z',
                          'opponent': {'abbreviation': 'ATL'}}},
                'seasonTypes': [
                    {'displayName': '2026-27 Regular Season', 'categories': [{'type': 'event', 'events': [
                        {'eventId': '1', 'stats': ['31:30', '25']},
                        {'eventId': '2', 'stats': ['0', '0']}]}]},
                    {'displayName': '2026-27 Preseason', 'categories': [{'type': 'event', 'events': [
                        {'eventId': '3', 'stats': ['30', '40']}]}]}]}

    def test_regular_season_only_and_eastern_dates(self):
        games = parse_espn_logs(self.data(), date(2026, 10, 29))
        self.assertEqual(len(games), 1)
        self.assertEqual((games[0].date, games[0].minutes, games[0].points, games[0].opponent),
                         (date(2026, 10, 27), 31.5, 25, 'GSW'))
        self.assertFalse(parse_espn_logs(self.data(), date(2026, 10, 27)))

    def test_gated_statmuse_logs_use_roster_athlete_id_and_record_provenance(self):
        fixture = self.data()
        class Http:
            def get(self, *args, **kwargs):
                return '<h1>Player</h1><div>Create free account</div>'
            def json(self, url, params, ttl=0):
                if '/athletes/123/gamelog' not in url:
                    raise AssertionError('Wrong athlete fallback')
                return fixture
        muse = StatMuse(Http())
        with patch.object(muse, 'usage', return_value=(.25, 'https://www.statmuse.com/nba/player/example-1')):
            result = muse.player('Example', date(2026, 10, 29), '123')
        self.assertEqual(len(result.games), 1)
        self.assertTrue(all('ESPN public fallback' in source for source in result.log_sources))


class ExitStatusTests(unittest.TestCase):
    def run_main(self, status, strict=False):
        argv = ['polybot', '--dry-run'] + (['--strict'] if strict else [])
        with patch('sys.argv', argv), patch('polybot.__main__.Http'), \
             patch('polybot.__main__.analyze', return_value={'status': status, 'picks': []}), \
             patch('polybot.__main__.payloads', return_value=[{'content': status}]), \
             patch('polybot.__main__.Path'), patch('builtins.print') as output:
            return main(), output

    def test_degraded_report_warns_but_does_not_claim_delivery_failure(self):
        code, output = self.run_main('degraded')
        self.assertEqual(code, 0)
        self.assertTrue(any('::warning::' in str(call) for call in output.call_args_list))
        self.assertEqual(self.run_main('degraded', strict=True)[0], 1)

    def test_total_source_failure_still_exits_nonzero(self):
        self.assertEqual(self.run_main('failed')[0], 1)


if __name__ == '__main__':
    unittest.main()
