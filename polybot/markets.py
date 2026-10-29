"""Public ESPN slate/rosters and public Polymarket Gamma/CLOB data."""

import json
import math
import re
from datetime import datetime, time, timedelta, timezone

from .http import SourceError
from .stats import normalize, team

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
ESPN = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'


def array(value):
    return json.loads(value) if isinstance(value, str) else (value or [])


def slate(http, day, tz):
    data = http.json(ESPN + '/scoreboard', {'dates': day.strftime('%Y%m%d'), 'limit': 100})
    games = []
    for event in data.get('events', []):
        start = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
        if start.astimezone(tz).date() != day:
            continue
        competitors = event['competitions'][0]['competitors']
        if len(competitors) != 2:
            raise SourceError('ESPN: unexpected game competitors')
        sides = [{'id': c['team']['id'], 'team': team(c['team']['abbreviation'])} for c in competitors]
        odds = event['competitions'][0].get('odds', [])
        spread = abs(float(odds[0]['spread'])) if odds and odds[0].get('spread') is not None else None
        games.append({'id': event['id'], 'start': start, 'teams': sides, 'spread': spread,
                      'state': event.get('status', {}).get('type', {}).get('state')})
    return games


def rosters(http, games):
    players = {}
    for game in games:
        for side in game['teams']:
            opponent = next(x['team'] for x in game['teams'] if x != side)
            data = http.json(ESPN + f"/teams/{side['id']}/roster", ttl=1800)
            if not data.get('athletes'):
                raise SourceError(f"ESPN: empty roster for {side['team']}")
            for athlete in data['athletes']:
                name = athlete.get('displayName', '')
                players[normalize(name)] = {
                    'id': athlete['id'], 'name': name, 'team': side['team'], 'opponent': opponent,
                    'position': athlete.get('position', {}).get('abbreviation', ''),
                    'injuries': athlete.get('injuries', []),
                    'status': athlete.get('status', {}).get('type', ''), 'game': game,
                }
    return players


def available(player):
    if player['status'].lower() not in ('active',):
        return False
    for injury in player['injuries']:
        status = injury.get('status', '').lower()
        # Questionable players are withheld until their availability is confirmed.
        if status not in ('active', 'probable'):
            return False
        note = json.dumps(injury).lower()
        if any(word in note for word in ('minutes restriction', 'minute restriction',
                                         'minutes limit', 'limited minutes')):
            return False
    return True


def nba_events(http, day, tz):
    sports = http.json(GAMMA + '/sports', ttl=86400)
    sport = next((s for s in sports if s.get('sport') == 'nba'), None)
    if not sport:
        raise SourceError('Polymarket: NBA sports metadata missing')
    start = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    results = []
    # Tags cover NBA events including props; series alone can omit child events.
    for offset in range(0, 2000, 100):
        page = http.json(GAMMA + '/events', {
            'tag_id': sport.get('primaryTagId') or '745', 'active': 'true', 'closed': 'false',
            'end_date_min': start.isoformat(), 'end_date_max': end.isoformat(),
            'limit': 100, 'offset': offset,
        })
        if not isinstance(page, list):
            raise SourceError('Polymarket: unexpected events schema')
        results.extend(e for e in page if f'-{day.isoformat()}' in e.get('slug', ''))
        if len(page) < 100:
            return results
    raise SourceError('Polymarket: pagination limit reached; refusing incomplete market scan')


def parse_contract(market, event):
    if (market.get('sportsMarketType') != 'points' or market.get('closed')
            or not market.get('active') or not market.get('acceptingOrders')
            or not market.get('enableOrderBook')):
        return None
    question = market.get('question', '')
    # Full-game points only. No threes, rebounds, combinations, quarters or unders.
    match = re.fullmatch(r'(.+?):\s*Points O/U (\d+(?:\.\d+)?)', question, flags=re.I)
    if match:
        name, line = match.groups()
        line = float(line)
        # Integer O/U contracts can have push/void semantics. Only half-point lines.
        if line % 1 != 0.5:
            return None
        threshold = math.floor(line) + 1
    else:
        match = re.fullmatch(r'(?:Will )?(.+?) (?:score |record )?(\d+)\+ points\??', question, re.I)
        if not match:
            return None
        name, threshold = match.groups()
        threshold = int(threshold)
    outcomes = array(market.get('outcomes'))
    tokens = array(market.get('clobTokenIds'))
    yes = next((i for i, x in enumerate(outcomes) if x.lower() in ('yes', 'over')), None)
    if yes is None or len(tokens) != len(outcomes):
        return None
    return {'id': market['id'], 'player': name, 'threshold': threshold, 'token': tokens[yes],
            'event': event['slug'], 'question': question,
            'url': f"https://polymarket.com/event/{event['slug']}?tid={market['id']}",
            'fee_enabled': bool(market.get('feesEnabled')), 'fee_schedule': market.get('feeSchedule'),
            'teams': set(re.findall(r'^nba-([a-z]+)-([a-z]+)-', event['slug'])[0])
                     if re.match(r'^nba-[a-z]+-[a-z]+-', event['slug']) else set()}


def contracts(events):
    result = {}
    for event in events:
        for market in event.get('markets', []):
            try:
                contract = parse_contract(market, event)
            except (ValueError, TypeError, KeyError, IndexError):
                raise SourceError('Polymarket: malformed points contract') from None
            if contract:
                result[contract['id']] = contract
    return list(result.values())


def event_spread(events, player):
    if player['game']['spread'] is not None:
        return player['game']['spread']
    teams = {t['team'].lower() for t in player['game']['teams']}
    possibilities = []
    for event in events:
        match = re.match(r'^nba-([a-z]+)-([a-z]+)-', event['slug'])
        if not match or set(match.groups()) != teams:
            continue
        for m in event.get('markets', []):
            if m.get('sportsMarketType') == 'spreads' and m.get('line') is not None:
                possibilities.append((float(m.get('volumeNum') or 0), abs(float(m['line']))))
    return max(possibilities, default=(0, None), key=lambda x: x[0])[1]


def quote(http, contract, stake):
    data = http.json(CLOB + '/book', {'token_id': contract['token']})
    asks = sorted((float(x['price']), float(x['size'])) for x in data.get('asks', []))
    asks = [(p, n) for p, n in asks if 0 < p < 1 and n > 0]
    if not asks:
        raise SourceError('Polymarket: no executable asks')
    price = asks[0][0]
    depth = sum(p*n for p, n in asks if p == price)
    if depth < stake:
        raise SourceError('Polymarket: insufficient depth at best ask')
    if stake / price < float(data.get('min_order_size') or 0):
        raise SourceError('Polymarket: unit below minimum order size')
    stamp = data.get('timestamp')
    if not stamp:
        raise SourceError('Polymarket: quote timestamp missing')
    quoted_at = datetime.fromtimestamp(float(stamp)/1000, timezone.utc)
    if not -60 <= (datetime.now(timezone.utc) - quoted_at).total_seconds() <= 900:
        raise SourceError('Polymarket: stale order book')
    # Zero-fee sports contracts are common. Unknown or nonzero fees are withheld
    # so a fee-free EV calculation cannot turn a losing contract into a pick.
    schedule = contract['fee_schedule']
    if contract['fee_enabled'] and (not schedule or float(schedule.get('rate', -1)) != 0):
        raise SourceError('Polymarket: nonzero or unknown fee schedule')
    return {'price': price, 'depth_usd': depth, 'timestamp': quoted_at.isoformat()}
