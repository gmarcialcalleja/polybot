import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from polybot import kalshi
from polybot.__main__ import analyze, finalize
from polybot.http import SourceError
from polybot.model import value
from polybot.report import payloads
from polybot.stats import GameLog, PlayerStats, POSITIONS


class KalshiTests(unittest.TestCase):
    def market(self):
        now = datetime.now(timezone.utc)
        return {'ticker': 'KXNBAPTS-26OCT29CLEATL-CLEDONOVAN45-25',
                'event_ticker': 'KXNBAPTS-26OCT29CLEATL', 'status': 'active',
                'market_type': 'binary', 'title': 'Donovan Mitchell: 25+ points',
                'strike_type': 'greater', 'floor_strike': 24.5, 'notional_value_dollars': '1.0000',
                'open_time': (now-timedelta(days=1)).isoformat(),
                'close_time': (now+timedelta(days=1)).isoformat(),
                'rules_primary': 'If Donovan Mitchell records 25+ points in the Cleveland vs Atlanta Pro Basketball game originally scheduled for Oct 29, 2026, then the market resolves to Yes.'}

    def test_date_teams_rules_and_threshold(self):
        m = self.market()
        c = kalshi.parse_contract(m, date(2026, 10, 29), 'quadratic', 1)
        self.assertEqual((c['venue'], c['threshold'], c['teams']), ('Kalshi', 25, {'CLE', 'ATL'}))
        self.assertIsNone(kalshi.parse_contract(m, date(2026, 10, 30), 'quadratic', 1))
        m['floor_strike'] = 25.5
        with self.assertRaises(SourceError):
            kalshi.parse_contract(m, date(2026, 10, 29), 'quadratic', 1)
        m['floor_strike'] = 24.5
        m['rules_primary'] = m['rules_primary'].replace('points in', 'points in the first quarter of')
        with self.assertRaises(SourceError):
            kalshi.parse_contract(m, date(2026, 10, 29), 'quadratic', 1)

    def test_ask_comes_from_no_bid_and_fee_reduces_roi(self):
        class Http:
            def json(self, *args):
                return {'orderbook_fp': {'no_dollars': [['0.50', '200'], ['0.75', '100']],
                                         'yes_dollars': [['0.10', '100']]}}
        c = {'id': 'x', 'fee_type': 'quadratic', 'fee_multiplier': 1}
        q = kalshi.quote(Http(), c, 10)
        self.assertEqual(q['price'], .25)
        self.assertGreater(q['fee_per_share'], 0)
        self.assertLessEqual(q['shares']*q['price']+q['fee_usd'], 10)
        self.assertEqual(q['shares'], 38)
        metrics = value(.6, q['price'], fee_per_share=q['fee_per_share'])
        self.assertLess(metrics['expected_roi'], value(.6, .25)['expected_roi'])
        self.assertAlmostEqual(metrics['edge'], .6 - metrics['cost_per_share'])
        self.assertEqual(kalshi.fee_for_order(1, '.25', 1), Decimal('.0132'))

    def test_empty_book_thin_depth_and_unknown_fees_withheld(self):
        class Http:
            levels = []
            def json(self, *args):
                return {'orderbook_fp': {'no_dollars': self.levels}}
        h = Http()
        c = {'id': 'x', 'fee_type': 'quadratic', 'fee_multiplier': 1}
        with self.assertRaises(SourceError):
            kalshi.quote(h, c, 10)
        h.levels = [['.75', '2']]
        with self.assertRaises(SourceError):
            kalshi.quote(h, c, 10)
        c['fee_multiplier'] = None
        with self.assertRaises(SourceError):
            kalshi.quote(h, c, 10)
        c.update(fee_multiplier=1, fee_type='unknown')
        with self.assertRaises(SourceError):
            kalshi.quote(h, c, 10)

    def test_cursor_pagination_and_event_fee_overrides(self):
        m = self.market()
        class Http:
            calls = []
            def json(self, url, params=None):
                self.calls.append((url, params))
                if '/series/' in url:
                    return {'series': {'fee_type': 'quadratic', 'fee_multiplier': 1}}
                if '/events/' in url:
                    return {'event': {'fee_multiplier_override': 2}}
                if not params.get('cursor'):
                    return {'markets': [], 'cursor': 'next'}
                return {'markets': [m], 'cursor': ''}
        h = Http()
        result = kalshi.contracts(h, date(2026, 10, 29))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['fee_multiplier'], 2)
        self.assertEqual(sum('/markets' in u for u, _ in h.calls), 2)

    def test_repeated_cursor_is_not_silently_truncated(self):
        class Http:
            def json(self, url, params=None):
                if '/series/' in url:
                    return {'series': {'fee_type': 'quadratic', 'fee_multiplier': 1}}
                return {'markets': [], 'cursor': 'same'}
        with self.assertRaises(SourceError):
            kalshi.contracts(Http(), date(2026, 10, 29))


