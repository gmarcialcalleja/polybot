"""BeautifulSoup adapters for StatMuse and Hashtag Basketball."""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .http import SourceError

POSITIONS = ('PG', 'SG', 'SF', 'PF', 'C')
POSITION_NAMES = {'Point Guard': 'PG', 'Shooting Guard': 'SG', 'Small Forward': 'SF',
                  'Power Forward': 'PF', 'Center': 'C'}
TEAM_ALIASES = {'GS': 'GSW', 'NO': 'NOP', 'NY': 'NYK', 'PHO': 'PHX', 'SA': 'SAS',
                'BRK': 'BKN', 'CHO': 'CHA'}
TEAMS = {'ATL','BKN','BOS','CHA','CHI','CLE','DAL','DEN','DET','GSW','HOU','IND',
         'LAC','LAL','MEM','MIA','MIL','MIN','NOP','NYK','OKC','ORL','PHI','PHX',
         'POR','SAC','SAS','TOR','UTA','WAS'}


def team(value):
    value = value.strip().upper()
    return TEAM_ALIASES.get(value, value)


def normalize(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]', '', value)


def slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')


@dataclass(frozen=True)
class GameLog:
    date: date
    opponent: str
    points: int
    minutes: float


@dataclass
class PlayerStats:
    name: str
    usage: float
    prior_usage: float | None
    games: list[GameLog]
    profile: str
    log_sources: list[str] = field(default_factory=list)


def table_rows(soup):
    for table in soup.select('table'):
        headers = [c.get_text(' ', strip=True).upper() for c in table.select('thead th')]
        if not headers:
            first = table.find('tr')
            headers = [c.get_text(' ', strip=True).upper() for c in first.select('th')] if first else []
        yield table, headers


def parse_usage(html, name=None, season_end=None):
    soup = BeautifulSoup(html, 'html.parser')
    for table, headers in table_rows(soup):
        if 'USG%' not in headers:
            continue
        for row in table.select('tbody tr'):
            cells = row.select('td')
            if len(cells) != len(headers):
                continue
            if season_end is not None:
                label = f'{season_end-1}-{str(season_end)[-2:]}'
                if 'SEASON' not in headers or cells[headers.index('SEASON')].get_text(strip=True) != label:
                    continue
            try:
                usage = float(cells[headers.index('USG%')].get_text(strip=True)) / 100
            except ValueError:
                continue
            link = row.find('a', href=re.compile(r'^/nba/player/[^/]+$'))
            if link and name is not None:
                canonical = re.sub(r'-\d+$', '', link['href'].rsplit('/', 1)[-1])
                if normalize(canonical) != normalize(name):
                    continue
            if link and 0 < usage < 1:
                return usage, urljoin('https://www.statmuse.com', link['href'])
    # Some guest pages omit the table but still publish the answer in the H1.
    # Read that visible headline only; do not extract gated table data/scripts.
    heading = soup.find('h1')
    if heading and name is not None and season_end is not None:
        link = heading.find('a', href=re.compile(r'^/nba/player/[^/]+$'))
        match = re.fullmatch(r'.+? (?:has|had) a usage percentage of (\d+(?:\.\d+)?) '
                             r'(this season|in \d{4}-\d{2})\.', heading.get_text(' ', strip=True))
        now = datetime.now(ZoneInfo('America/New_York'))
        current_season = now.year + (now.month >= 10)
        period = f'in {season_end-1}-{str(season_end)[-2:]}'
        if link and match and (match[2] == period or (match[2] == 'this season' and season_end == current_season)):
            canonical = re.sub(r'-\d+$', '', link['href'].rsplit('/', 1)[-1])
            usage = float(match[1])/100
            if normalize(canonical) == normalize(name) and 0 < usage < 1:
                return usage, urljoin('https://www.statmuse.com', link['href'])
    raise SourceError('StatMuse: usage percentage or canonical player link missing')


