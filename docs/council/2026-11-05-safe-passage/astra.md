## Verdict
Don't build the full "safe passage" system. The packet's own evidence shows a working exit signal and filters that predict survival, but no measured entry edge. The trading thesis rests on turning that survival lift into returns from a realistic entry price, and that has not been tested. Narrow the next two weeks to that one test, plus an exit that tracks when insiders start selling. Cut the pre-launch, influencer and latency-race work.

## Answers

**Q1.** Not at entry speed. Block-0 bundles and devs act before you can (Bitquery: bundles on 21% of coins, devs sell within 1 minute on 65%). Following profitable wallets' buys at 1–3 slots has already lost money for you. The most plausible edge is to be late and selective: enter after the launch score is known or after graduation, once evidence has piled up and pools are cheaper to trade (PumpSwap ~0.3% vs 1.25% per side). Then exit on the validated winner-sell signal and its generalization, insider distribution. Your only out-of-sample win is an exit, so build where the evidence is. "Ride the operator's markup" assumes you can see what they hold. On-chain funding links make that partly possible, but obfuscation is cheap.

**Q2.**
- **Likely robust (costly or structural):** a serial creator, or a creator selling in the first 60 s, as near-certain death (0/9,482); mayhem mode (0/10,351); the winner-sell exit; breadth across distinct older X accounts (median account age 3.5 years); excess distinct creators on LunarCrush.
- **Easy to fake or illusory:**
  - "First-ever launch" and "never-seen wallets → 1.7x" are what a fresh-wallet operator produces for free, and manufactured graduations explicitly use fresh wallets.
  - "> 25 first-minute buyers" is beaten by bundlers running 20–50 wallets.
  - "Dev buy > 2 SOL" is cheap to fake.
  - The 5.5x for identical-size buys is unresolved, possibly volume bots. Don't trade on it.
  - Follower counts and 10k+ accounts: the research says larger KOLs did worse.
  - Every feature that is cheap to fake will drift once operators adapt, so re-check the lift on every new tape.

**Q3.**
- **Fix the clock.** Outcomes are read 3–33 h after launch, and "traded in the last hour" depends on when you read. Re-label at fixed horizons (6 h, 24 h, 72 h, 7 days).
- **Real community:** count distinct wallets with net buys at a fixed horizon. Exclude one-transaction round trips, wallets funding-linked to the dev or bundles, and transfer recipients. Require ≥ N such wallets plus pool depth.
- **Profitable passage:** net SOL return of a fully specified policy. Entry is at decision time + 2 slots, priced from the actual curve or pool reserves with slippage. Exit follows your exit engine. Include all fees. Dead coins are marked to last trade. Coins that die after you enter must stay in the denominator.
- **Medians vs. a lottery thesis.** "Medians over means" cannot validate a strategy whose payoff comes from rare winners. Report the mean with a bootstrap confidence interval, plus the result after dropping the top 1–2 coins.

**Q4.** Build in this order:
1. Bundle detection (same bundle/slot as the create transaction).
2. One-hop funding clusters.
3. Transfer hand-offs.

All three feed the insider-inventory exit, which extends the signal you have already validated. Volume-bot detection is cheap (one transaction, two signers, per Bitquery), so add it as a filter. "Bots buying to become our exit" isn't viable: wash round-trips add no net demand, and paid market-making is the operator's own exit, not yours. Kill it.

**Q5.** Score a KOL only on strictly-prior posts:
- median net return from post time + latency to fixed horizons;
- the lead time from their wallet buy to their post;
- the share of their coins alive at 72 h.

Following the KOL's wallet into the post is testable now because Kolscan publishes trades in real time. But that publicity makes it crowded, KOLs use alternate wallets, and your own wallet-follow results were all negative. My prior is that it fails. The pre-CA watchlist is contradicted by your data: first posts mostly come after the move, and pre-announced launches mostly die.

**Q6.** "House money" is mental accounting; it doesn't change expected value. On a curve, recovering cost needs roughly +3% after fees and slippage. Limit orders don't exist natively on the curve or in pools, so bot-triggered sells fire 1–3 slots late into drops of 19–37% within 60 slots. Better design:
- triggers from the validated signal (winner-sell, insider distribution);
- a time stop;
- a trailing stop and partial take-profit tuned on tapes;
- position size capped by pool depth so price impact stays below 1–2%.

Don't size up on "breakouts" until a size-up rule shows lift out of sample.

**Q7.** Two-week plan, in priority order:
1. **Fixed-horizon re-label (days 1–2).** Success: launch score ≥ 4 still gives ≥ 5x lift at 24 h and 72 h on all 3 tapes. Kill: lift < 2x at 72 h.
2. **Launch-score entry backtest:** enter at 60 s + 2 slots with exact curve math and the current exit engine. Success: pooled mean net return > 0 with 90% bootstrap lower bound > 0, positive on ≥ 2 of 3 tapes, and still ≥ 0 after removing the top 2 coins. Kill: mean ≤ 0, or the result depends on a single coin. Caveat: only about 13 alive coins per tape fall in this bucket, so add tapes before trusting any result.
3. **Post-graduation entry:** enter on PumpSwap at graduation + 5/15/30 min, filtered by launch score and strictly-prior ≥ 3 X authors / LunarCrush. Same success and kill criteria as item 2.
4. **Insider-inventory exit:** use bundle + one-hop funder tags. Success: an insider net sell predicts a 25-slot drop beyond what winner-sell already predicts, and lifts median exit by ≥ 1% of position on 3 tapes. Kill: incremental lift < 0.5%.
5. **Kolscan wallet-follow:** Success: net-positive return at a 2-slot delay on ≥ 2 tapes. Kill: negative, as the earlier wallet-follow results were.

Do not build: pre-launch watchlist, ShredStream/Jito latency race, fake-follower ML, the "bots as exit" model, breakout up-sizing, or a full KOL intelligence database.

**Q8.** My prior: ~15% chance of positive net EV at 0.05–1 SOL within 3 months. Biggest risk: survival lift ≠ returns. When the evidence is in, price already reflects it, and the remaining edge is too tail-dependent to confirm with ~40 winners.

## Top 3 risks
1. **The gap between survival and profit at the price you can actually get.** No entry has ever beaten cash for this program.
2. **Too few winners.** Score ≥ 4 is ~6% of launches with ~2.3% alive, so about 13 winners per tape. That is too thin to tell edge from noise, and the median rule hides it.
3. **Operators adapt and the data breaks.** The fakeable features (fresh wallets, buyer counts) are exactly what manufactured graduations generate. Your recorder has already silently lost data twice.
