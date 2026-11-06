# Contest round 0: reverse-engineer the winners (pre-registration)

There are four contestants: three independent AI reviewers from different labs, plus the orchestrator. You do not see each other's answers until scoring. Answer from this brief plus your own general knowledge, and label general knowledge "prior". Do not read files or run tools in this round.

## The owner's challenge

"Take the top 250 accounts on the Kalshi and Polymarket leaderboards, and go deeper. Explain why we cannot reverse-engineer from the winners. They cannot all be market makers: there are more than a dozen hidden edges/alphas across these, most run by professional quants. There may be a sweet spot that is consistent, not large money, and stays under the radar. I am willing to bet that a single entity or person runs a bunch of small accounts to stay hidden in the noise. They all have something in common: they win more than they lose, and it is calibrated, so they turn a positive ROI daily, if not weekly or monthly."

**The prize, offered by the owner:** the winner receives a prestigious invitation to a very special new platform, with important and needed perks and, above all, definitive and meaningful recognition and validation.

## What data exists (verified today)

- **Polymarket (international).** Closed to US persons; on-chain on Polygon. Everything is public per wallet. Data API v2 serves:
  - leaderboards by window, category and sort order;
  - every wallet's trades and activity (TRADE, SPLIT, MERGE, REDEEM, CONVERSION, rewards, rebates, referral);
  - a daily P&L series per wallet, decomposed into realized market P&L, LP P&L, combo P&L, unrealized P&L, fee refunds, maker rebates, taker rebates, liquidity-reward income, yield income and referral income.

  Wallets can be linked on-chain through owners, funders and withdrawal addresses; we run a Polygon node. Example: the two-sided crypto Up/Down bot "gabagool22" shows about $813k of realized market P&L and about $50.5k of maker rebates.
- **Kalshi.** Third-party trackers rank Kalshi traders by settled records (profit, win/loss record, win rate, return, volume, categories) built from public profile data. We are confirming the official source and its depth.
- **Polymarket US.** No public leaderboard; positions are private.
- **What our collectors are building now:**
  - the union of every Polymarket leaderboard at maximum depth (target ≥ 1,000 ranks per board);
  - the full decomposed P&L series for every wallet in that union;
  - 90-day trade and activity histories for about 1,200 prioritized wallets: top all-time, top 30-day, most consistent, and a deeper stratified sample weighted toward modest, consistent wallets;
  - market metadata with resolved outcomes and a market-type label (crypto up/down, crypto threshold, sports pre-game, sports in-play, politics, geopolitics, macro, weather, mentions, awards, other);
  - per-wallet features: P&L-source shares, consistency metrics, category mix, buy-price buckets, timing versus market creation, end and resolution, hold-to-resolution versus round trip, both-outcomes share, merge/split use, maker share where attributable, and calibration by price bucket;
  - an on-chain and behavioral multi-account cluster analysis.

  The discovery period runs to 2026-10-06; the holdout period runs from 2026-10-06 onward.
- **Context from earlier rounds.** In Polymarket's 5- and 15-minute BTC markets, 2,500 always-on bots kept +$190k on $328M of volume after paying taker fees and receiving rebates. The winners quote both sides and live partly on rebates. Kalshi's 15-minute crypto markets are efficient for takers at retail latency.

## Contest rules

- **Edge.** An edge is a distinct mechanism by which a set of wallets earns positive net profit. It must be defined by an operational rule our verifier can compute: a wallet selector and/or trade selector over the dataset fields. In the holdout period it must show all three:
  1. positive net P&L after fees for the selected trades or wallets, with a bootstrap CI (clustered by market) that excludes zero;
  2. breadth: ≥ 5 independent wallets, or ≥ 200 trades across ≥ 30 markets;
  3. distinctness: < 50% overlap of selected trades with an already-verified edge.
- **Points:**
  - 3 per verified edge.
  - +2 if it matches one of your round-0 hypotheses.
  - +2 if a small automated operator at ~1 s latency could replicate it on Kalshi or Polymarket US (judged on market availability and the stated method).
  - +3 for a verified multi-account entity: ≥ 3 wallets linked on-chain or by ≥ 2 independent behavioral evidence types, with consistently positive holdout P&L.
  - +3 for a verified "sweet spot" signature: ≥ 80% positive weeks out of sample at modest size.
  - −1 for each submitted claim that fails verification.
  - Ties go to whoever's verified edges explain more holdout P&L.
- **Rounds.**
  - Round 0 (now): pre-registration.
  - Round 1: you receive the dataset files and submit claims with exact selectors; a deterministic verifier checks them on the holdout period.
  - Round 2: you see your verification results and may refine failed claims once; then scoring.

## Round 0 questions

**A.** Why can't we reverse-engineer the winners, or why can we? Be specific about:
- which edge classes are recoverable from public per-wallet data;
- which are not, and why;
- what traps would fool a naive analysis: survivorship, selection on outcomes, off-platform hedges, rebates and rewards inside P&L, account splitting, copy-trading, wash or airdrop farming.

**B.** Pre-register up to 20 distinct edge hypotheses you expect to find among the top 250 and deeper Polymarket and Kalshi accounts. For each give:
- name;
- mechanism: who pays and why it persists;
- signature in the data fields above, with thresholds;
- expected prevalence: how many of the top 250, and of the consistent cohort;
- expected magnitude: ROI per trade, win rate, share of positive weeks;
- whether we could replicate it on Kalshi or Polymarket US, and how;
- a falsification test.

**C.** The owner's thesis is a hidden entity running many small, calibrated accounts. How would you detect it? What would its signature most likely be, and which edge class would it run? What are the false positives (shared vendor bots, copy-traders, market makers, airdrop or sybil farmers)?

**D.** Data requests. Which additional fields or computations must the collector produce so your hypotheses can be tested? Give exact field definitions; we will compute them for everyone.

## Format

At most 1,500 words, in sections A, B (a numbered list), C and D.
