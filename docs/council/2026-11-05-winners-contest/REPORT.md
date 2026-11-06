# Winners contest: reverse-engineering Polymarket and Kalshi winners (2026-11-05/06)

## Result

| Contestant | Round 1 (blind) | Final (one revision per failed claim) | Verified holdout P&L |
|---|---|---|---|
| **argon** (Gemini 4 Argon) | 22 pts, 6/18 | **42 pts, 9/18: winner** | trades $966,509 · entities $38,252 |
| astra (GPT-6 Astra) | **24 pts, 5/12** (best blind score and precision) | 24 pts, 5/12 | trades $35,001 |
| fable (Claude Fable 5) | 0 pts, 2/12 | 4 pts, 3/12 | trades $41,190 · entities $34,631 |
| orchestrator (non-competing benchmark) | 1 pt, 1/7 | — | trades $114,405 |

Argon wins under the published rules, which allow one revision per failed claim after seeing holdout results.

- **Argon's three revision passes:** one entity trim to linked wallets (legitimate), one weather rule broadened by dropping a wallet filter (reasonable), and one after-end carry rule tightened to the holdout (CI lower bound $0.02).
- **On blind discovery alone,** astra led 24 to 22.

Pre-registrations were hashed before anyone saw data (`round0/HASHES.txt`), and round-1 claims were hash-sealed before round 2.

## Verified edges (holdout 2026-10-06 → 11-05, U_disc wallets selected on pre-cutoff data only)

| Edge | Mechanism (who pays) | Holdout | Kalshi or Polymarket US? |
|---|---|---|---|
| 15-minute crypto up/down maker fills | Retail takers pay spread and fees; makers buy below fair value | +$0.57–0.75 per trade, CI > 0; 33–43 wallets; found by all four | Yes as maker (Kalshi 15-minute markets, no rebate) |
| Pre-end "already decided" carry: 96–99.9¢ before the scheduled end, outside sports and crypto | Holders sell decided outcomes early to free capital | +$3.16 per trade [2.56, 3.72]; 33k trades, 116 independent wallets | Yes; human or rules-checked |
| Multi-outcome slate NO: 85–96¢ NO on fringe outcomes of events with ≥ 3 markets | Retail bids longshot YES; the slate's prices sum above $1 | +$18.24 per trade [11.3, 23.6]; 20k trades, 79 independent wallets | Yes (Kalshi multi-candidate events) |
| Smart-wallet shadowing: wallets that follow proven non-maker leaders within 60 s | Stale resting quotes after an informed on-chain fill | +$3.94 per trade [1.1, 6.9]; 92k trades, 27 independent wallets | Partly: follow on Kalshi off Polygon fills |
| After-end resolution carry (revised): ≥ 1 h after scheduled end, 97–99.9¢ | Slow resolution; sellers exit | +$0.77 per trade [0.02, 1.39]; 33k trades | Polymarket only (Kalshi halts trading at the event) |
| Weather (revised): 10–96¢, at most $2.5k per fill | Model forecasts beat retail priors | +$1.44 per trade [0.46, 2.79]; 11k trades, 54 independent wallets | Yes (Kalshi weather) |
| Category near-certainty "bonds" at ≥ 95¢: crypto 4h/1d, mentions, tech | Capital lock-up and verification effort | +$0.10–6.83 per trade, small n (202–831 trades) | Mentions and tech: yes |
| 5-minute crypto maker | Spread plus rebates at sub-second speed | +$0.13 per trade [0.03, 0.23]; 406k trades | No (not listed; needs speed) |

**Verified multi-account entities** (linked on-chain or by ≥ 2 independent behavioral evidence types, 100% positive holdout weeks):
- **E0000:** 6 linked wallets of a post-match sports resolution fleet, +$36.6k in the holdout.
- **E0005:** 6 wallets of a coordinated pre-game round-trip scalping fleet, +$1.6k.

**Outside the contest universe** (SybilHunter, full union):
- **32-wallet sports farm:** 27 wallets created within 20 minutes; $110 clips at 0.99; markets split between wallets; 100% positive weeks; $797k total, +$200k in the holdout.
- **38-wallet maker farm** with keyboard-mash names.
- **gabagool22 family:** about 8 wallets sharing a gas funder; $2.4M across a 13-wallet component.

## What failed in the holdout

- **Pre-game sports:** every entrant's version failed, including the orchestrator's (−$28 per trade).
- **In-play sports and broad politics or geopolitics books:** large positive totals, but event-clustered CIs crossed zero. Fable's in-play claim missed by 3¢.
- **Sweet-spot and wallet-persistence claims:** positive means, but no excess over controls matched on discovery-P&L decile and market type. Most discovery-selected wallets were dormant in October; the U_disc median holdout P&L is $0. Consistency persists only among wallets that keep trading, and that cannot be selected before the fact.
- **Longshot and awards rules, and the sibling-ladder arbitrage:** CIs crossed zero, except the awards-culture rule, which lost.

## Why copying winners mostly fails (the three contestants agree)

1. **The data shows the mechanism, not the signal.** You can see that in-play buyers win, not which feed or model told them to buy.
2. **Followers trade after the price has moved.** About 72% of winners' discovery fills are passive maker fills; a copier pays the spread they earned.
3. **Exits, hedges and sharding happen elsewhere.**
   - E0005's profit came from round trips before kickoff; its buys held to resolution lose money.
   - Some winners hedge on other venues.
   - Fleets split clips across 3–30 wallets.
4. **Leaderboards select on outcomes.** Today's boards already include October results; 12,322 wallets were on short-window boards only. Most wallets ranked consistent before the cutoff went quiet afterwards.

What can be copied is a repeated process signature: price band × timing relative to end × category × maker share, within capacity limits.

## Method and leak controls

- **Scoring universe U_disc:** 1,332 wallets in tiers D1–D4, defined from pre-cutoff P&L only. Wallets seen only on short-window boards, or collected only through holdout volume, were removed.
- **Contestant data:** an as-of-cutoff view (`build_disc.py`, with self-checks).
  - Outcomes are masked for markets resolving after the cutoff.
  - Markouts are masked where their horizon crosses the cutoff.
  - Wallet features come from an as-of replay (positions as of the cutoff).
  - The newest-first cap leak was refetched as of the cutoff.
  - Board P&L and ranks were removed.
- **Sandboxes:** each contestant could read only its own view and workspace; secrets and the verifier were unreadable; no network data was used.
- **Verifier:** deterministic; holdout-only; bootstrap clustered by event; breadth counts independent wallets; matched controls for wallet claims.
- **Known residual bias:** the collected union came from today's boards (wallets that were good before and then fell off every board are missing). 75 heavy wallets have holdout coverage gaps.
