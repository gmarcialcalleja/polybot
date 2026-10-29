import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from polybot.http import SourceError
from polybot.markets import available, parse_contract, quote
from polybot.model import estimate, top_picks, value
from polybot.report import payload, send
from polybot.stats import GameLog, PlayerStats, POSITIONS, TEAMS, normalize, parse_defense, parse_logs


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 29)
        self.ranks = {p: {'ATL': 27, 'BOS': 2} for p in POSITIONS}
        games = [GameLog(self.day - timedelta(days=i), 'ATL', 30 if i % 2 else 10, 32)
                 for i in range(1, 5)]
        games += [GameLog(date(2026, 4, 20) - timedelta(days=i), 'ATL', 30, 34)
                  for i in range(78)]
        self.stats = PlayerStats('Player', 0.25, 0.25, games, 'https://www.statmuse.com')

    def test_early_season_blend_and_blowout_discount(self):
        result = estimate(self.stats, 25, 'PG', self.ranks, self.day, 11)
        self.assertEqual((result.hits, result.sample), (2, 4))
        self.assertAlmostEqual(result.current_weight, 0.4)
        self.assertAlmostEqual(result.probability, .4*.5 + .6*(80/82) - .035)
        self.assertTrue(any('LOW SAMPLE' in n for n in result.notes))

    def test_filter_unknown_spread_low_usage_and_minutes(self):
        with self.assertRaises(SourceError):
            estimate(self.stats, 25, 'PG', self.ranks, self.day, None)
        self.stats.usage = .20
        with self.assertRaises(SourceError):
            estimate(self.stats, 25, 'PG', self.ranks, self.day, 5)
        self.stats.usage = .25
        self.stats.games = [GameLog(date(2026, 10, 28), 'ATL', 20, 15)]
        with self.assertRaises(SourceError):
            estimate(self.stats, 25, 'PG', self.ranks, self.day, 5)

    def test_edge_ev_kelly(self):
        result = value(.722, .58)
        self.assertAlmostEqual(result['edge'], .142)
        self.assertAlmostEqual(result['ev_per_dollar'], .722/.58 - 1)
        self.assertAlmostEqual(result['fractional_kelly'], .25*.142/.42)

    def test_distinct_players_and_positive_edges(self):
        picks = top_picks([{'player': 'A', 'edge': .1}, {'player': 'A', 'edge': .2},
                           {'player': 'B', 'edge': 0}, {'player': 'C', 'edge': .05}])
        self.assertEqual([p['player'] for p in picks], ['A', 'C'])


class ParsingTests(unittest.TestCase):
    def test_real_format_over_is_converted_without_off_by_one(self):
        market = {'id': '1', 'question': 'Luka Dončić: Points O/U 24.5', 'sportsMarketType': 'points',
                  'active': True, 'closed': False, 'acceptingOrders': True, 'enableOrderBook': True,
                  'outcomes': '["No", "Yes"]', 'clobTokenIds': '["no-token", "yes-token"]'}
        result = parse_contract(market, {'slug': 'nba-lal-lac-2026-10-29'})
        self.assertEqual(result['threshold'], 25)
        self.assertEqual(result['token'], 'yes-token')
        market['question'] = 'Luka Dončić: Points O/U 25'
        self.assertIsNone(parse_contract(market, {'slug': 'nba-lal-lac-2026-10-29'}))
        market['sportsMarketType'] = 'totals'
        self.assertIsNone(parse_contract(market, {'slug': 'nba-lal-lac-2026-10-29'}))
        self.assertEqual(normalize('Luka Dončić'), normalize('Luka Doncic'))

    def test_defense_ranks_within_position_not_global_badges(self):
        rows = []
        teams = sorted(TEAMS)
        for position in POSITIONS:
            for i, t in enumerate(teams):
                rows.append(f'<tr><td>{position}</td><td><span id="team">{t}</span>'
                            f'<span class="badge">150</span></td><td><span id="pts">{i+10}</span>'
                            '<span class="badge">2</span></td></tr>')
        html = 'Updated: 28 October 2026<table>' + ''.join(rows) + '</table>'
        ranks, _ = parse_defense(html, date(2026, 10, 29))
        self.assertEqual(ranks['PG'][teams[-1]], 30)
        self.assertEqual(ranks['SG'][teams[0]], 1)
        with self.assertRaises(SourceError):
            parse_defense(html, date(2026, 11, 10))

    def test_logs_exclude_future_dnp_playoffs_and_duplicates(self):
        header = '<thead><tr><th>DATE</th><th>OPP</th><th>PTS</th><th>MIN</th></tr></thead>'
        row = '<tr><td><a href="/nba/game/10-27-2026-atl-at-bos-123">Tue 10/27</a></td><td>GS</td><td>25</td><td>31:30</td></tr>'
        html = '<h2>October</h2><table>' + header + '<tbody>' + row*2 + '</tbody></table>'
        html += '<h2>First Round</h2><table>' + header + '<tbody>' + row + '</tbody></table>'
        logs = parse_logs(html, date(2026, 10, 28))
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0].points, logs[0].minutes, logs[0].opponent), (25, 31.5, 'GSW'))
        self.assertFalse(parse_logs(html, date(2026, 10, 27)))

    def test_injury_screen(self):
        self.assertTrue(available({'status': 'active', 'injuries': []}))
        for status in ['Out', 'Doubtful', 'Questionable']:
            self.assertFalse(available({'status': 'active', 'injuries': [{'status': status}]}))
        self.assertFalse(available({'status': 'active', 'injuries':
                         [{'status': 'Probable', 'details': 'minutes restriction'}]}))


class DeliveryTests(unittest.TestCase):
    def test_executable_ask_not_midpoint_and_minimum_depth(self):
        class FakeHttp:
            def json(self, *args):
                return {'asks': [{'price': '0.8', 'size': '100'}, {'price': '0.6', 'size': '20'}],
                        'timestamp': str(datetime.now(timezone.utc).timestamp()*1000), 'min_order_size': '5'}
        contract = {'token': 'x', 'fee_enabled': True, 'fee_schedule': {'rate': 0}}
        self.assertEqual(quote(FakeHttp(), contract, 10)['price'], .6)
        with self.assertRaises(SourceError):
            quote(FakeHttp(), contract, 15)
        contract['fee_schedule']['rate'] = .05
        with self.assertRaises(SourceError):
            quote(FakeHttp(), contract, 10)

    def test_webhook_failures_do_not_leak_secrets(self):
        import requests
        url = 'https://discord.com/api/webhooks/123/super-secret'
        with patch('polybot.report.requests.post', side_effect=requests.Timeout(url)):
            with self.assertRaises(SourceError) as result:
                send(url, {})
        self.assertNotIn('super-secret', str(result.exception))
        with self.assertRaises(SourceError):
            send('http://example.com/secret', {})

    def test_no_picks_report_is_explicit_and_mentions_disabled(self):
        message = payload({'date': '2026-10-29', 'timezone': 'America/New_York', 'status': 'ok',
                           'contracts_found': 0, 'valued': 0, 'picks': [], 'message': 'No contracts published.'})
        self.assertIn('No contracts published.', message['content'])
        self.assertEqual(message['allowed_mentions'], {'parse': []})


if __name__ == '__main__':
    unittest.main()
