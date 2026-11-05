## Verdict

The data you've gathered is unusually honest and it mostly refutes the vision: every entry-side signal has failed out-of-sample, social signals lag the chain, and your only validated edge is an exit. "Safe passage" as a fast-entry thesis is not viable at 1–3 slot latency; a slower selection-plus-exit-discipline system might be marginally viable. Treat the KOL wallet-to-post gap as the one untested entry hypothesis worth money.

## Answers

**Q1.** Not viable as a speed game. Block-0 bundlers hold inventory before you can see the launch; at 1–3 slots you are structurally their exit liquidity, and your own data (delayed-follow cells all negative, deciles beat nothing) confirms it. The only plausible "safe passage": don't race the open. Enter minutes-to-hours later on legitimacy-filtered graduates (score ≥4, real social breadth), ride confirmed retail inflow, and exit on insider distribution / winner-sell — your one validated signal, generalized. The second plausible mechanism is the KOL wallet-buy→post gap (Q5): if that gap is minutes, 1–3 slots suffices. Everything else in the vision is a latency game you've already lost.

**Q2.** Robust: the *negative* filters — serial creator, dev sold <60s, mayhem, bundle-dominated first minute — because faking cleanliness costs the operator money and time, and your n is huge. Winner-sell exits are robust (faking requires burning real prior profit). Fragile/fakeable: "never-seen wallets → 1.7x" directly collides with Bitquery's manufactured-graduation pattern (bundles hand off to fresh wallets) — this will invert; ≥5 X authors is purchasable (aged accounts are a commodity; your median-3.5y-age finding is consistent with bought aged accounts, not safety); identical-size buys is unresolved and trivially scripted; LunarCrush cashtag counts are spammable. Assume any positive signal decays once your flow becomes detectable.

**Q3.** Drop "alive" as the target — it's a survival label, and you already note survival ≠ profit. Label = *simulated passage PnL*: strictly-prior entry rule, fills modeled against actual pool depth at that slot (slippage + 2× fees), exit by your actual exit engine, dead coins marked to last trade, fixed max horizon so every coin resolves. Wash-adjust all volume/breadth features by stripping one-tx round trips and funding-cluster internal flow *before* feature computation. Keep the 3-tape walk-forward and read-time discipline you have; the 3–33 h read window is fine only because features are strictly prior — keep enforcing that mechanically, not by convention.

**Q4.** Build order: (1) funding-cluster graph + Jito bundle membership — they feed the insider-inventory tracker, which generalizes your only validated signal; (2) transfer hand-off detection (catches manufactured graduations, your biggest label contaminant); (3) one-tx round-trip wash filter (cheap, pure hygiene). Volume-bot *fingerprinting* beyond that is lower priority. "Anticipate bots buying to become our exit" is not viable: bots that buy are either informed (you're front-run) or wash (their buys aren't real demand and won't fill your sell at size). Kill it.

**Q5.** Score KOLs purely on strictly-prior *wallet* outcomes: for each past (buy, post) pair, forward return from a fill 2–3 slots after the wallet buy, net of costs, plus buy→post gap distribution and whether they distribute into their own post. Ignore followers/engagement (the KOL literature says bigger = worse). The front-run trade is testable this week with Kolscan + your Geyser feed and is the most plausible entry in the packet — but expect: gaps too short, KOLs rotating to fresh wallets once copied, and heavy copy-trade competition already in the trade. It's observation of public data; ethically fine.

**Q6.** Recoup-then-ladder is half right. Recoup-first is sound (fees mean breakeven ≈ +4–8%; take it). The ladder is not: PumpSwap is an AMM — there are no resting limit orders, only your own triggered market sells into whatever depth exists, and insider distribution removes that depth before your upper rungs fill. Better: size each exit tranche to current pool depth (e.g., never >2–5% of pool per sell), and make insider-distribution/winner-sell triggers *preempt* the ladder unconditionally, plus a time stop. The run you're riding ends in one slot; ladders assume continuity that thin pools don't have.

**Q7.** Two weeks, in order:
1. **KOL gap study.** Capture Kolscan wallets + X posts; measure buy→post gaps and net forward returns entering 3 slots after wallet buy. Success: median net > 0 after costs on ≥50 events. Kill: median gap < 5 s or returns negative.
2. **Insider-inventory exit.** Dev + bundle + funding-linked net position per coin; exit on first distribution. Success: beats winner-sell-only exit by a measurable median margin on 3 tapes. Kill: no improvement.
3. **Slow-entry paper strategy.** Enter 30 min post-graduation on score ≥4 + ≥3 prior X authors; exits from #2. Success: median net PnL > 0 per tape. Kill: negative on 2 of 3 tapes.
4. **Wash/hand-off filters** as hygiene feeding all of the above.

Do NOT build: pre-launch watchlist (your own data: pre-announced coins die), ShredStream (speed isn't your game), fake-follower ML, bot-exit anticipation, breakout-hunting position sizing (you have zero evidence for rare-breakout capture).

**Q8.** ~15–20% that some version is net-positive EV after costs within 3 months at 0.05–1 SOL. Biggest risk: you have exits without entries — every entry signal so far is selection (who survives), not timing (where profit is), and the ecosystem adapts faster than your 3-tape validation cycle.

## Top 3 risks

1. **No validated entry exists**; the program may converge to "lose less," mistaken for an edge via survivorship in paper results.
2. **Adversarial decay**: fresh-wallet and social-breadth signals are exactly what manufactured-graduation operators already fake; your strongest positive features invert under adaptation.
3. **Thin-pool execution**: paper fills overstate real fills; slippage on exit into insider distribution can erase the 2–3% exit edge that is currently your only proven asset.
