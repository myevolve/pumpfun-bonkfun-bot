## Verdict

At 1–3 slot latency and 2.5% curve round-trip fees, pre-CA hype detection, wallet copy-trading, and outmaneuvering block-0 cabals mid-pump are dead ends; your negative out-of-sample entries confirm obvious signals price in before you land. "Safe passage" inside intentional manipulation fails whenever operators fund exit wallets via CEX withdrawals outside your transfer graph, making you their exit liquidity. Your only plausible edge is **slow-graduation (>10 min) PumpSwap continuation** (~0.6% round-trip fees, minute-scale decay), combining hard rug vetoes, wash-stripped buyer breadth, and insider-transfer exits.

## Answers Q1–Q8

**Q1.** Curve-stage "safe passage" fails at 1–3 slots: block-0 bundlers (21%) and 0-slot snipers (38%) hold steep cost advantages, while manufactured graduations pass tokens to fresh wallets and claw back >=90% on 98% of launches. Most plausible edge: **>10 min PumpSwap continuation** (13.6% survival vs 2.6% for 1–10 min graduations). Round-trip fees drop 4x (~0.6%), X/terminal discovery unfolds over minutes (neutralizing 1s lag), and your validated sell signal caps downside while tracked insider supply stays unmoved.

**Q2.** *Robust (costly to fake):* (1) **Hard vetoes**: serial creators (2/14,891 alive), dev sell <60s (0/9,482), mayhem (0/10,351) cut ~94% of launches (though 97.7% of survivors still die); (2) **Insider/winner sells**: operators must extract SOL to profit; (3) **>=5 aged X authors** and **>10 min graduation**.
*Illusory/faked:* (1) **"Never-seen-before" wallets (1.7x)**: CEX-withdrawal sybils; (2) **Raw volume and >25 buyers**: 58% of DEX volume is wash and 20–50 sybils are cheap; (3) **Pre-launch X/cashtags**: pre-announced coins mostly die and 76% of tickers collide; (4) **>=10 identical buys (5.5x)**: valid *only if* proven to be terminal presets, not wash bots.

**Q3.** *Profitable passage:* Never use future peaks or survival flags. Simulate executable entry at `t0+3` slots against actual pool reserves for size `S` (including fees, tips, and impact), paired with a causal exit against executable sell reserves at `trigger+3` slots (drained pools marked to reserve liquidation, not last dust price). **Critical fix:** use medians for feature outliers, *never* strategy gating; require positive **mean (total-SOL) net EV**, since memecoin returns are skew-driven.
*Real community:* Fix horizons (+4h, +24h; 3–33h mixes survivorship). Require >=25 SOL reserves, >=30 funding-collapsed directional traders after stripping 1-tx/short-window round trips, and top-5 non-LP clusters <50% supply.

**Q4.** Build first: (1) **Per-coin slot-0 bundle + SPL transfer tracker** (cheap, local, catches fresh-wallet handoffs taking 35–83% of bundle value); (2) **Pool wash filter** (1-tx two-signer swaps catch 95% of fake volume, plus look-alike cohorts); (3) **1-hop recent SOL funder clusters** (stop at CEX hot wallets). **Kill "anticipate bots buying for our exit":** sniper/copy bots react in 0–1 slots on the same Geyser data, and volume bots self-cross atomically inside one tx.

**Q5.** Score KOLs strictly prior (>=10 calls, shrunk toward the >65%-zero base rate) on executable `+3` slot net return (+1m/+5m/+30m), `first_linked_sell - post_time` (blacklist dumpers), and pre- vs post-peak posting share. Wallet-to-post front-running is **cheaply testable** via Kolscan + your $1.39 X API (include *unposted* KOL buys to avoid selection bias), but **low-probability (<15%)**: pump.fun's Kolscan is copy-botted in slot 0–1, and savvy KOLs use undisclosed wallets or dev transfers.

**Q6.** **Recoup-then-ladder is broken.** Neither venue has a passive orderbook (every leg pays taker fees, slippage, and tips), and early recoup cuts exposure 4–5x before the rare fat-tail breakouts (DOGE/PEPE) needed to offset losses—creating **positive medians with negative total EV**. Better: (1) exit **100% immediately** on first dev/bundle/transfer-recipient sell or >=2 SOL winner-sell; (2) **velocity/time stop** when wash-stripped buying stalls (before liquidity drains); (3) trim partially only when position exceeds ~1–2% of pool SOL reserves, otherwise trail executable reserve value.

**Q7.** *2-week plan:*
- **Exp 1 (Days 1–3):** Reserve-exact `+3` slot EV simulator + bundle/transfer-exit tracker on 3 tapes. *Success:* >=+3% mean return lift or >=5% smaller p25 loss vs winner-sell exit. *Kill:* <+1% lift (keep dev + winner-sell).
- **Exp 2 (Days 4–7):** Split "10 identical buys" (terminal presets vs wash) + replay PumpSwap entries (>10m grad, Score >=4, zero insider sells, wash-stripped inflow). *Success:* Positive mean *and* median net SOL EV on >=3 tapes (`N>=50`). *Kill:* Mean EV <=0 or identical-buy lift <=1.5x post-wash.
- **Exp 3 (Days 8–10):** Kolscan wallet-to-post replay (all KOL buys, posted or not, `+3` slots). *Success:* >+5% mean net return (`N>=30` OOS), no KOL >30% PnL. *Kill:* Mean EV <=0 or >70% unposted buys.
- **Exp 4 (Days 11–14):** Poll X CA API on Score >=4 graduates; enter PumpSwap at >=5 aged authors with >85% insider hold. *Success:* Positive walk-forward net SOL EV on 3 new tapes. *Kill:* Mean EV <=0 from X lag.

*Do NOT build:* Pre-launch X watchlists, ShredStream, deep chain-walking, fake-follower ML, LunarCrush feeds, or 0.05 SOL live sizing (priority fees crush <0.2 SOL trades).

**Q8.** **15–20% probability** of positive net EV within 3 months at 0.25–1 SOL on PumpSwap (**<5%** on curves or at 0.05 SOL). **Single biggest risk:** CEX-funded insider wallets evading your tracker while median-based gating masks negative-EV tail losses.

## Top 3 Risks

1. **Positive-median, negative-EV trap:** Pairing median PnL gates with recoup-then-ladder hides -50%..-90% rug slippage behind +2% scalps while truncating rare right-tail winners needed for positive SOL EV.
2. **CEX-funded sybil blindness:** Operators funding 20–50 fresh wallets via separate CEX withdrawals mimic "never-seen-before" buyers (1.7x), luring "safe passage" entries into coordinated dumps.
3. **Architecture sprawl before signal proof:** Building six subsystems (ShredStream, deep graphs, fake-follower ML, pre-launch watchlists) burns 3 months before proving *any* causal entry beats cash.