def parse_position(html, name):
    """Use the identified player's profile heading, never a G/F guess or sidebar."""
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.find('h1')
    title = soup.find('title')
    if not heading or normalize(heading.get_text(' ', strip=True)) != normalize(name):
        raise SourceError(f'StatMuse: position profile identity mismatch for {name}')
    if title:
        text = title.get_text(' ', strip=True)
        for label, position in POSITION_NAMES.items():
            if text.endswith(f' {label} | StatMuse'):
                return position
    raise SourceError(f'StatMuse: specific primary position missing for {name}')


def parse_logs(html, before):
    soup = BeautifulSoup(html, 'html.parser')
    games = {}
    for table, headers in table_rows(soup):
        if not {'DATE', 'OPP', 'PTS', 'MIN'}.issubset(headers):
            continue
        # The profile page also includes playoffs: use regular-season month tables only.
        heading = table.find_previous(['h2', 'h3'])
        if heading and any(x in heading.get_text().lower() for x in
                           ('round', 'final', 'play-in', 'preseason')):
            continue
        for row in table.select('tbody tr'):
            cells = row.select('td')
            if len(cells) != len(headers):
                continue
            values = dict(zip(headers, cells))
            anchor = values['DATE'].find('a', href=True)
            match = re.search(r'/game/(\d{1,2})-(\d{1,2})-(\d{4})-', anchor['href'] if anchor else '')
            try:
                if match:
                    month, day, year = map(int, match.groups())
                    played = date(year, month, day)
                else:
                    played = datetime.strptime(values['DATE'].get_text(strip=True), '%m/%d/%Y').date()
                minutes_text = values['MIN'].get_text(strip=True)
                minutes = (float(minutes_text.split(':')[0]) + float(minutes_text.split(':')[1])/60
                           if ':' in minutes_text else float(minutes_text))
                points = int(values['PTS'].get_text(strip=True))
                opponent = team(values['OPP'].get_text(' ', strip=True))
            except (ValueError, IndexError):
                continue
            if played < before and opponent in TEAMS and minutes > 0:
                games[(played, opponent)] = GameLog(played, opponent, points, minutes)
    return sorted(games.values(), key=lambda g: g.date, reverse=True)


def parse_espn_logs(data, before):
    """Public ESPN game logs; regular-season, played, completed games only."""
    labels = data.get('labels', [])
    if not {'MIN', 'PTS'}.issubset(labels):
        if 'filters' in data and not data.get('events') and not data.get('seasonTypes'):
            return []  # Valid empty season, e.g. before this player's NBA debut.
        raise SourceError('ESPN: game-log labels missing')
    metadata = data.get('events', {})
    games = {}
    for season in data.get('seasonTypes', []):
        if not season.get('displayName', '').endswith(' Regular Season'):
            continue
        for category in season.get('categories', []):
            if category.get('type') != 'event':
                continue
            for entry in category.get('events', []):
                event = metadata.get(str(entry.get('eventId')), {})
                values = entry.get('stats', [])
                if len(values) != len(labels) or event.get('gameResult') not in ('W', 'L'):
                    continue
                try:
                    played = datetime.fromisoformat(event['gameDate'].replace('Z', '+00:00')).astimezone(
                        ZoneInfo('America/New_York')).date()
                    minutes_text = str(values[labels.index('MIN')])
                    minutes = (float(minutes_text.split(':')[0]) + float(minutes_text.split(':')[1])/60
                               if ':' in minutes_text else float(minutes_text))
                    points = int(values[labels.index('PTS')])
                    opponent = team(event['opponent']['abbreviation'])
                except (KeyError, ValueError, IndexError):
                    continue
                if played < before and minutes > 0 and opponent in TEAMS:
                    games[(played, opponent)] = GameLog(played, opponent, points, minutes)
    return sorted(games.values(), key=lambda g: g.date, reverse=True)


