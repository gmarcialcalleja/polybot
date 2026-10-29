"""Public Kalshi NBA player-points discovery, YES asks and taker fees."""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from .http import SourceError
from .stats import TEAMS, TEAM_ALIASES, team

BASE = 'https://external-api.kalshi.com/trade-api/v2'
SERIES = 'KXNBAPTS'
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


def number(value):
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except InvalidOperation:
        pass
    raise SourceError('Kalshi: missing or invalid numeric field')


def event_teams(event_ticker, day):
    prefix = f'{SERIES}-{day.year % 100:02d}{MONTHS[day.month-1]}{day.day:02d}'
    if not event_ticker.startswith(prefix):
        return None
    suffix = event_ticker[len(prefix):]
    codes = TEAMS | set(TEAM_ALIASES)
    pairs = {frozenset((team(a), team(b))) for a in codes for b in codes
             if a+b == suffix and team(a) != team(b)}
    if len(pairs) != 1:
        raise SourceError('Kalshi: ambiguous or unknown NBA event team codes')
    return set(next(iter(pairs)))


def parse_contract(market, day, fee_type, fee_multiplier):
    event = market.get('event_ticker', '')
    teams = event_teams(event, day)
    if teams is None or market.get('status') != 'active' or market.get('market_type') != 'binary':
        return None
    if market.get('mve_selected_legs') or market.get('mve_collection_ticker'):
        return None
    match = re.fullmatch(r'(.+?): (\d+)\+ points', market.get('title', ''), re.I)
    if not match:
        return None
    name, threshold = match.groups()
    threshold = int(threshold)
    if (market.get('strike_type') != 'greater'
            or number(market.get('floor_strike')) != Decimal(threshold) - Decimal('.5')
            or number(market.get('notional_value_dollars')) != 1):
        raise SourceError('Kalshi: unsupported points threshold or contract payout')
    # Confirm full-game points semantics in the actual rules, not just a ticker.
    rules = market.get('rules_primary', '')
    if (not rules.startswith(f'If {name} records {threshold}+ points in ')
            or 'game originally scheduled for' not in rules
            or not rules.endswith('then the market resolves to Yes.')
            or any(term in rules.lower() for term in ('quarter', 'half', 'combined', 'series'))):
        raise SourceError('Kalshi: unsupported full-game points rules')
    now = datetime.now(timezone.utc)
    opened = datetime.fromisoformat(market['open_time'].replace('Z', '+00:00'))
    closed = datetime.fromisoformat(market['close_time'].replace('Z', '+00:00'))
    if not opened <= now < closed or market.get('result'):
        return None
    return {'id': market['ticker'], 'venue': 'Kalshi', 'side': 'over', 'outcome': 'YES',
            'player': name, 'threshold': threshold,
            'event': event, 'question': market['title'], 'teams': teams,
            'url': f'https://kalshi.com/markets/kxnbapts/nba-player-points/{event.lower()}',
            'fee_type': fee_type, 'fee_multiplier': fee_multiplier,
            'settlement_note': 'Inactive/DNP or cancellation can settle at a fair price; ROI assumes a played, normally settled game.'}


def contracts(http, day):
    series = http.json(BASE + f'/series/{SERIES}')['series']
    fee_type = series.get('fee_type')
    fee_multiplier = series.get('fee_multiplier')
    cursor, seen, markets = '', set(), []
    for _ in range(50):
        params = {'series_ticker': SERIES, 'status': 'open', 'limit': 1000}
        if cursor:
            params['cursor'] = cursor
        page = http.json(BASE + '/markets', params)
        if not isinstance(page.get('markets'), list):
            raise SourceError('Kalshi: unexpected markets schema')
        markets.extend(page['markets'])
        cursor = page.get('cursor', '')
        if not cursor:
            break
        if cursor in seen:
            raise SourceError('Kalshi: pagination cursor repeated')
        seen.add(cursor)
    else:
        raise SourceError('Kalshi: pagination limit reached; refusing an incomplete scan')
    events, results = {}, {}
    for market in markets:
        event = market.get('event_ticker', '')
        if event_teams(event, day) is None:
            continue
        if event not in events:
            events[event] = http.json(BASE + f'/events/{event}')['event']
        metadata = events[event]
        contract = parse_contract(market, day,
                                  metadata.get('fee_type_override') or fee_type,
                                  metadata.get('fee_multiplier_override')
                                  if metadata.get('fee_multiplier_override') is not None else fee_multiplier)
        if contract:
            results[(contract['id'], 'over')] = contract
            results[(contract['id'], 'under')] = {**contract, 'side': 'under', 'outcome': 'NO'}
    return list(results.values())


def fee_for_order(count, price, multiplier):
    """General quadratic taker fee; round total cost upward to a centicent."""
    price, count, multiplier = Decimal(str(price)), Decimal(count), Decimal(str(multiplier))
    notional = count*price
    fee = multiplier*Decimal('.07')*count*price*(1-price)
    total = (notional+fee).quantize(Decimal('.0001'), rounding=ROUND_CEILING)
    return total-notional


def quote(http, contract, stake):
    fee_type = contract.get('fee_type')
    if fee_type not in ('quadratic', 'quadratic_with_maker_fees'):
        raise SourceError('Kalshi: unsupported or unknown taker fee type')
    multiplier = number(contract.get('fee_multiplier'))
    if not multiplier.is_finite() or multiplier < 0:
        raise SourceError('Kalshi: missing or invalid fee multiplier')
    data = http.json(BASE + f"/markets/{contract['id']}/orderbook")
    # Opposite-side bids supply the executable ask for the side being purchased.
    under = contract.get('side', 'over') == 'under'
    levels = data.get('orderbook_fp', {}).get('yes_dollars' if under else 'no_dollars')
    if levels is None:
        raise SourceError('Kalshi: expected fixed-point opposite-side bid levels')
    bids = [(number(p), number(n)) for p, n in levels]
    bids = [(p, n) for p, n in bids if 0 < p < 1 and n > 0]
    if not bids:
        raise SourceError(f"Kalshi: no executable {'NO' if under else 'YES'} asks")
    best_no = max(p for p, _ in bids)
    price = 1-best_no
    depth_shares = sum(n for p, n in bids if p == best_no)
    budget = Decimal(str(stake))
    cost_estimate = price + multiplier*Decimal('.07')*price*(1-price)
    count = int((budget/cost_estimate).to_integral_value(rounding=ROUND_FLOOR))
    while count > 0 and count*price + fee_for_order(count, price, multiplier) > budget:
        count -= 1
    if count < 1:
        raise SourceError('Kalshi: unit too small for one contract including fees')
    if depth_shares < count:
        raise SourceError('Kalshi: insufficient depth at best ask for the configured unit')
    fee = fee_for_order(count, price, multiplier)
    return {'price': float(price), 'fee_per_share': float(fee/count),
            'fee_usd': float(fee), 'shares': count,
            'depth_usd': float(price*depth_shares),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'timestamp_basis': 'fresh REST order-book retrieval; no book timestamp supplied'}
