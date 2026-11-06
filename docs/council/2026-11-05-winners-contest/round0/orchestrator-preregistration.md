# Orchestrator pre-registration (written 2026-11-05, before any contestant answer or winners data was seen)

Each edge: mechanism / signature in public per-wallet data / replicable on Kalshi or Polymarket US?

1. **Two-sided crypto Up/Down quoting.**
   - Mechanism: spread capture plus maker rebates.
   - Signature: buys both outcomes in > 80% of markets; maker share > 80%; maker_rebate > 30% of P&L; > 80% crypto up/down; held to resolution.
   - Replicable: partially (Kalshi 15-minute has no rebate; Polymarket US brackets are wide).
2. **Near-certainty harvesting ("endgame bonds").**
   - Mechanism: buys at >= 0.95 in the final hours, when the outcome is effectively known.
   - Signature: >= 98% win rate, tiny ROI per trade, positive almost every day, rare large loss (dispute or reversal).
   - Replicable: yes.
3. **Resolution-window trading.**
   - Mechanism: buys after the real-world outcome is public (game over, data printed) but before the market closes or resolves; slow convergence pays.
   - Signature: trades after the event end time at < 0.99, about 100% win rate.
   - Replicable: yes.
4. **Longshot NO grinding.**
   - Mechanism: buys NO at 0.85-0.98 across many low-probability propositions and holds to resolution.
   - Signature: hundreds of markets, high win rate, many positive weeks, occasional large loss.
   - Replicable: yes.
5. **Liquidity-reward farming.**
   - Signature: reward_income a large share of P&L, maker-heavy, many markets, small directional exposure.
   - Replicable: only if Polymarket US runs rewards.
6. **Weather specialists.**
   - Mechanism: daily temperature and precipitation markets, priced from ensemble forecasts.
   - Signature: weather concentration, many small calibrated positions, positive most days.
   - Replicable: yes (Kalshi weather).
7. **Mention/speech-market specialists.**
   - Mechanism: transcript base rates.
   - Signature: concentration in "will X say Y" markets, consistent.
   - Replicable: yes (Kalshi mentions).
8. **Sports in-play latency.**
   - Mechanism: buys seconds after score changes before the book reprices.
   - Signature: trades cluster just after price jumps, on the right side, positive short-horizon markouts.
   - Replicable: no (speed).
9. **Sharp-book line following, pre-game.**
   - Mechanism: trades right after sharp sportsbook moves.
   - Signature: positive drift between the trade and the close (closing-line value).
   - Replicable: yes, slow enough.
10. **Cross-venue arbitrage.**
    - Signature: looks one-sided on Polymarket; small, steady positive markouts; size capped like an external book's depth.
    - Replicable: we run it.
11. **Neg-risk / complete-set arbitrage.**
    - Signature: CONVERSION/SPLIT/MERGE activity; holds every outcome of an event below $1 or sells sets above $1.
    - Replicable: only where the venue supports it.
12. **Macro nowcasting (CPI, payrolls, Fed).**
    - Signature: macro-release concentration, positions in the final days before a print, calibrated.
    - Replicable: yes (Kalshi macro).
13. **Informed one-shot wallets (insiders).**
    - Signature: new wallet, few markets, large size, pre-announcement timing, near-perfect record.
    - Replicable: no; a distinct class to exclude.
14. **News-speed traders (politics, geopolitics).**
    - Signature: on the right side within minutes of large moves; positive +10 min / +1 h markouts.
    - Replicable: unlikely.
15. **Market-open mispricing.**
    - Signature: first trades within minutes of listing; positive markout as liquidity arrives.
    - Replicable: maybe.
16. **Late crypto Up/Down lead pricing.**
    - Mechanism: taker on the leader at 0.80-0.95 after minute 8-12, conditioned on volatility.
    - Replicable: weak.
17. **Yield/holding-reward harvesting.**
    - Signature: yield_income a large share of P&L, long-dated holdings.
    - Replicable: if the venue pays interest (Kalshi pays on positions).
18. **Referral income as the profit source.**
    - Signature: referral rewards a large share of P&L.
    - Replicable: not trading.
19. **One entity, many small accounts, one strategy** (most likely classes 2, 4 or 6).
    - Signature: shared funder or withdrawal address, synchronized trades, identical sizing, near-identical P&L curves, consistent positive weeks.
20. **Esports and niche-league specialists.**
    - Mechanism: lines lag sharp books in thin leagues.
    - Signature: concentration there, positive closing-line value, modest size.
    - Replicable: yes (Kalshi esports).

**Why plain reverse-engineering of winners fails, and what survives:**
- **The ranking selects on outcomes.** Many top all-time accounts are lucky one-shot directional bets.
- **Some edges cannot be copied:** inside information, speed, and capital or rebate tiers.
- **Part of some winners' position sits off-platform.** A cross-venue hedge leg or a sportsbook position makes the Polymarket side look like a pure bet.
- **The P&L field mixes in rebates, rewards and unrealized marks.**
- **Account splitting hides who is who.**

What survives is a mechanical, repeated behavior signature, testable out of sample on held-out weeks: price bucket x timing x category x maker share x consistency.
