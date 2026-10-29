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
from . import kalshi
from .espn import ESPN
from .markets import (available, contracts, event_spread, nba_events, quote, rosters, slate)
from .model import estimate, high_roi_picks, top_picks, value
from .report import bet_label, payloads, send
from .stats import POSITIONS, defense, normalize, team
from .unders import DefenderContext, context_notes, estimate_under


def analyze(http, day, tz, stake):
    report = {'date': str(day), 'timezone': str(tz), 'generated_at': datetime.now(timezone.utc).isoformat(),
              'status': 'ok', 'contracts_found': 0, 'valued': 0, 'picks': [], 'candidates': [],
              'skipped': [], 'sources': {}, 'unit_usd': stake, 'high_roi_picks': [],
              'high_roi_contracts': 0, 'under_picks': [], 'venues': {
                  name: {'venue': name, 'status': 'ok', 'contracts_found': 0, 'valued': 0,
                         'picks': [], 'high_roi_picks': [], 'high_roi_contracts': 0}
                  for name in ('Polymarket', 'Kalshi')}}
    try:
        games = slate(http, day, tz)
        report['games'] = [{'teams': [s['team'] for s in g['teams']], 'start': g['start'].isoformat()}
                           for g in games]
        if not games:
            report['message'] = 'No NBA games scheduled today.'
            return report
        events, props = [], []
        for venue in report['venues']:
            try:
                if venue == 'Polymarket':
                    events = nba_events(http, day, tz)
                    discovered = contracts(events)
                else:
                    discovered = kalshi.contracts(http, day)
                report['venues'][venue]['contracts_found'] = len({c['id'] for c in discovered})
                report['venues'][venue]['over_options'] = sum(c.get('side', 'over') == 'over' for c in discovered)
                report['venues'][venue]['under_options'] = sum(c.get('side') == 'under' for c in discovered)
                props.extend(discovered)
            except (SourceError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                report['venues'][venue]['status'] = 'failed'
                report['venues'][venue]['error'] = (str(exc) if isinstance(exc, SourceError)
                                                    else f'{venue}: unexpected discovery schema')
        report['contracts_found'] = sum(d['contracts_found'] for d in report['venues'].values())
        if not props:
            report['message'] = 'No open full-game player-points contracts are published for today yet.'
            return finalize(report)
        ranks, source = defense(http, day)
        report['sources']['defense'] = source
        players = rosters(http, games)
        stats_provider = ESPN(http)
        defenders = DefenderContext(http, stats_provider, players, day)
        stats_cache = {}
        stats_errors = {}
        position_cache = {}
        position_errors = {}
        candidates = []
        now = datetime.now(timezone.utc)
        print(f"Screening {len(props)} contracts across both exchanges...", flush=True)
        for contract in props:
            name = contract['player']
            venue = contract['venue']
            side = contract.get('side', 'over')
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
                position_source = 'ESPN roster'
                if position not in POSITIONS:
                    if key in position_errors:
                        raise SourceError(position_errors[key])
                    if key not in position_cache:
                        try:
                            position_cache[key] = stats_provider.position(player['name'], day, player.get('id'))
                        except SourceError as exc:
                            position_errors[key] = str(exc)
                            stats_errors[key] = str(exc)
                            raise
                    position, position_source = position_cache[key]
                rank = ranks[position][player['opponent']]
                if side == 'over' and rank < 21:
                    raise SourceError('opponent is not a bottom-10 positional defense')
                if side == 'under' and not 1 <= rank <= 10:
                    raise SourceError('opponent is not a top-10 positional defense')
                if key in stats_errors:
                    raise SourceError(stats_errors[key])
                if key not in stats_cache:
                    try:
                        print(f"Loading game history: {player['name']}", flush=True)
                        stats_cache[key] = stats_provider.player(player['name'], day, player.get('id'))
                    except SourceError as exc:
                        stats_errors[key] = str(exc)
                        raise
                stats = stats_cache[key]
                estimator = estimate_under if side == 'under' else estimate
                model = estimator(stats, contract['threshold'], position, ranks, day,
                                  event_spread(events, player))
                model.notes.insert(0, 'ESPN game logs; usage estimated from box scores and team totals (rounded player minutes).')
                market_quote = (kalshi.quote(http, contract, stake) if venue == 'Kalshi'
                                else quote(http, contract, stake))
                metrics = value(model.probability, market_quote['price'],
                                fee_per_share=market_quote.get('fee_per_share', 0))
                defender_context = None
                if side == 'under':
                    defender_context = defenders.for_opponent(player['opponent'])
                    model.notes.extend(context_notes(defender_context))
                report['valued'] += 1
                candidate = {**contract, **market_quote, **metrics, 'side': side,
                             'selection_id': f"{venue}:{contract['id']}:{side}", 'player': player['name'],
                             'team': player['team'], 'position': position, 'opponent': player['opponent'],
                             'position_source': position_source, 'defense_rank': rank,
                             'estimate': asdict(model), 'stats_profile': stats.profile,
                             'stats_provider': 'ESPN', 'usage': stats.usage, 'prior_usage': stats.prior_usage,
                             'game_log_sources': stats.log_sources, 'defender_context': defender_context}
                # Sets are only internal matching metadata.
                candidate.pop('teams', None)
                candidates.append(candidate)
            except SourceError as exc:
                report['skipped'].append({'player': name, 'market': contract['id'],
                                          'venue': venue, 'side': side, 'reason': str(exc)})
            except (KeyError, TypeError, ValueError, ArithmeticError):
                report['skipped'].append({'player': name, 'market': contract['id'], 'venue': venue, 'side': side,
                                          'reason': f'{venue}: unexpected contract or quote schema'})
                report['venues'][venue]['status'] = 'degraded'
                report['venues'][venue]['error'] = 'A contract or quote had an unsupported schema.'
        report['candidates'] = candidates
        if stats_errors:
            for venue, data in report['venues'].items():
                if data['status'] != 'failed' and any(s['venue'] == venue and normalize(s['player']) in stats_errors
                                                     for s in report['skipped']):
                    data['status'] = 'degraded'
                    data['error'] = 'Some player stats fetches failed; see the report artifact.'
        return finalize(report)
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


def finalize(report):
    """Choose separate top-three lists and preserve failures on either venue."""
    over_players = {normalize(p['player']) for p in report['candidates']
                    if p.get('side', 'over') == 'over' and p['edge'] > 0}
    for venue, data in report['venues'].items():
        candidates = [p for p in report['candidates'] if p['venue'] == venue]
        overs = [p for p in candidates if p.get('side', 'over') == 'over']
        unders = [p for p in candidates if p.get('side') == 'under']
        safe_unders = [p for p in unders if normalize(p['player']) not in over_players]
        data['valued'] = len(candidates)
        data['over_valued'] = len(overs)
        data['picks'] = top_picks(overs)
        data['high_roi_picks'] = high_roi_picks(overs)
        data['high_roi_contracts'] = sum(p['expected_roi'] > 1 for p in overs)
        data['skipped_reasons'] = dict(Counter(s['reason'] for s in report['skipped']
                                               if s['venue'] == venue and s.get('side', 'over') == 'over'))
        data['unders'] = {'direction': 'under', 'venue': venue, 'status': data['status'],
                          'contracts_found': data.get('under_options', 0), 'valued': len(unders),
                          'picks': top_picks(safe_unders), 'high_roi_picks': high_roi_picks(safe_unders),
                          'high_roi_contracts': sum(p['expected_roi'] > 1 for p in safe_unders),
                          'conflicts_with_overs': len(unders)-len(safe_unders),
                          'defensive_matchups': list({p['opponent']: p['defender_context'] for p in unders
                                                       if p.get('defender_context')}.values()),
                          'skipped_reasons': dict(Counter(s['reason'] for s in report['skipped']
                                                   if s['venue'] == venue and s.get('side') == 'under'))}
        if data.get('error'):
            data['unders']['error'] = data['error']
        data['unders']['message'] = ('No open under/NO options are published for today yet.'
                                     if not data['unders']['contracts_found'] else
                                     'No under options could be valued; screening reasons are listed below.'
                                     if not unders else 'No qualifying positive-edge under picks after fees and conflict checks.')
        data['message'] = ('Data unavailable for this exchange.' if data['status'] == 'failed' else
                           'No open full-game player-points contracts are published for today yet.'
                           if not data['contracts_found'] else
                           'No contracts could be valued; screening reasons are listed below.' if not overs else
                           'No positive-edge picks among the contracts that could be valued.')
    report['picks'] = [p for data in report['venues'].values() for p in data['picks']]
    report['under_picks'] = [p for data in report['venues'].values() for p in data['unders']['picks']]
    report['high_roi_picks'] = [p for data in report['venues'].values() for p in data['high_roi_picks']]
    report['high_roi_contracts'] = sum(d['high_roi_contracts'] for d in report['venues'].values())
    report['skipped_reasons'] = dict(Counter(s['reason'] for s in report['skipped']))
    statuses = [d['status'] for d in report['venues'].values()]
    report['status'] = ('failed' if all(s == 'failed' for s in statuses) else
                        'degraded' if any(s != 'ok' for s in statuses) else 'ok')
    return report


def main():
    parser = argparse.ArgumentParser(description='NBA points EV report to Discord')
    parser.add_argument('--dry-run', action='store_true', help='Write report without posting to Discord')
    parser.add_argument('--date', type=date.fromisoformat, help='Today or a future slate, YYYY-MM-DD')
    parser.add_argument('--check-sources', action='store_true', help='Live stats/schema smoke check; never posts')
    parser.add_argument('--strict', action='store_true', help='Also exit nonzero for partial/degraded data')
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
        provider = ESPN(http)
        stats = provider.player('LeBron James', today, '1966')
        result = {'defense': source, 'positions': len(ranks), 'game_logs': len(stats.games),
                  'game_log_sources': stats.log_sources,
                  'usage': stats.usage, 'prior_usage': stats.prior_usage, 'profile': stats.profile,
                  'nba_events_today': len(nba_events(http, today, tz)),
                  'kalshi_points_contracts_today': len(kalshi.contracts(http, today)),
                  'position_check': provider.position('Donovan Mitchell', today, '3908809'),
                  'stats_provider': 'ESPN'}
        print(json.dumps(result, indent=2))
        return 0
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not args.dry_run and not webhook:
        raise SourceError('Missing DISCORD_WEBHOOK_URL. Add it in GitHub Settings > Secrets and variables > Actions.')
    report = analyze(http, day, tz, stake)
    folder = Path('reports')
    folder.mkdir(exist_ok=True)
    (folder / f'{day}.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    messages = payloads(report)
    (folder / f'{day}-discord.json').write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding='utf-8')
    for message in messages:
        print(message['content'])
    for pick in report['picks'] + report.get('under_picks', []):
        print(f"{pick['venue']} {pick['player']} {bet_label(pick)} | edge +{pick['edge']*100:.1f} pp")
    if not args.dry_run:
        for message in messages:
            send(webhook, message)
        print('Discord exchange reports delivered.')
    if report['status'] == 'degraded':
        print('::warning::Report completed with partial data; inspect exchange statuses and screening reasons.')
    return 1 if report['status'] == 'failed' or (args.strict and report['status'] == 'degraded') else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SourceError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
