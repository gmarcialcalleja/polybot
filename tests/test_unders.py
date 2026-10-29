import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from polybot import kalshi
from polybot.http import SourceError
from polybot.__main__ import analyze, finalize
from polybot.markets import contracts, quote
from polybot.model import estimate, value
from polybot.report import bet_label, payload, payloads
from polybot.stats import GameLog, PlayerStats, POSITIONS
from polybot.unders import DefenderContext, estimate_under, recent_defensive_awards


class UnderModelTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 29)
        self.ranks = {p: {'BOS': 2, 'ATL': 27} for p in POSITIONS}
        current = [GameLog(self.day-timedelta(days=i), 'BOS', points, 32)
                   for i, points in enumerate((24, 25, 10, 30), 1)]
        baseline = [GameLog(date(2026, 4, 20)-timedelta(days=i), 'BOS', 24, 34) for i in range(78)]
        self.stats = PlayerStats('Example', .25, .25, current+baseline, 'https://espn.com')

    def test_under_exact_boundary_and_strong_defense_cohort(self):
        result = estimate_under(self.stats, 25, 'SG', self.ranks, self.day, 5)
        self.assertEqual((result.hits, result.sample), (2, 4))
        self.assertAlmostEqual(result.probability, .4*.5 + .6*(80/82))
        # The over's weak-defense cohort is independent; we never invert that TP.
        weak = [GameLog(self.day-timedelta(days=i), 'ATL', 40, 32) for i in range(1, 5)]
        changed = PlayerStats('Example', .25, .25,
                              sorted(self.stats.games+weak, key=lambda g: g.date, reverse=True), '')
        self.assertGreater(estimate(changed, 25, 'SG', self.ranks, self.day, 5).probability, 0)
        self.assertEqual(result.raw_hit_rate, .5)

    def test_under_does_not_get_unvalidated_blowout_probability_boost(self):
        normal = estimate_under(self.stats, 25, 'SG', self.ranks, self.day, 5)
        blowout = estimate_under(self.stats, 25, 'SG', self.ranks, self.day, 12)
        self.assertEqual(normal.probability, blowout.probability)
        self.assertTrue(any('no automatic' in n for n in blowout.notes))

    def test_original_over_model_regression(self):
        games = [GameLog(self.day-timedelta(days=i), 'ATL', 30 if i%2 else 10, 32) for i in range(1, 5)]
        games += [GameLog(date(2026, 4, 20)-timedelta(days=i), 'ATL', 30, 34) for i in range(78)]
        stats = PlayerStats('Example', .25, .25, games, '')
        result = estimate(stats, 25, 'SG', self.ranks, self.day, 11)
        self.assertAlmostEqual(result.probability, .4*.5+.6*(80/82)-.035)


class UnderPricingTests(unittest.TestCase):
    def test_polymarket_no_token_has_its_own_ask(self):
        market = {'id': '1', 'question': 'Example: Points O/U 24.5', 'sportsMarketType': 'points',
                  'active': True, 'closed': False, 'acceptingOrders': True, 'enableOrderBook': True,
                  'outcomes': '["Yes", "No"]', 'clobTokenIds': '["yes-token", "no-token"]',
                  'feesEnabled': False}
        parsed = contracts([{'slug': 'nba-atl-bos-2026-10-29', 'markets': [market]}])
        self.assertEqual(len(parsed), 2)
        class Http:
            def json(self, url, params):
                price = '.7' if params['token_id'] == 'yes-token' else '.4'
                return {'asks': [{'price': price, 'size': '100'}],
                        'timestamp': str(datetime.now(timezone.utc).timestamp()*1000)}
        self.assertEqual(quote(Http(), parsed[0], 10)['price'], .7)
        self.assertEqual(quote(Http(), parsed[1], 10)['price'], .4)
        self.assertEqual(parsed[1]['outcome'], 'NO')
        self.assertIn('24 or fewer', bet_label(parsed[1]))

    def test_kalshi_no_ask_uses_yes_bid_and_no_fee_cost(self):
        class Http:
            def json(self, *args):
                return {'orderbook_fp': {'yes_dollars': [['.55', '100']], 'no_dollars': [['.70', '100']]}}
        base = {'id': 'x', 'fee_type': 'quadratic', 'fee_multiplier': 1}
        over = kalshi.quote(Http(), base, 10)
        under = kalshi.quote(Http(), {**base, 'side': 'under'}, 10)
        self.assertEqual(over['price'], .3)
        self.assertEqual(under['price'], .45)
        self.assertGreater(under['fee_per_share'], 0)
        self.assertNotAlmostEqual(under['price'], 1-over['price'])


