import unittest
from datetime import date

from polybot.espn import ESPN, team_context, usage_segments
from polybot.http import SourceError
from polybot.stats import parse_espn_logs


def player_logs():
    return {'labels': ['MIN', 'FG', 'FT', 'TO', 'PTS'], 'filters': [],
            'events': {'1': {'gameResult': 'W', 'gameDate': '2026-10-28T01:00Z',
                             'opponent': {'abbreviation': 'ATL'}, 'atVs': 'vs',
                             'homeTeamId': '5', 'awayTeamId': '1'},
                       '2': {'gameResult': 'L', 'gameDate': '2026-10-26T01:00Z',
                             'opponent': {'abbreviation': 'BOS'}, 'atVs': '@',
                             'homeTeamId': '2', 'awayTeamId': '13'},
                       '3': {'gameResult': 'W', 'gameDate': '2026-10-25T01:00Z',
                             'opponent': {'abbreviation': 'ALLSTAR'}, 'atVs': '@',
                             'homeTeamId': '100000', 'awayTeamId': '132375'}},
            'seasonTypes': [{'displayName': '2026-27 Regular Season', 'categories': [{'type': 'event',
                            'events': [{'eventId': '1', 'stats': ['32', '8-20', '4-5', '3', '22']},
                                       {'eventId': '2', 'stats': ['30', '7-15', '2-3', '2', '18']},
                                       {'eventId': '3', 'stats': ['20', '9-10', '0-0', '0', '20']}]}]}]}


def team_stats(games=2):
    return {'splits': {'categories': [{'stats': [{'name': 'fieldGoalsAttempted', 'value': 180},
                                                {'name': 'freeThrowsAttempted', 'value': 40},
                                                {'name': 'turnovers', 'value': 24},
                                                {'name': 'gamesPlayed', 'value': games}]}]}}


def schedule(year=2027):
    return {'events': [{'season': {'year': year}, 'seasonType': {'id': '2'},
                        'competitions': [{'status': {'type': {'completed': True}, 'period': period}}]}
                       for period in (4, 5)]}


class ESPNUsageTests(unittest.TestCase):
    def test_usage_groups_traded_player_by_actual_team_and_ignores_allstars(self):
        result = usage_segments(player_logs(), date(2026, 10, 29))
        self.assertEqual(set(result), {'5', '13'})
        self.assertAlmostEqual(result['5']['used'], 20+.44*5+3)
        self.assertEqual(result['13']['minutes'], 30)

    def test_team_minutes_include_overtime_and_require_consistent_game_count(self):
        used, minutes = team_context(team_stats(), schedule(), 2027)
        self.assertAlmostEqual(used, 180+.44*40+24)
        self.assertEqual(minutes, 101)
        with self.assertRaises(SourceError):
            team_context(team_stats(games=3), schedule(), 2027)

    def test_dnp_and_unavailable_history_are_not_fabricated(self):
        self.assertEqual(parse_espn_logs({'filters': []}, date(2026, 10, 29)), [])
        with self.assertRaises(SourceError):
            parse_espn_logs({'events': {'1': {}}, 'labels': []}, date(2026, 10, 29))

    def test_primary_provider_uses_only_espn_and_reuses_shared_requests(self):
        class Http:
            def __init__(self):
                self.calls = []
            def json(self, url, params=None, ttl=0, headers=None):
                self.calls.append((url, params, headers))
                if 'statmuse' in url:
                    raise AssertionError('Primary provider must never call StatMuse')
                if 'fantasy.espn.com' in url:
                    return [{'id': 123, 'fullName': 'Example Player', 'defaultPositionId': 2}]
                if 'gamelog' in url:
                    if params['season'] != 2027:
                        return {'filters': []}
                    return player_logs()
                if url.endswith('statistics'):
                    return team_stats()
                if url.endswith('schedule'):
                    return schedule()
                raise AssertionError(url)
        http = Http()
        provider = ESPN(http)
        position, _ = provider.position('Example Player', date(2026, 10, 29), '123')
        self.assertEqual(position, 'SG')
        result = provider.player('Example Player', date(2026, 10, 29), '123')
        used = 180+.44*40+24
        expected = ((20+.44*5+3)*101/used + (15+.44*3+2)*101/used)/62
        self.assertAlmostEqual(result.usage, expected)
        self.assertIsNone(result.prior_usage)
        self.assertEqual(len(result.games), 2)
        self.assertTrue(all('ESPN' in source for source in result.log_sources))
        provider.player('Example Player', date(2026, 10, 29), '123')
        self.assertEqual(len(http.calls), 8)  # 1 positions + 3 seasons + 2 teams × 2 feeds
        self.assertIsNotNone(http.calls[0][2]['X-Fantasy-Filter'])
        with self.assertRaises(SourceError):
            provider.position('Wrong Player', date(2026, 10, 29), '123')


if __name__ == '__main__':
    unittest.main()
