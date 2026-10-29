"""BeautifulSoup adapters for StatMuse and Hashtag Basketball."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import SourceError

POSITIONS = ('PG', 'SG', 'SF', 'PF', 'C')
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


def table_rows(soup):
    for table in soup.select('table'):
        headers = [c.get_text(' ', strip=True).upper() for c in table.select('thead th')]
        if not headers:
            first = table.find('tr')
            headers = [c.get_text(' ', strip=True).upper() for c in first.select('th')] if first else []
        yield table, headers


def parse_usage(html):
    soup = BeautifulSoup(html, 'html.parser')
    for table, headers in table_rows(soup):
        if 'USG%' not in headers:
            continue
        for row in table.select('tbody tr'):
            cells = row.select('td')
            if len(cells) != len(headers):
                continue
            try:
                usage = float(cells[headers.index('USG%')].get_text(strip=True)) / 100
            except ValueError:
                continue
            link = row.find('a', href=re.compile(r'^/nba/player/[^/]+$'))
            if link and 0 < usage < 1:
                return usage, urljoin('https://www.statmuse.com', link['href'])
    raise SourceError('StatMuse: usage percentage or canonical player link missing')


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


class StatMuse:
    def __init__(self, http):
        self.http = http

    def usage(self, name, season_end):
        label = f'{season_end - 1}-{str(season_end)[-2:]}'
        url = 'https://www.statmuse.com/nba/ask/' + slug(f'{name} usage percentage in {label}')
        usage, profile = parse_usage(self.http.get(url, ttl=6*3600))
        canonical_name = re.sub(r'-\d+$', '', profile.rsplit('/', 1)[-1])
        if normalize(canonical_name) != normalize(name):
            raise SourceError(f'StatMuse: player identity mismatch for {name}')
        return usage, profile

    def player(self, name, as_of):
        season_end = as_of.year + (as_of.month >= 10)
        usage, profile = self.usage(name, season_end)
        all_games = []
        for year in (season_end, season_end - 1, season_end - 2):
            html = self.http.get(profile + '/game-log', {'seasonYear': year}, ttl=6*3600)
            all_games.extend(parse_logs(html, as_of))
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
        return PlayerStats(name, usage, prior_usage, all_games, profile)


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