class DefenderTests(unittest.TestCase):
    def test_missing_defender_data_is_a_gap_not_a_probability_adjustment(self):
        class Http:
            def json(self, *args, **kwargs):
                raise SourceError('Defensive feed unavailable')
        players = {'a': {'name': 'Example', 'id': '1', 'team': 'BOS', 'position': 'SG',
                         'status': 'active', 'injuries': []}}
        result = DefenderContext(Http(), None, players, date(2026, 10, 29)).for_opponent('BOS')
        self.assertEqual(len(result['data_gaps']), 1)
        self.assertEqual(result['probability_adjustment'], 0)
        self.assertEqual(result['perimeter_defenders'], [])

    def test_recent_awards_exclude_old_awards_and_future_seasons(self):
        awards = {'awards': [{'league': 'nba', 'name': 'All-Defensive 1st Team', 'seasons': ['2027', '2026', '2020']},
                             {'league': 'nba', 'name': 'MVP', 'seasons': ['2026']}]}
        self.assertEqual(recent_defensive_awards(awards, 2027), ['All-Defensive 1st Team 2026'])

    def test_absent_defenders_never_become_supporting_evidence(self):
        class Http:
            def json(self, url, ttl=0):
                if 'bio' in url:
                    return {'awards': [{'league': 'nba', 'name': 'All-Defensive 2nd Team', 'seasons': ['2026']}]}
                return {'splits': {'categories': [{'stats': [{'name': 'gamesPlayed', 'value': 20},
                                                            {'name': 'avgMinutes', 'value': 28},
                                                            {'name': 'avgBlocks', 'value': 2}]}]}}
        players = {'a': {'name': 'Available Wing', 'id': '1', 'team': 'BOS', 'position': 'SF',
                         'status': 'active', 'injuries': []},
                   'b': {'name': 'Injured Blocker', 'id': '2', 'team': 'BOS', 'position': 'C',
                         'status': 'active', 'injuries': [{'status': 'Out'}]}}
        provider = DefenderContext(Http(), None, players, date(2026, 10, 29))
        context = provider.for_opponent('BOS')
        self.assertEqual([p['player'] for p in context['perimeter_defenders']], ['Available Wing'])
        self.assertEqual([p['player'] for p in context['shot_blockers']], ['Available Wing'])
        self.assertEqual(context['unavailable'], ['Injured Blocker'])
        self.assertEqual(context['probability_adjustment'], 0)
        self.assertFalse(context['confirmed_assignment'])
        self.assertIs(provider.for_opponent('BOS'), context)


