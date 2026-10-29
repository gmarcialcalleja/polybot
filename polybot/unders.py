"""Independent under model and auditable defender context; over model is untouched."""

from datetime import date
from statistics import mean

from .espn import CORE, WEB
from .http import SourceError
from .markets import available
from .model import Estimate


def estimate_under(stats, threshold, position, ranks, as_of, spread):
    season_end = as_of.year + (as_of.month >= 10)
    current = [g for g in stats.games if date(season_end-1, 10, 1) <= g.date < as_of]
    trailing = [g for g in stats.games if g.date < as_of][:82]
    strong = lambda g: 1 <= ranks[position].get(g.opponent, 0) <= 10
    cohort = [g for g in current if strong(g)]
    baseline = [g for g in trailing if strong(g)]
    minutes = mean(g.minutes for g in trailing[:5]) if trailing else 0
    if minutes < 24:
        raise SourceError('last-five-game minutes proxy below 24')
    if stats.usage <= .20:
        raise SourceError('usage rate is not above 20%')
    if not cohort:
        raise SourceError('no current-season games against top-10 positional defenses')
    # NO on 25+ means 24 or fewer, not 25 or fewer.
    hits = sum(g.points < threshold for g in cohort)
    raw = hits/len(cohort)
    p, weight, baseline_rate = raw, 1.0, None
    notes = ['Under model: historical games against currently top-10 positional defenses.',
             'Minutes eligibility uses the last 5 games, not a confirmed minutes projection.']
    if len(current) < 10:
        if not baseline or stats.prior_usage is None:
            raise SourceError('early-season baseline or previous-season usage missing')
        baseline_rate = sum(g.points < threshold for g in baseline)/len(baseline)
        weight = min(1.0, len(current)/10 * stats.usage/stats.prior_usage)
        p = weight*raw + (1-weight)*baseline_rate
        notes.append(f'Early-season blend: {weight:.0%} current / {1-weight:.0%} trailing baseline.')
        if len(trailing) < 82:
            notes.append(f'Trailing baseline has {len(trailing)} games (target 82).')
    if len(cohort) < 5:
        notes.append(f'LOW SAMPLE: {len(cohort)} current strong-defense matchups.')
    if spread is None:
        raise SourceError('game spread unavailable; cannot assess blowout risk')
    if spread > 10.5:
        notes.append('Large spread: blowout risk noted; no automatic under probability boost.')
    return Estimate(max(0, min(1, p)), raw, hits, len(cohort), len(current), weight,
                    baseline_rate, len(baseline), minutes, notes)


def recent_defensive_awards(data, season_end):
    result = []
    for award in data.get('awards', []):
        name = award.get('name', '')
        if award.get('league') != 'nba' or not ('All-Defensive' in name or name == 'Defensive Player of the Year'):
            continue
        years = [int(y) for y in award.get('seasons', []) if str(y).isdigit()
                 and season_end-2 <= int(y) <= season_end-1]
        if years:
            result.append(f'{name} {max(years)}')
    return result


def defensive_totals(data):
    values = {s['name']: s.get('value') for category in data.get('splits', {}).get('categories', [])
              for s in category.get('stats', [])}
    required = ('gamesPlayed', 'avgMinutes', 'avgBlocks')
    if any(values.get(k) is None for k in required):
        raise SourceError('ESPN: defensive production fields unavailable')
    return {k: float(values[k]) for k in required}


class DefenderContext:
    def __init__(self, http, provider, players, as_of):
        self.http, self.provider, self.players, self.as_of = http, provider, players, as_of
        self.cache = {}

    def for_opponent(self, opponent):
        if opponent in self.cache:
            return self.cache[opponent]
        year = self.as_of.year + (self.as_of.month >= 10)
        result = {'team': opponent, 'perimeter_defenders': [], 'shot_blockers': [],
                  'unavailable': [], 'data_gaps': [], 'confirmed_assignment': False,
                  'probability_adjustment': 0,
                  'method': 'Available rotation players; recent defensive honors and observed block production. No tracking-based POA grade.'}
        roster = [p for p in self.players.values() if p['team'] == opponent]
        for player in roster:
            name, aid = player['name'], player.get('id')
            if not available(player):
                result['unavailable'].append(name)
                continue
            try:
                current_url = f'{CORE}/seasons/{year}/types/2/athletes/{aid}/statistics'
                current = defensive_totals(self.http.json(current_url, ttl=3600))
                if current['gamesPlayed'] <= 0 or current['avgMinutes'] < 20:
                    continue
                position = player['position']
                if position not in ('PG', 'SG', 'SF', 'PF', 'C'):
                    position, _ = self.provider.position(name, self.as_of, aid)
                # Awards support a candidate perimeter defender, not a promised matchup.
                if position in ('PG', 'SG', 'SF'):
                    url = f'{WEB}/athletes/{aid}/bio'
                    awards = recent_defensive_awards(self.http.json(url, ttl=86400), year)
                    if awards:
                        result['perimeter_defenders'].append({'player': name, 'position': position,
                                                              'evidence': awards, 'source': url})
                sample, sample_year, source = current, year, current_url
                if current['gamesPlayed'] < 5:
                    source = f'{CORE}/seasons/{year-1}/types/2/athletes/{aid}/statistics'
                    sample = defensive_totals(self.http.json(source, ttl=86400))
                    sample_year = year-1
                if sample['gamesPlayed'] >= 5 and sample['avgMinutes'] >= 20 and sample['avgBlocks'] >= 1.5:
                    result['shot_blockers'].append({'player': name, 'blocks_per_game': sample['avgBlocks'],
                                                    'sample_games': int(sample['gamesPlayed']),
                                                    'season_end': sample_year, 'source': source})
            except (SourceError, KeyError, ValueError, TypeError) as exc:
                result['data_gaps'].append({'player': name, 'reason': str(exc) if isinstance(exc, SourceError)
                                            else 'Unsupported defender data schema'})
        result['shot_blockers'].sort(key=lambda p: p['blocks_per_game'], reverse=True)
        self.cache[opponent] = result
        return result


def context_notes(context):
    notes = []
    for player in context['perimeter_defenders'][:2]:
        notes.append(f"Perimeter evidence: {player['player']} ({'; '.join(player['evidence'])}); guarding assignment unconfirmed.")
    for player in context['shot_blockers'][:2]:
        notes.append(f"Available blocker: {player['player']} — {player['blocks_per_game']:.1f} BLK/game, "
                     f"{player['sample_games']} games, season ending {player['season_end']}.")
    if not context['perimeter_defenders'] and not context['shot_blockers']:
        notes.append('No qualifying individual defender evidence found; this is not proof the opponent lacks strong defenders.')
    if context['unavailable']:
        notes.append('Availability caution: unavailable/unconfirmed opponents: ' + ', '.join(context['unavailable'][:4]))
    if context['data_gaps']:
        notes.append(f"Defender details unavailable for {len(context['data_gaps'])} rotation/roster players.")
    notes.append('Defender context is supporting evidence only; no numerical probability boost or confirmed POA matchup.')
    return notes
