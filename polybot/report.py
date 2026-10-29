"""Discord output. Webhook secrets never enter logs or report artifacts."""

import re
from urllib.parse import urlparse

import requests

from .http import SourceError


def payload(report):
    picks = report['picks']
    roi_picks = report.get('high_roi_picks', [])
    status = report['status']
    summary = (f"NBA points EV report — {report['date']} ({report['timezone']})\n"
               f"Status: {status}. {report['contracts_found']} points contracts found; "
               f"{report['valued']} valued; {len(picks)} top-edge picks.\n"
               f"Estimated ROI >100%: {report.get('high_roi_contracts', 0)} qualifying contracts; "
               f"{len(roi_picks)} distinct-player alerts shown.\n")
    if not picks:
        summary += report.get('message', 'No qualifying positive-edge picks today.') + '\n'
    if report.get('error'):
        summary += report['error'] + '\n'
    reasons = sorted(report.get('skipped_reasons', {}).items(), key=lambda x: x[1], reverse=True)
    if reasons:
        summary += 'Main screening reasons:\n' + '\n'.join(f'- {count}: {reason[:180]}' for reason, count in reasons[:3]) + '\n'
    if not roi_picks:
        summary += ('ROI >100% could not be assessed because no contracts were valued.\n'
                    if not report['valued'] else 'No valued contracts have estimated ROI above 100%.\n')
    summary += ('Expected ROI = model probability / price − 1; win ROI = 1 / price − 1.\n'
                'Model estimates, not guaranteed returns. Quarter-Kelly is a model bankroll fraction.\n'
                'Availability and prices are a report-time snapshot; confirmed lineups may still be unavailable.')
    embeds = []
    entries = [(f'Top edge #{rank}', pick) for rank, pick in enumerate(picks, 1)]
    edge_ids = {p['id'] for p in picks}
    entries.extend((f'ROI alert #{rank}', pick) for rank, pick in enumerate(roi_picks, 1) if pick['id'] not in edge_ids)
    for label, pick in entries:
        estimate = pick['estimate']
        high_roi = pick['expected_roi'] > 1
        embeds.append({
            'title': f"{label}: {pick['player']} — {pick['threshold']}+ points" + (' | ROI >100%' if high_roi else ''),
            'url': pick['url'], 'color': 0xF0A020 if high_roi else 0x22AA77,
            'description': f"{pick['team']} · {pick['position']} vs {pick['opponent']} (DvP #{pick['defense_rank']})",
            'fields': [
                {'name': 'Ask / implied probability', 'value': f"${pick['price']:.3f} / {pick['price']:.1%}", 'inline': True},
                {'name': 'Model probability / edge', 'value': f"{estimate['probability']:.1%} / +{pick['edge']*100:.1f} pp", 'inline': True},
                {'name': 'Estimated expected ROI', 'value': f"{pick['expected_roi']:+.1%}" + (' — ABOVE 100%' if high_roi else ''), 'inline': True},
                {'name': 'Net ROI if contract wins', 'value': f"{pick['win_roi']:+.1%}; a loss returns −100%", 'inline': True},
                {'name': 'Quarter-Kelly', 'value': f"{pick['fractional_kelly']:.1%} of bankroll", 'inline': True},
                {'name': 'Sample / liquidity', 'value': f"{estimate['hits']}/{estimate['sample']} current cohort hits; "
                 f"{estimate['baseline_sample']} baseline matchups; ${pick['depth_usd']:.2f} at ask", 'inline': False},
                {'name': 'Notes', 'value': '\n'.join(estimate['notes'])[:400], 'inline': False},
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