class IsolationTests(unittest.TestCase):
    def test_six_under_cards_with_defender_evidence_fit_discord(self):
        context = {'team': 'BOS', 'perimeter_defenders': [
            {'player': 'Example Defender', 'evidence': ['All-Defensive 1st Team 2026']}],
            'shot_blockers': [], 'unavailable': [], 'data_gaps': []}
        def pick(i):
            return {'id': str(i), 'player': 'Example Player', 'side': 'under', 'threshold': 25,
                    'url': 'https://kalshi.com/markets/example', 'team': 'ATL', 'position': 'SG',
                    'opponent': 'BOS', 'defense_rank': 2, 'price': .2, 'depth_usd': 100,
                    'defender_context': context, **value(.8, .2),
                    'estimate': {'probability': .8, 'hits': 4, 'sample': 5,
                                 'baseline_sample': 25, 'notes': ['A'*400]}}
        message = payload({'date': '2026-10-29', 'timezone': 'America/New_York', 'status': 'ok',
                           'venue': 'Kalshi', 'direction': 'under', 'contracts_found': 6, 'valued': 6,
                           'picks': [pick(i) for i in range(3)],
                           'high_roi_picks': [pick(i) for i in range(3, 6)]})
        length = sum(len(e['title'])+len(e['description'])+len(e['footer']['text'])+
                     sum(len(f['name'])+len(f['value']) for f in e['fields']) for e in message['embeds'])
        self.assertEqual(len(message['embeds']), 6)
        self.assertLessEqual(length, 6000)
        self.assertLessEqual(len(message['content']), 2000)

    def test_separate_rankings_and_opposing_recommendation_guard(self):
        candidates = [{'id': '1', 'venue': 'Polymarket', 'player': 'A', 'side': 'over', **value(.7, .4)},
                      {'id': '2', 'venue': 'Kalshi', 'player': 'A', 'side': 'under', **value(.8, .2)},
                      {'id': '3', 'venue': 'Kalshi', 'player': 'B', 'side': 'under', **value(.8, .2)}]
        report = {'candidates': candidates, 'skipped': [], 'venues': {
            v: {'venue': v, 'status': 'ok', 'contracts_found': 1, 'under_options': 2} for v in ('Polymarket', 'Kalshi')}}
        result = finalize(report)
        self.assertEqual([p['player'] for p in result['picks']], ['A'])
        self.assertEqual([p['player'] for p in result['under_picks']], ['B'])
        self.assertEqual(result['venues']['Kalshi']['unders']['conflicts_with_overs'], 1)

    def test_under_goes_through_its_own_path_without_changing_over_selection(self):
        day = date(2026, 10, 29)
        game = {'teams': [{'team': 'ATL'}, {'team': 'BOS'}], 'spread': 5, 'state': 'pre',
                'start': datetime.now(timezone.utc)+timedelta(hours=3)}
        players = {'overplayer': {'name': 'Over Player', 'id': '1', 'team': 'BOS', 'opponent': 'ATL',
                                  'position': 'SG', 'status': 'active', 'injuries': [], 'game': game},
                   'underplayer': {'name': 'Under Player', 'id': '2', 'team': 'ATL', 'opponent': 'BOS',
                                   'position': 'SG', 'status': 'active', 'injuries': [], 'game': game}}
        props = [{'id': str(i), 'venue': 'Polymarket', 'player': name, 'side': side,
                  'threshold': 25, 'teams': {'ATL', 'BOS'}, 'url': 'https://polymarket.com'}
                 for i, name in enumerate(('Over Player', 'Under Player')) for side in ('over', 'under')]
        def stats(name, *args):
            opponent, pts = ('ATL', 30) if name == 'Over Player' else ('BOS', 20)
            games = [GameLog(day-timedelta(days=i), opponent, pts, 32) for i in range(1, 5)]
            return PlayerStats(name, .25, .25, games, 'https://espn.com')
        context = {'team': 'BOS', 'perimeter_defenders': [], 'shot_blockers': [], 'unavailable': [], 'data_gaps': []}
        with patch('polybot.__main__.slate', return_value=[game]), \
             patch('polybot.__main__.nba_events', return_value=[]), \
             patch('polybot.__main__.contracts', return_value=props), \
             patch('polybot.__main__.kalshi.contracts', return_value=[]), \
             patch('polybot.__main__.defense', return_value=({p: {'ATL': 27, 'BOS': 2} for p in POSITIONS}, {})), \
             patch('polybot.__main__.rosters', return_value=players), \
             patch('polybot.__main__.ESPN') as provider, \
             patch('polybot.__main__.quote', return_value={'price': .4, 'depth_usd': 100}), \
             patch('polybot.__main__.DefenderContext') as defenders:
            provider.return_value.player.side_effect = stats
            defenders.return_value.for_opponent.return_value = context
            report = analyze(None, day, ZoneInfo('America/New_York'), 10)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual([p['player'] for p in report['picks']], ['Over Player'])
        self.assertEqual([p['player'] for p in report['under_picks']], ['Under Player'])
        self.assertEqual(report['contracts_found'], 2)
        messages = payloads(report)
        self.assertEqual(len(messages), 4)
        self.assertIn('OVER', messages[0]['content'])
        self.assertIn('UNDER', messages[1]['content'])
        self.assertIn('Under 24.5', messages[1]['embeds'][0]['title'])


if __name__ == '__main__':
    unittest.main()