class StatMuse:
    def __init__(self, http):
        self.http = http
        self.usage_cache = {}
        self.position_cache = {}

    def usage(self, name, season_end):
        key = (normalize(name), season_end)
        if key in self.usage_cache:
            return self.usage_cache[key]
        label = f'{season_end - 1}-{str(season_end)[-2:]}'
        failures = []
        for wording in ('usage percentage', 'usage rate'):
            url = 'https://www.statmuse.com/nba/ask/' + slug(f'{name} {wording} in {label}')
            try:
                result = parse_usage(self.http.get(url, ttl=6*3600), name, season_end)
                self.usage_cache[key] = result
                return result
            except SourceError as exc:
                failures.append(str(exc))
        raise SourceError(f'StatMuse: usage unavailable for {name} in {label}; {failures[-1]}')

    def position(self, name, as_of):
        key = normalize(name)
        if key not in self.position_cache:
            _, profile = self.usage(name, as_of.year + (as_of.month >= 10))
            html = self.http.get(profile, ttl=6*3600)
            self.position_cache[key] = parse_position(html, name), profile
        return self.position_cache[key]

    def player(self, name, as_of, athlete_id=None):
        season_end = as_of.year + (as_of.month >= 10)
        usage, profile = self.usage(name, season_end)
        all_games = []
        sources = []
        for year in (season_end, season_end - 1, season_end - 2):
            games = []
            try:
                html = self.http.get(profile + '/game-log', {'seasonYear': year}, ttl=6*3600)
                games = parse_logs(html, as_of)
            except SourceError:
                pass
            source = f'StatMuse season {year}'
            if not games and athlete_id:
                url = f'https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog'
                data = self.http.json(url, {'season': year}, ttl=6*3600)
                games = parse_espn_logs(data, as_of)
                source = f'ESPN public fallback season {year}'
            if games:
                sources.append(source)
                all_games.extend(games)
            if len(all_games) >= 82:
                break
        all_games = sorted({(g.date, g.opponent): g for g in all_games}.values(),
                           key=lambda g: g.date, reverse=True)
        if not all_games:
            raise SourceError(f'StatMuse: no completed regular-season logs for {name}')
        try:
            prior_usage, _ = self.usage(name, season_end - 1)
        except SourceError:
            prior_usage = None
        return PlayerStats(name, usage, prior_usage, all_games, profile, sources)


def parse_defense(html, as_of):
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)
    updated = re.search(r'Updated:\s*(\d{1,2} \w+ \d{4})', text)
    if not updated:
        raise SourceError('DvP: source update date missing')
    stamp = datetime.strptime(updated.group(1), '%d %B %Y').date()
    if not 0 <= (as_of - stamp).days <= 3:
        raise SourceError(f'DvP: stale or future source ({stamp})')
    values = {p: {} for p in POSITIONS}
    for row in soup.select('table tr'):
        cells = row.select('td')
        if len(cells) < 3:
            continue
        position = cells[0].get_text(strip=True)
        if position not in values:
            continue
        # Badges rank all 150 position/team combinations. Extract values and rerank
        # separately within each position, from fewest to most points allowed.
        team_span = cells[1].find('span', id=True)
        points_span = cells[2].find('span', id=True)
        if team_span and points_span:
            opponent = team(team_span.get_text(strip=True))
            try:
                points = float(points_span.get_text(strip=True))
            except ValueError:
                continue
            if opponent in TEAMS:
                values[position][opponent] = points
    if any(len(v) != 30 for v in values.values()):
        raise SourceError('DvP: expected all 30 teams for each of PG, SG, SF, PF, C')
    ranks = {p: {t: i for i, (t, _) in enumerate(sorted(v.items(), key=lambda x: (x[1], x[0])), 1)}
             for p, v in values.items()}
    return ranks, str(stamp)


def defense(http, as_of):
    # The public default is the complete current season, allowed by the guide.
    url = 'https://hashtagbasketball.com/nba-defense-vs-position'
    html = http.get(url, ttl=3600)
    ranks, updated = parse_defense(html, as_of)
    return ranks, {'url': url, 'updated': updated, 'window': 'current full season; points per 48 minutes'}
