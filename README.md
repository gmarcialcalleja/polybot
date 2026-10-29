# Polybot: daily NBA points EV report

Python bot using **ESPN as the primary player-data source**, with BeautifulSoup for the positional-defense table. It posts separate **OVER and UNDER reports for Polymarket and Kalshi** to a Discord channel (four messages when markets are processed). Each side on each exchange gets up to three distinct-player opportunities plus up to three distinct-player **estimated ROI above 100%** alerts. GitHub Actions schedules it for **2:00 p.m. America/New_York every day**, including daylight saving time. GitHub can queue or delay scheduled jobs, so delivery is shortly after the scheduled start, not guaranteed at exactly 14:00. Reports are still sent when no games, no player contracts, or no qualifying picks are available.

## Add your Discord webhook privately

1. In the destination Discord server, open the channel's **Edit Channel → Integrations → Webhooks → New Webhook**, select the channel, and copy the webhook URL. You need permission to manage webhooks.
2. Open [this repository's Actions secrets](https://github.com/gmarcialcalleja/polybot/settings/secrets/actions).
3. Select **New repository secret**. Name it exactly **`DISCORD_WEBHOOK_URL`** and paste the entire URL as its value.
4. Open **Actions → Daily NBA Discord report → Run workflow**. Keep `dry_run` checked for live-source diagnostics and a preview artifact; uncheck it to deliver to Discord.

You do not need to send anyone the URL, commit it, or add a separate Discord bot token. Neither Polymarket nor Kalshi requires an API key for the public data used here. This program only reads data and publishes reports; it never places trades. Both exchange reports use the same existing `DISCORD_WEBHOOK_URL` secret.

Optional: in **Settings → Secrets and variables → Actions → Variables**, add `UNIT_USD` (default `10`). This is the dollar size used to check ask depth and minimum order size. It does not move money.

## Data and model

The supplied NBA Points Prop EV Analyzer guide is the algorithm reference, not a source of live picks. Its October 29 case-study names, prices, ranks and probabilities are never used as data.

* **Slate and availability:** ESPN's public NBA scoreboard and current team rosters. Only today's unstarted games qualify. OUT, DOUBTFUL, QUESTIONABLE, inactive and explicitly minute-restricted players are withheld. If the roster supplies generic G/F, the specific PG/SG/SF/PF/C position comes from ESPN's public fantasy player list, matched by athlete ID and name. Generic positions are never guessed. The artifact records the position source.
* **Primary player data:** ESPN's public athlete game-log feed supplies completed regular-season games, with DNPs, preseason, playoffs and All-Star exhibitions excluded. UTC tip-off times are converted to Eastern calendar dates before filtering. Up to three seasons supply the trailing baseline. Each season's source is retained in the JSON artifact. No StatMuse webpage, API, account or subscription is used by the report pipeline. Legacy StatMuse parser code/tests are retained but are not called by the bot.
* **Usage percentage estimate:** ESPN's `usageRate` field is a different per-40-minute statistic and is **not** substituted for USG%. Instead, calculate an estimate from ESPN box-score FGA, FTA, turnovers and player minutes, plus team season totals and regulation/overtime minutes. For a single-team season: `usage = (player FGA + 0.44×FTA + TO) × team court minutes / (player minutes × (team FGA + 0.44×FTA + TO))`. Court minutes are one-player time (48 per game plus 5 per overtime), equivalent to total team-player minutes divided by five. For traded players, compute the numerator separately for each actual team before dividing by total player minutes. ESPN's rounded player minutes can cause small differences versus other providers; this is disclosed on picks. Team season totals and completed schedule counts must agree. Missing usage data is withheld, never filled with zeros or a prior-season value. Current usage must exceed 20%; early-season blending also requires prior usage.
* **Defense vs position:** [Hashtag Basketball](https://hashtagbasketball.com/nba-defense-vs-position), current full-season points allowed per 48 minutes. Teams are independently ranked 1–30 for each position; the page's global 150-row badges are **not** used as positional ranks. Opponents ranked 21–30 qualify. All 30 teams and five positions must be present; a snapshot older than three days is rejected.
* **Probability:** current-season milestone hits divided by games against the currently bottom-10 defenses. Historical opponents use today's DvP classification, not an archived game-day ranking. Cohorts below five games carry a low-sample flag; zero current cohort games are withheld.
* **Early season:** the guide does not prescribe exact blend weights. For fewer than 10 current-season appearances, this implementation uses `w = min(1, current_games / 10 × current_usage / prior_usage)`. The model probability is `w × current_cohort_hit_rate + (1−w) × baseline_hit_rate`. Baseline means the bottom-10 subset of the trailing 82 regular-season appearances, including current-season games. Fewer than 82 available games are disclosed. These are implementation assumptions, not a validated forecasting model.
* **Minutes:** last-five-game average minutes must be at least 24. This is a minutes **proxy**, not an independent projected-minutes feed. Report-time injury/roster data cannot establish confirmed starting lineups or catch every restriction. The report explicitly says so. Usage must exceed 20%.
* **Blowout:** subtract 0.035 from probability when the absolute game spread exceeds 10.5. Use ESPN's spread when available, otherwise the highest-volume Polymarket spread for that matchup. Missing spreads are withheld.
* **Markets:** public Gamma NBA discovery, fully paginated within the date window, then CLOB order books for full-game `points` contracts. Half-point overs such as 24.5 translate to 25+; YES and NO tokens are selected by outcome label rather than assumed array position. NO on 25+ is under 24.5 (24 or fewer), not 25 or fewer. Integer O/U lines, combinations, quarters, team totals and unsupported wording are excluded. If player props have not been listed by report time, the report says so.
* **Price:** best executable ask for the actual YES/over or NO/under token, with at least `UNIT_USD` available at that price and enough shares to meet the venue's minimum order. The NO ask is never inferred as one minus the YES ask. Stale books and nonzero/unknown Polymarket fee schedules are withheld. No midpoint is presented as an executable buy price.
* **Ranking:** `cost = ask + entry_fee_per_share`; `edge = probability − cost`. Retain strictly positive edges and choose up to three distinct players by descending edge **separately for each exchange**. `EV per $1 = probability / cost − 1`. Quarter-Kelly output is `0.25 × max(0, (probability − cost)/(1 − cost))`. The artifact also preserves raw price-only edge.

### Independent under model and defender checks

The probability, early-season blend and blowout bullets above describe the unchanged **over** model. Unders use their own estimator and rankings: the opponent must rank **1–10** against the player's position, and both the current cohort and trailing-82 baseline use games against currently top-10 positional defenses. A hit is strictly fewer points than the milestone. Usage, minutes, availability, baseline and spread requirements still apply. Small samples are disclosed. A large spread adds a warning, not an unvalidated numerical under boost. This model is not simply `1 − over_probability`, since the historical cohorts differ.

For priced under options, ESPN opponent-roster checks flag available rotation guards/wings (at least 20 current-season minutes per game) with NBA All-Defensive or Defensive Player of the Year honors in the last two completed seasons. Available rotation players with at least 1.5 blocks per game, at least five games and 20 minutes per game in the sample are flagged as shot blockers. Before five current-season games, block production uses the previous season and is labeled accordingly. OUT/DOUBTFUL/QUESTIONABLE/inactive/restricted players cannot support these flags. Missing fields are recorded as data gaps, not treated as proof of weak defense.

Recent honors are a **perimeter-defense proxy**, not a tracking-based point-of-attack grade or confirmed assignment. Blocks are not a complete measure of rim defense. These names support review but **do not change model probability**, avoiding an arbitrary boost or double-counting positional defense. The JSON artifact retains individual evidence, sample seasons and source URLs. Under summaries show the checked matchups even if no under is profitable.

Over selections remain separate and retain their existing model. As a conservative conflict guard, an under recommendation is withheld if that player has any positive-edge over candidate on either exchange, including alternate thresholds. The over selection is not removed or reranked. This can omit valid alternate-line combinations, but prevents contradictory recommendations. Both sides retain separate ROI-above-100% alerts and use their own executable price and fees.

### Kalshi public market integration

Discovery uses the `KXNBAPTS` (NBA Player Points) series, fetching every cursor page from the public REST API. Event tickers establish the originally scheduled local date and team matchup; closing timestamps are not treated as game start times. Exact player names are matched to ESPN rosters, and the existing points model, availability rules and pregame checks apply. The parser checks the title, strike, $1 payout and full-game settlement wording. Protected, combined-stat, quarter, playoff-series and other unsupported series are excluded. This integration does not add the last-20-game or other stat-category models discussed as future work.

Kalshi's order book returns YES bids and NO bids. The executable **YES ask is `1 − highest NO bid`**; the **NO ask is `1 − highest YES bid`**, each using the corresponding bid size as available depth. A fresh REST snapshot is fetched for each valuation; Kalshi supplies no book timestamp here, so the artifact labels its timestamp as retrieval time rather than last book update time.

Fee type and multiplier come from live series metadata, including event overrides. Supported general quadratic taker fees are `0.07 × multiplier × contracts × ask × (1 − ask)`. Total entry cost is rounded upward to a centicent per the published July 2026 schedule. The bot selects a whole-contract quantity whose total including fees fits `UNIT_USD`, requires enough depth at the best ask, and calculates ROI and Kelly from the resulting cost per share. Unknown fee types/multipliers are withheld. Fee waivers and third-party broker charges are not modeled; the exchange's general fee is used. Settlement notes disclose that DNP/canceled contracts can settle at a fair price; probabilities/ROI assume normal settlement after participation.

If one exchange's discovery fails, the other exchange is still processed. The failed exchange gets an explicit unavailable status, the overall run is degraded (or failed if both fail), and artifacts retain exchange-specific counts and skip reasons. Shared stats-source failures can affect both reports. Separate side/exchange messages and an aggregate embed-text budget keep picks, defender evidence and ROI alerts within Discord's limits.

Reports include model probability, ask/implied probability, edge in percentage points, EV, quarter-Kelly, cohort sizes, liquidity and modeling notes. They do not invent pace, lineup, tactical or injury explanations. No qualifying picks is a valid result; the bot never fills the list with sample picks.

### ROI alerts and screening counts

**Estimated expected ROI** is `model_probability / cost_including_fees − 1`. Alerts require **strictly greater than 100%**, ranked by estimated ROI independently of the top-edge list, with up to three distinct players shown per exchange. A contract appearing in both lists is displayed once with an `ROI >100%` label. Existing eligibility, sample, injury and liquidity checks still apply.

**Net ROI if it wins** is `1 / cost_including_fees − 1`; this is the winning payout's profit, not an expectation. For example, a $0.25 contract with zero fees and an estimated 60% chance has estimated ROI of **+140%**, net ROI if it wins of **+300%**, and a losing outcome of **−100%**. Kalshi entry fees reduce those returns. Model errors can make estimated ROI inaccurate.

`Found` counts parsed open points contracts (a physical market is not counted twice for its YES and NO sides); each side's report shows its own valued count and skip reasons. The artifact also retains combined side-option valuation counts. Picks are positive-edge selections. The Discord summary includes the three most common skip reasons. If zero contracts were valued, ROI was **not assessed**, rather than assessed and found unattractive. For the initial late-night October 28 preview, all 222 contracts were skipped because their games had already started.

## Local use

Requires Python 3.12 or newer. Run from this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m polybot --check-sources
.\.venv\Scripts\python.exe -m polybot --dry-run
```

For a future-slate preview, add `--dry-run --date YYYY-MM-DD`. This is not a historical backtester; past dates are rejected because archived prices and defense snapshots would be required.

For an actual local delivery, set `DISCORD_WEBHOOK_URL` in your process environment and run `python -m polybot`. GitHub Actions injects the repository secret automatically. `.env` files are ignored by Git but are not automatically loaded. Never put a webhook into the workflow YAML or source code.

## Operation and troubleshooting

* Every run writes `reports/YYYY-MM-DD.json` (including per-exchange and under results) plus a JSON array of Discord payload previews (normally four; an early shared failure/no-games result can have two). Actions uploads them as a 14-day artifact, including screening skip reasons. Public artifacts contain player/market data, never the webhook.
* ESPN/defense responses have short local caches, with player, position and team lookups reused across exchanges. Cache files, local environments, secrets and generated reports are ignored by Git. Prices are always fetched fresh.
* Total source failures produce an explicit data-unavailable report and a failing Actions run. Partial source failures mark the report degraded, print a GitHub Actions warning, and retain specific error/skip reasons; they do not mark a delivered partial report as a failed delivery. Use `--strict` to also exit nonzero for degraded data (for diagnostics). Missing liquidity or ineligible players are recorded as skips. A missing webhook fails before delivery without printing the secret; Discord delivery errors still exit nonzero.
* Discord posts are not automatically retried after a timeout, to avoid duplicate deliveries. Re-running a successful job or manually delivering a second report can post twice.
* Public sites can change their markup or block automated requests. Run `--check-sources` to verify the current parsers. The public feeds are not an availability SLA, and the 2 p.m. report may still precede some player-market publication. There is no paid or authenticated fallback silently substituted.
* Scheduled workflows run from `main`. GitHub can disable public-repository schedules after 60 days with no repository activity; re-enable them in Actions if that occurs.

References: [Polymarket public market data](https://docs.polymarket.com/market-data/overview), [order books](https://docs.polymarket.com/market-data/prices-order-books), [GitHub scheduled workflows and timezone support](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Kalshi references: [public market data](https://docs.kalshi.com/getting_started/quick_start_market_data), [market order books](https://docs.kalshi.com/api-reference/market/get-market-orderbook), [series fee metadata](https://docs.kalshi.com/api-reference/market/get-series), [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf).
