import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .http import Http, SourceError
from .markets import (available, contracts, event_spread, nba_events, quote, rosters, slate)
from .model import estimate, top_picks, value
from .report import payload, send
from .stats import POSITIONS, StatMuse, defense, normalize, team


def analyze(http, day, tz, stake):
    report = {'date': str(day), 'timezone': str(tz), 'generated_at': datetime.now(timezone.utc).isoformat(),
              'status': 'ok', 'contracts_found': 0, 'valued': 0, 'picks': [], 'candidates': [],
              'skipped': [], 'sources': {}, 'unit_usd': stake}
    try:
        games = slate(http, day, tz)
        report['games'] = [{'teams': [s['team'] for s in g['teams']], 'start': g['start'].isoformat()}
                           for g in games]
        if not games:
            report['message'] = 'No NBA games scheduled today.'
            return report
        events = nba_events(http, day, tz)
        props = contracts(events)
        report['contracts_found'] = len(props)
        if not props:
            report['message'] = 'No open full-game player-points contracts are published for today yet.'
            return report
        ranks, source = defense(http, day)
        report['sources']['defense'] = source
        players = rosters(http, games)
        muse = StatMuse(http)
        stats_cache = {}
        stats_errors = {}
        candidates = []
        now = datetime.now(timezone.utc)
        for contract in props:
            name = contract['player']
            key = normalize(name)
            player = players.get(key)
            try:
                if not player:
                    raise SourceError('player name not matched to today\'s roster')
                if player['game']['start'] <= now or player['game']['state'] != 'pre':
                    raise SourceError('game already started or not in pregame state')
                expected_teams = {x['team'] for x in player['game']['teams']}
                if {team(t) for t in contract['teams']} != expected_teams:
                    raise SourceError('market teams do not match player\'s scheduled game')
                if not available(player):
                    raise SourceError('inactive, unconfirmed injury status, or minutes restriction')
                position = player['position']
                if position not in POSITIONS:
                    raise SourceError('primary PG/SG/SF/PF/C position unavailable')
                rank = ranks[position][player['opponent']]
                if rank < 21:
                    raise SourceError('opponent is not a bottom-10 positional defense')
                if key in stats_errors:
                    raise SourceError(stats_errors[key])
                if key not in stats_cache:
                    try:
                        stats_cache[key] = muse.player(player['name'], day)
                    except SourceError as exc:
                        stats_errors[key] = str(exc)
                        raise
                stats = stats_cache[key]
                model = estimate(stats, contract['threshold'], position, ranks, day,
                                 event_spread(events, player))
                market_quote = quote(http, contract, stake)
                metrics = value(model.probability, market_quote['price'])
                report['valued'] += 1
                candidate = {**contract, **market_quote, **metrics, 'player': player['name'],
                             'team': player['team'], 'position': position, 'opponent': player['opponent'],
                             'defense_rank': rank, 'estimate': asdict(model), 'statmuse_profile': stats.profile}
                # Sets are only internal matching metadata.
                candidate.pop('teams', None)
                candidates.append(candidate)
            except SourceError as exc:
                report['skipped'].append({'player': name, 'market': contract['id'], 'reason': str(exc)})
        report['candidates'] = candidates
        report['picks'] = top_picks(candidates)
        report['skipped_reasons'] = dict(Counter(s['reason'] for s in report['skipped']))
        if stats_errors:
            report['status'] = 'degraded'
            report['error'] = f'{len(stats_errors)} player stats fetches failed; see the report artifact.'
        if not report['picks']:
            report['message'] = 'No qualifying positive-edge picks after eligibility, sample, price and liquidity checks.'
        return report
    except SourceError as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        report['message'] = 'Data unavailable. No picks generated.'
        return report
    except (KeyError, TypeError, ValueError, IndexError):
        report['status'] = 'failed'
        report['error'] = 'A public source returned an unexpected schema; inspect the source adapters.'
        report['message'] = 'Data unavailable. No picks generated.'
        return report


def main():
    parser = argparse.ArgumentParser(description='NBA points EV report to Discord')
    parser.add_argument('--dry-run', action='store_true', help='Write report without posting to Discord')
    parser.add_argument('--date', type=date.fromisoformat, help='Today or a future slate, YYYY-MM-DD')
    parser.add_argument('--check-sources', action='store_true', help='Live stats/schema smoke check; never posts')
    args = parser.parse_args()
    tz = ZoneInfo(os.environ.get('REPORT_TIMEZONE', 'America/New_York'))
    today = datetime.now(tz).date()
    day = args.date or today
    if day < today:
        parser.error('Past-date analysis needs archived prices and DvP snapshots and is not supported.')
    if args.date and not args.dry_run and not args.check_sources:
        parser.error('--date is only supported with --dry-run')
    try:
        stake = float(os.environ.get('UNIT_USD', '10'))
        if not 0 < stake <= 100000:
            raise ValueError
    except ValueError:
        parser.error('UNIT_USD must be greater than 0 and at most 100000')
    http = Http()
    if args.check_sources:
        ranks, source = defense(http, today)
        stats = StatMuse(http).player('LeBron James', today)
        result = {'defense': source, 'positions': len(ranks), 'game_logs': len(stats.games),
                  'usage': stats.usage, 'prior_usage': stats.prior_usage, 'profile': stats.profile,
                  'nba_events_today': len(nba_events(http, today, tz))}
        print(json.dumps(result, indent=2))
        return 0
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not args.dry_run and not webhook:
        raise SourceError('Missing DISCORD_WEBHOOK_URL. Add it in GitHub Settings > Secrets and variables > Actions.')
    report = analyze(http, day, tz, stake)
    folder = Path('reports')
    folder.mkdir(exist_ok=True)
    (folder / f'{day}.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    message = payload(report)
    (folder / f'{day}-discord.json').write_text(json.dumps(message, indent=2, ensure_ascii=False), encoding='utf-8')
    print(message['content'])
    for pick in report['picks']:
        print(f"{pick['player']} {pick['threshold']}+ | edge +{pick['edge']*100:.1f} pp")
    if not args.dry_run:
        send(webhook, message)
        print('Discord report delivered.')
    return 1 if report['status'] in ('failed', 'degraded') else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SourceError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
