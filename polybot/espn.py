"""Primary public ESPN player data; no StatMuse calls or credentials."""

import json
import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .http import SourceError
from .stats import TEAMS, PlayerStats, normalize, parse_espn_logs, slug, team

WEB = 'https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba'
CORE = 'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba'
SITE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
FANTASY = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba'
POSITION_IDS = {1: 'PG', 2: 'SG', 3: 'SF', 4: 'PF', 5: 'C'}


def season_rows(data):
    labels = data.get('labels', [])
    metadata = data.get('events', {})
    for season in data.get('seasonTypes', []):
        if not season.get('displayName', '').endswith(' Regular Season'):
            continue
        for category in season.get('categories', []):
            if category.get('type') == 'event':
                for row in category.get('events', []):
                    stats = row.get('stats', [])
                    if len(stats) == len(labels):
                        yield metadata.get(str(row.get('eventId')), {}), dict(zip(labels, stats))


def usage_segments(data, before):
    """Player attempts/turnovers grouped by actual team, including traded players."""
    segments = defaultdict(lambda: {'minutes': 0.0, 'used': 0.0})
    for event, values in season_rows(data):
        if (event.get('gameResult') not in ('W', 'L')
                or team(event.get('opponent', {}).get('abbreviation', '')) not in TEAMS):
            continue
        try:
            day = datetime.fromisoformat(event['gameDate'].replace('Z', '+00:00')).astimezone(
                ZoneInfo('America/New_York')).date()
            text = str(values['MIN'])
            minutes = float(text) if ':' not in text else float(text.split(':')[0])+float(text.split(':')[1])/60
            fga = float(values['FG'].split('-')[1])
            fta = float(values['FT'].split('-')[1])
            turnovers = float(values['TO'])
            side = event.get('atVs', '').lower()
            team_id = str(event['awayTeamId'] if side == '@' else event['homeTeamId'])
        except (KeyError, ValueError, IndexError, AttributeError):
            continue
        if side not in ('@', 'vs') or day >= before or minutes <= 0:
            continue
        if not all(math.isfinite(n) and n >= 0 for n in (minutes, fga, fta, turnovers)):
            continue
        segments[team_id]['minutes'] += minutes
        segments[team_id]['used'] += fga + .44*fta + turnovers
    return dict(segments)


def team_context(stats_data, schedule, season_end):
    stats = {s['name']: s.get('value') for category in stats_data.get('splits', {}).get('categories', [])
             for s in category.get('stats', [])}
    required = ('fieldGoalsAttempted', 'freeThrowsAttempted', 'turnovers', 'gamesPlayed')
    if any(stats.get(k) is None for k in required):
        raise SourceError('ESPN: team totals required for usage percentage are missing')
    games, court_minutes = 0, 0
    for event in schedule.get('events', []):
        if event.get('season', {}).get('year') != season_end or str(event.get('seasonType', {}).get('id')) != '2':
            continue
        competition = event.get('competitions', [{}])[0]
        status = competition.get('status', {})
        if not status.get('type', {}).get('completed'):
            continue
        period = status.get('period', 0)
        if not isinstance(period, int) or period < 4:
            raise SourceError('ESPN: completed game period unavailable for usage minutes')
        court_minutes += 48 + 5*(period-4)
        games += 1
    if not games or games != int(stats['gamesPlayed']):
        raise SourceError('ESPN: team schedule and season totals disagree on completed games')
    used = float(stats['fieldGoalsAttempted']) + .44*float(stats['freeThrowsAttempted']) + float(stats['turnovers'])
    if not math.isfinite(used) or used <= 0:
        raise SourceError('ESPN: invalid team possession totals')
    return used, court_minutes


class ESPN:
    def __init__(self, http):
        self.http = http
        self.positions = {}
        self.logs = {}
        self.teams = {}

    def position(self, name, as_of, athlete_id):
        year = as_of.year + (as_of.month >= 10)
        if year not in self.positions:
            url = f'{FANTASY}/seasons/{year}/players'
            data = self.http.json(url, {'view': 'players_wl'}, ttl=6*3600,
                                  headers={'X-Fantasy-Filter': json.dumps({'players': {'limit': 10000, 'offset': 0}})})
            if not isinstance(data, list):
                raise SourceError('ESPN: invalid public fantasy player list')
            self.positions[year] = {str(p['id']): p for p in data if p.get('id') is not None}
        athlete = self.positions[year].get(str(athlete_id), {})
        if normalize(athlete.get('fullName', '')) != normalize(name):
            raise SourceError(f'ESPN: fantasy player identity unavailable for {name}')
        position = POSITION_IDS.get(athlete.get('defaultPositionId'))
        if not position:
            raise SourceError(f'ESPN: specific primary position unavailable for {name}')
        return position, f'{FANTASY}/seasons/{year}/players?view=players_wl'

    def season(self, athlete_id, year):
        key = (str(athlete_id), year)
        if key not in self.logs:
            self.logs[key] = self.http.json(f'{WEB}/athletes/{athlete_id}/gamelog', {'season': year}, ttl=3600)
        return self.logs[key]

    def team(self, team_id, year):
        key = (str(team_id), year)
        if key not in self.teams:
            stats = self.http.json(f'{CORE}/seasons/{year}/types/2/teams/{team_id}/statistics', ttl=3600)
            schedule = self.http.json(f'{SITE}/teams/{team_id}/schedule',
                                      {'season': year, 'seasontype': 2}, ttl=3600)
            self.teams[key] = team_context(stats, schedule, year)
        return self.teams[key]

    def usage(self, athlete_id, year, as_of):
        segments = usage_segments(self.season(athlete_id, year), as_of)
        if not segments:
            raise SourceError('ESPN: player box scores required for usage percentage are unavailable')
        equivalent_minutes, minutes = 0.0, 0.0
        for team_id, segment in segments.items():
            team_used, team_court_minutes = self.team(team_id, year)
            equivalent_minutes += segment['used'] * team_court_minutes / team_used
            minutes += segment['minutes']
        usage = equivalent_minutes/minutes
        if not math.isfinite(usage) or not 0 < usage < 1:
            raise SourceError('ESPN: calculated usage percentage is invalid')
        return usage

    def player(self, name, as_of, athlete_id):
        if not athlete_id:
            raise SourceError(f'ESPN: athlete ID missing for {name}')
        self.position(name, as_of, athlete_id)  # identity-check the roster ID
        year = as_of.year + (as_of.month >= 10)
        usage = self.usage(athlete_id, year, as_of)
        all_games, sources = [], []
        for season in (year, year-1, year-2):
            data = self.season(athlete_id, season)
            games = parse_espn_logs(data, as_of)
            if games:
                all_games.extend(games)
                sources.append(f'ESPN regular-season game logs {season}')
            if len(all_games) >= 82:
                break
        all_games = sorted({(g.date, g.opponent): g for g in all_games}.values(), key=lambda g: g.date, reverse=True)
        if not all_games:
            raise SourceError(f'ESPN: completed game logs unavailable for {name}')
        try:
            prior_usage = self.usage(athlete_id, year-1, as_of)
        except SourceError:
            prior_usage = None
        profile = f'https://www.espn.com/nba/player/_/id/{athlete_id}/{slug(name)}'
        return PlayerStats(name, usage, prior_usage, all_games, profile, sources)
