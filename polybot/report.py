"""Discord output. Webhook secrets never enter logs or report artifacts."""

import re
from urllib.parse import urlparse

import requests

from .http import SourceError


def payload(report):
    picks = report['picks']
    status = report['status']
    summary = (f"NBA points EV report — {report['date']} ({report['timezone']})\n"
               f"Status: {status}. {report['contracts_found']} points contracts found; "
               f"{report['valued']} valued; {len(picks)} selected.\n")
    if not picks:
        summary += report.get('message', 'No qualifying positive-edge picks today.') + '\n'
    if report.get('error'):
        summary += report['error'] + '\n'
    summary += ('Model estimates, not guaranteed probabilities. Quarter-Kelly is shown as a model output.\n'
                'Injuries and lines are a morning snapshot; confirmed lineups are generally unavailable at 8 a.m.')
    embeds = []
    for rank, pick in enumerate(picks, 1):
        estimate = pick['estimate']
        embeds.append({
            'title': f"#{rank} {pick['player']} — {pick['threshold']}+ points",
            'url': pick['url'], 'color': 0x22AA77,
            'description': f"{pick['team']} · {pick['position']} vs {pick['opponent']} (DvP #{pick['defense_rank']})",
            'fields': [
                {'name': 'Ask / implied probability', 'value': f"${pick['price']:.3f} / {pick['price']:.1%}", 'inline': True},
                {'name': 'Model probability / edge', 'value': f"{estimate['probability']:.1%} / +{pick['edge']*100:.1f} pp", 'inline': True},
                {'name': 'EV / quarter-Kelly', 'value': f"{pick['ev_per_dollar']:+.1%} per $1 / {pick['fractional_kelly']:.1%} of bankroll", 'inline': True},
                {'name': 'Sample / liquidity', 'value': f"{estimate['hits']}/{estimate['sample']} current cohort hits; "
                 f"{estimate['baseline_sample']} baseline matchups; ${pick['depth_usd']:.2f} at ask", 'inline': False},
                {'name': 'Notes', 'value': '\n'.join(estimate['notes'])[:1024], 'inline': False},
            ],
            'footer': {'text': 'StatMuse game logs & usage · Hashtag Basketball DvP · ESPN availability · Polymarket CLOB'},
        })
    return {'content': summary[:2000], 'embeds': embeds, 'allowed_mentions': {'parse': []}}


def send(webhook, message):
    parsed = urlparse(webhook)
    if (parsed.scheme != 'https' or parsed.hostname not in ('discord.com', 'discordapp.com')
            or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_.-]+', parsed.path)
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise SourceError('DISCORD_WEBHOOK_URL must be a Discord HTTPS webhook URL')
    # Do not retry POSTs after ambiguous timeouts: that could duplicate messages.
    try:
        response = requests.post(webhook, params={'wait': 'true'}, json=message, timeout=(10, 30))
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', 'connection/timeout')
        raise SourceError(f'Discord delivery failed ({status}); webhook URL suppressed') from None
