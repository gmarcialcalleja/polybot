"""Discord output. Webhook secrets never enter logs or report artifacts."""

import re
from urllib.parse import urlparse

import requests

from .http import SourceError


def bet_label(pick):
    if pick.get('side') == 'under':
        return f"Under {pick['threshold']-.5:g} points ({pick['threshold']-1} or fewer)"
    return f"{pick['threshold']}+ points (OVER)"


def payloads(report):
    if 'venues' not in report:
        return [payload(report)]
    messages = []
    for data in report['venues'].values():
        sections = [{**data, 'direction': 'over', 'valued': data.get('over_valued', data['valued'])}]
        if 'unders' in data:
            sections.append(data['unders'])
        for section in sections:
            combined = {**report, **section}
            if report['status'] == 'failed' and report.get('error'):
                combined.update(status='failed', error=report['error'], message=report['message'])
            elif not report.get('games'):
                combined['message'] = report.get('message', 'No NBA games scheduled today.')
            messages.append(payload(combined))
    return messages


def payload(report):
    picks = report['picks']
    roi_picks = report.get('high_roi_picks', [])
    status = report['status']
    venue = report.get('venue', 'Polymarket')
    direction = report.get('direction', 'over').upper()
    summary = (f"{venue} NBA points EV report — {direction} — {report['date']} ({report['timezone']})\n"
               f"Status: {status}. {report['contracts_found']} points contracts found; "
               f"{report['valued']} valued; {len(picks)} top-edge picks.\n"
               f"Estimated ROI >100%: {report.get('high_roi_contracts', 0)} qualifying contracts; "
               f"{len(roi_picks)} distinct-player alerts shown.\n")
    if not picks:
        summary += report.get('message', 'No qualifying positive-edge picks today.') + '\n'
    if report.get('error'):
        summary += report['error'] + '\n'
    if report.get('conflicts_with_overs'):
        summary += f"{report['conflicts_with_overs']} under options withheld to avoid opposing an over recommendation.\n"
    if report.get('defensive_matchups'):
        matchups = report['defensive_matchups']
        summary += 'Strong-defense matchups checked: ' + ', '.join(c['team'] for c in matchups) + '.\n'
        perimeter = list(dict.fromkeys(p['player'] for c in matchups for p in c['perimeter_defenders']))
        blockers = list(dict.fromkeys(p['player'] for c in matchups for p in c['shot_blockers']))
        if perimeter:
            summary += 'Available perimeter award honorees: ' + ', '.join(perimeter[:4]) + ' (assignments unconfirmed).\n'
        if blockers:
            summary += 'Available shot blockers flagged: ' + ', '.join(blockers[:4]) + '.\n'
        summary += 'Defender context supports review; it does not add a probability boost.\n'
    reasons = sorted(report.get('skipped_reasons', {}).items(), key=lambda x: x[1], reverse=True)
    if reasons:
        summary += 'Main screening reasons:\n' + '\n'.join(f'- {count}: {reason[:180]}' for reason, count in reasons[:3]) + '\n'
    if not roi_picks:
        summary += ('ROI >100% could not be assessed because no contracts were valued.\n'
                    if not report['valued'] else 'No valued contracts have estimated ROI above 100%.\n')
    summary += ('ROI uses price plus entry fees; winning payout is $1 per contract.\n'
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
            'title': f"{label}: {pick['player']} — {bet_label(pick)}" + (' | ROI >100%' if high_roi else ''),
            'url': pick['url'], 'color': 0xF0A020 if high_roi else 0x22AA77,
            'description': f"{venue} · {pick['team']} · {pick['position']} vs {pick['opponent']} (DvP #{pick['defense_rank']})",
            'fields': [
                {'name': 'Ask / implied probability', 'value': f"${pick['price']:.3f} / {pick['price']:.1%}", 'inline': True},
                {'name': 'Model probability / edge after fees', 'value': f"{estimate['probability']:.1%} / +{pick['edge']*100:.1f} pp", 'inline': True},
                {'name': 'Entry cost including fees', 'value': f"${pick.get('cost_per_share', pick['price']):.4f} per share", 'inline': True},
                {'name': 'Estimated expected ROI', 'value': f"{pick['expected_roi']:+.1%}" + (' — ABOVE 100%' if high_roi else ''), 'inline': True},
                {'name': 'Net ROI if contract wins', 'value': f"{pick['win_roi']:+.1%}; a loss returns −100%", 'inline': True},
                {'name': 'Quarter-Kelly', 'value': f"{pick['fractional_kelly']:.1%} of bankroll", 'inline': True},
                {'name': 'Sample / liquidity', 'value': f"{estimate['hits']}/{estimate['sample']} current cohort hits; "
                 f"{estimate['baseline_sample']} baseline matchups; ${pick['depth_usd']:.2f} at ask", 'inline': False},
                {'name': 'Notes', 'value': '\n'.join(([pick['settlement_note']] if pick.get('settlement_note') else []) + estimate['notes'])[:400], 'inline': False},
            ],
            'footer': {'text': f'ESPN stats & availability · Hashtag Basketball DvP · {venue} order book'},
        })
        context = pick.get('defender_context')
        if context:
            from .unders import context_notes
            embeds[-1]['fields'].append({'name': 'Strong-defense evidence',
                                         'value': '\n'.join(context_notes(context))[:650], 'inline': False})
    # At most six cards per side message. Share Discord's 6000-character embed
    # budget across them so additional defender context cannot break delivery.
    def embed_length(embed):
        return (len(embed['title']) + len(embed['description']) + len(embed['footer']['text'])
                + sum(len(f['name'])+len(f['value']) for f in embed['fields']))
    while sum(embed_length(e) for e in embeds) > 5900:
        fields = [f for e in embeds for f in e['fields'] if f['name'] in ('Notes', 'Strong-defense evidence')]
        longest = max(fields, key=lambda f: len(f['value']))
        if len(longest['value']) <= 80:
            break
        longest['value'] = longest['value'][:-80] + '…'
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