class VenueTests(unittest.TestCase):
    def test_both_venues_value_same_player_and_share_stats_fetch(self):
        game = {'teams': [{'team': 'CLE'}, {'team': 'ATL'}], 'spread': 5, 'state': 'pre',
                'start': datetime.now(timezone.utc)+timedelta(hours=3)}
        player = {'name': 'Donovan Mitchell', 'position': 'SG', 'team': 'CLE', 'opponent': 'ATL',
                  'game': game, 'status': 'active', 'injuries': []}
        base = {'player': 'Donovan Mitchell', 'threshold': 25, 'teams': {'CLE', 'ATL'},
                'url': 'https://example.com', 'event': 'example'}
        poly = {**base, 'id': 'poly', 'venue': 'Polymarket'}
        kal = {**base, 'id': 'kal', 'venue': 'Kalshi'}
        stats = PlayerStats('Donovan Mitchell', .25, .25,
                            [GameLog(date(2026, 10, 28), 'ATL', 30, 34)], 'https://statmuse.com')
        with patch('polybot.__main__.slate', return_value=[game]), \
             patch('polybot.__main__.nba_events', return_value=[]), \
             patch('polybot.__main__.contracts', return_value=[poly]), \
             patch('polybot.__main__.kalshi.contracts', return_value=[kal]), \
             patch('polybot.__main__.rosters', return_value={'donovanmitchell': player}), \
             patch('polybot.__main__.defense', return_value=({p: {'ATL': 27} for p in POSITIONS}, {})), \
             patch('polybot.__main__.StatMuse') as muse, \
             patch('polybot.__main__.quote', return_value={'price': .25, 'depth_usd': 100}), \
             patch('polybot.__main__.kalshi.quote', return_value={'price': .25, 'fee_per_share': .02, 'depth_usd': 100}):
            muse.return_value.player.return_value = stats
            report = analyze(None, date(2026, 10, 29), ZoneInfo('America/New_York'), 10)
        self.assertEqual(report['valued'], 2)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(muse.return_value.player.call_count, 1)
        self.assertEqual(len(report['venues']['Kalshi']['picks']), 1)
        messages = payloads(report)
        self.assertEqual(len(messages), 2)
        self.assertGreater(report['candidates'][0]['expected_roi'], report['candidates'][1]['expected_roi'])

    def test_maximum_venue_report_fits_discord_limits(self):
        def candidate(i):
            return {'id': str(i), 'player': 'Example Player', 'threshold': 25,
                    'url': 'https://kalshi.com/markets/example', 'team': 'CLE', 'position': 'SG',
                    'opponent': 'ATL', 'defense_rank': 27, 'price': .2, 'depth_usd': 100,
                    'settlement_note': 'Settlement note', **value(.8, .2, fee_per_share=.01),
                    'estimate': {'probability': .8, 'hits': 4, 'sample': 5,
                                 'baseline_sample': 25, 'notes': ['A'*400]}}
        report = {'date': '2026-10-29', 'timezone': 'America/New_York', 'status': 'ok',
                  'games': [1], 'venues': {'Kalshi': {'venue': 'Kalshi', 'status': 'ok',
                      'contracts_found': 6, 'valued': 6, 'picks': [candidate(i) for i in range(3)],
                      'high_roi_picks': [candidate(i) for i in range(3, 6)], 'high_roi_contracts': 6}}}
        message = payloads(report)[0]
        def text_length(value):
            if isinstance(value, str):
                return len(value)
            if isinstance(value, list):
                return sum(map(text_length, value))
            if isinstance(value, dict):
                # Discord's aggregate text limit excludes URLs and other metadata.
                return sum(text_length(v) for k, v in value.items() if k not in ('url', 'color', 'inline'))
            return 0
        self.assertLessEqual(text_length(message['embeds']), 6000)
        self.assertLessEqual(len(message['embeds']), 10)
        self.assertLessEqual(len(message['content']), 2000)

    def test_rankings_are_separate_and_same_player_can_appear_on_both(self):
        candidates = [{'id': 'p', 'venue': 'Polymarket', 'player': 'A', **value(.8, .3)},
                      {'id': 'k', 'venue': 'Kalshi', 'player': 'A', **value(.8, .25)}]
        report = {'candidates': candidates, 'skipped': [], 'venues': {
            v: {'status': 'ok', 'contracts_found': 1} for v in ('Polymarket', 'Kalshi')}}
        result = finalize(report)
        self.assertEqual(len(result['picks']), 2)
        self.assertEqual(len(result['venues']['Kalshi']['picks']), 1)
        self.assertEqual(result['high_roi_contracts'], 2)

    def test_failure_of_one_exchange_does_not_stop_other_discovery(self):
        game = {'teams': [{'team': 'CLE'}, {'team': 'ATL'}],
                'start': datetime.now(timezone.utc)+timedelta(hours=3)}
        with patch('polybot.__main__.slate', return_value=[game]), \
             patch('polybot.__main__.nba_events', side_effect=SourceError('Polymarket unavailable')), \
             patch('polybot.__main__.kalshi.contracts', return_value=[]) as other:
            report = analyze(None, date(2026, 10, 29), ZoneInfo('America/New_York'), 10)
        other.assert_called_once()
        self.assertEqual(report['status'], 'degraded')
        messages = payloads(report)
        self.assertEqual(len(messages), 2)
        self.assertIn('Polymarket unavailable', messages[0]['content'])
        self.assertIn('Kalshi NBA points EV report', messages[1]['content'])
        self.assertNotIn('Polymarket unavailable', messages[1]['content'])


if __name__ == '__main__':
    unittest.main()
