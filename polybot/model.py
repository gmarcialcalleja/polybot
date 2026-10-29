"""Guide formulas with explicit, auditable early-season assumptions."""

from dataclasses import dataclass
from datetime import date
from statistics import mean

from .http import SourceError


@dataclass
class Estimate:
    probability: float
    raw_hit_rate: float
    hits: int
    sample: int
    current_games: int
    current_weight: float
    baseline_hit_rate: float | None
    baseline_sample: int
    minutes_proxy: float
    notes: list[str]


def estimate(stats, threshold, position, ranks, as_of, spread):
    season_end = as_of.year + (as_of.month >= 10)
    season_start = date(season_end - 1, 10, 1)
    current = [g for g in stats.games if season_start <= g.date < as_of]
    trailing = [g for g in stats.games if g.date < as_of][:82]
    cohort = [g for g in current if ranks[position].get(g.opponent, 0) >= 21]
    baseline = [g for g in trailing if ranks[position].get(g.opponent, 0) >= 21]
    minutes = mean(g.minutes for g in trailing[:5]) if trailing else 0
    if minutes < 24:
        raise SourceError('last-five-game minutes proxy below 24')
    if stats.usage <= 0.20:
        raise SourceError('usage rate is not above 20%')
    if not cohort:
        raise SourceError('no current-season games against bottom-10 positional defenses')
    hits = sum(g.points >= threshold for g in cohort)
    raw = hits / len(cohort)
    notes = ['Minutes eligibility uses last 5 games, not a confirmed minutes projection.',
             'Historical opponents classified by today\'s DvP snapshot.']
    weight = 1.0
    baseline_rate = None
    p = raw
    if len(current) < 10:
        if not baseline or stats.prior_usage is None:
            raise SourceError('early-season baseline or previous-season usage missing')
        baseline_rate = sum(g.points >= threshold for g in baseline) / len(baseline)
        weight = min(1.0, len(current)/10 * stats.usage/stats.prior_usage)
        p = weight*raw + (1-weight)*baseline_rate
        notes.append(f'Early-season blend: {weight:.0%} current cohort / {1-weight:.0%} trailing baseline.')
        if len(trailing) < 82:
            notes.append(f'Trailing baseline has only {len(trailing)} games (target 82).')
    if len(cohort) < 5:
        notes.append(f'LOW SAMPLE: only {len(cohort)} current bottom-10 matchups.')
    if spread is None:
        raise SourceError('game spread unavailable; cannot apply blowout rule')
    if spread > 10.5:
        p -= 0.035
        notes.append('Blowout adjustment: minus 3.5 percentage points (absolute spread >10.5).')
    return Estimate(max(0, min(1, p)), raw, hits, len(cohort), len(current), weight,
                    baseline_rate, len(baseline), minutes, notes)


def value(probability, price, kelly_fraction=0.25, fee_per_share=0):
    cost = price + fee_per_share
    if not 0 < price < 1 or not 0 <= probability <= 1 or fee_per_share < 0 or cost >= 1:
        raise ValueError('invalid probability or contract price')
    edge = probability - cost
    return {'edge': edge, 'raw_edge': probability-price, 'cost_per_share': cost,
            'ev_per_dollar': probability/cost - 1,
            'expected_roi': probability/cost - 1, 'win_roi': 1/cost - 1,
            'fractional_kelly': max(0, edge/(1-cost))*kelly_fraction}


def high_roi_picks(candidates, count=3):
    """Strictly above 100% model expected ROI, independently ranked by ROI."""
    picks, seen = [], set()
    for pick in sorted(candidates, key=lambda p: p['expected_roi'], reverse=True):
        if pick['expected_roi'] > 1 and pick['player'] not in seen:
            picks.append(pick)
            seen.add(pick['player'])
        if len(picks) == count:
            break
    return picks


def top_picks(candidates, count=3):
    picks, seen = [], set()
    for pick in sorted(candidates, key=lambda p: p['edge'], reverse=True):
        if pick['edge'] > 0 and pick['player'] not in seen:
            picks.append(pick)
            seen.add(pick['player'])
        if len(picks) == count:
            break
    return picks
