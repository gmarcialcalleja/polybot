import unittest
from datetime import datetime, timezone
from decimal import Decimal

from polybot.http import SourceError
from polybot.markets import contracts, fee_for_order, fee_rate, quote
from polybot.model import high_roi_picks, top_picks, value


class PolymarketFeeTests(unittest.TestCase):
    def contract(self, rate=.05):
        return {'token': 'yes', 'fee_enabled': True,
                'fee_schedule': {'rate': rate, 'exponent': 1, 'takerOnly': True, 'rebateRate': .15}}

    def http(self, price='.5', minimum='5', size='1000'):
        class Http:
            def json(self, *args):
                return {'asks': [{'price': price, 'size': size}], 'min_order_size': minimum,
                        'timestamp': str(datetime.now(timezone.utc).timestamp()*1000)}
        return Http()

    def test_published_sports_table_and_symmetry(self):
        for p, expected in [('.01', '.04950'), ('.1', '.45000'), ('.3', '1.05000'),
                            ('.5', '1.25000'), ('.7', '1.05000'), ('.99', '.04950')]:
            with self.subTest(price=p):
                self.assertEqual(fee_for_order(100, p, '.05'), Decimal(expected))

    def test_fee_precision_and_smallest_charge(self):
        self.assertEqual(fee_for_order(1, '.333', '.05'), Decimal('.01111'))
        self.assertEqual(fee_for_order('.001', '.1', '.05'), 0)
        self.assertEqual(fee_for_order('.001', '.5', '.05'), Decimal('.00001'))

    def test_market_rate_is_used_not_hardcoded_or_rebate_discounted(self):
        self.assertEqual(fee_rate(self.contract('.03')), Decimal('.03'))
        self.assertEqual(fee_rate(self.contract('.05')), Decimal('.05'))
        c = self.contract('.05')
        c['fee_schedule']['rebateRate'] = .99
        q = quote(self.http(), c, 10)
        self.assertAlmostEqual(q['fee_per_share'], .0125, places=6)
        self.assertEqual(q['fee_rate'], .05)

    def test_explicit_zero_fee_and_missing_status_are_distinct(self):
        self.assertEqual(fee_rate({'fee_enabled': False}), 0)
        self.assertEqual(fee_rate({'fee_enabled': True, 'fee_schedule': {'rate': 0}}), 0)
        for c in ({}, {'fee_enabled': None}, {'fee_enabled': 'false'}, {'fee_enabled': 0}):
            with self.subTest(contract=c), self.assertRaises(SourceError):
                fee_rate(c)

    def test_invalid_and_unsupported_schedules_fail_closed(self):
        for schedule in (None, {}, 'unknown', {'rate': None}, {'rate': -.05}, {'rate': 2},
                         {'rate': 'NaN'}, {'rate': 'Infinity'}, {'rate': True},
                         {'rate': .05}, {'rate': .05, 'exponent': 2},
                         {'rate': .05, 'exponent': True}):
            with self.subTest(schedule=schedule), self.assertRaises(SourceError):
                fee_rate({'fee_enabled': True, 'fee_schedule': schedule})

    def test_budget_and_minimum_include_fee(self):
        q = quote(self.http(), self.contract(), 10)
        self.assertLessEqual(Decimal(str(q['shares']))*Decimal(str(q['price']))+
                             Decimal(str(q['fee_usd'])), Decimal('10'))
        self.assertEqual(q['shares'], 19.51)
        # Without fees 5 shares fit in $2.50; including fees they do not.
        with self.assertRaisesRegex(SourceError, 'minimum order size including fees'):
            quote(self.http(), self.contract(), 2.5)
        self.assertEqual(quote(self.http(), {'token': 'yes', 'fee_enabled': False}, 2.5)['shares'], 5)

    def test_fee_reduces_roi_kelly_and_can_remove_pick_or_roi_alert(self):
        q = quote(self.http(), self.contract(), 10)
        before = value(.51, q['price'])
        after = value(.51, q['price'], fee_per_share=q['fee_per_share'])
        self.assertGreater(before['edge'], 0)
        self.assertLess(after['edge'], 0)
        self.assertLess(after['fractional_kelly'], before['fractional_kelly'])
        self.assertLess(after['expected_roi'], before['expected_roi'])
        self.assertEqual(top_picks([{'player': 'A', **after}]), [])
        q = quote(self.http('.25'), self.contract(), 10)
        self.assertEqual(len(high_roi_picks([{'player': 'A', **value(.51, .25)}])), 1)
        self.assertEqual(high_roi_picks([{'player': 'A', **value(.51, .25, fee_per_share=q['fee_per_share'])}]), [])

    def test_parser_retains_fee_metadata_for_both_sides_and_uses_actual_asks(self):
        m = {'id': '1', 'question': 'Example: Points O/U 24.5', 'sportsMarketType': 'points',
             'active': True, 'closed': False, 'acceptingOrders': True, 'enableOrderBook': True,
             'outcomes': ['No', 'Yes'], 'clobTokenIds': ['no', 'yes'], 'feesEnabled': True,
             'feeSchedule': self.contract()['fee_schedule'], 'feeType': 'sports_fees_v3'}
        cs = contracts([{'slug': 'nba-atl-bos-2026-11-05', 'markets': [m]}])
        class Http:
            def json(self, url, params):
                p = '.4' if params['token_id'] == 'yes' else '.7'
                return {'asks': [{'price': p, 'size': '1000'}], 'min_order_size': '5',
                        'timestamp': str(datetime.now(timezone.utc).timestamp()*1000)}
        overs, unders = [quote(Http(), c, 10) for c in cs]
        self.assertEqual(cs[0]['fee_type'], 'sports_fees_v3')
        self.assertEqual((overs['price'], unders['price']), (.4, .7))
        for q in (overs, unders):
            self.assertGreater(q['fee_per_share'], 0)
        # Missing Gamma flag is no longer silently converted to fee-free.
        del m['feesEnabled']
        self.assertIsNone(contracts([{'slug': 'nba-atl-bos-2026-11-05', 'markets': [m]}])[0]['fee_enabled'])


if __name__ == '__main__':
    unittest.main()
