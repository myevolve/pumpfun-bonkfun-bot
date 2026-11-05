# Council review packet: "safe passage" trading radar for pump.fun memecoins

You are one of three independent reviewers (different AI labs). Critique honestly; do not defer
to the framing. Answer only from this packet. Do not read files or run tools beyond this packet.

## 1. Who we are and hard constraints
- Research program around a Solana pump.fun / PumpSwap trading bot. Today it runs only in paper
  mode (never signs). Live money requires >= 3 non-contiguous capture days ("tapes") confirming a
  signal walk-forward, then a paper period, then an explicit owner decision.
- Infrastructure: dedicated Chainstack Solana node + Yellowstone Geyser gRPC stream (London),
  sub-second creation and trade visibility, ~0.4 s slots. We are NOT a block builder and do not
  run validators. Realistic latency to act after an on-chain event: ~1-3 slots.
- Costs: pump.fun curve fee 1.25% per side, PumpSwap ~0.3%/side, network/priority fees; size
  0.05-1 SOL per position today.
- Evidence rules: medians over means (dust tails), dead coins mark to last trade (never
  excluded), strictly-prior features (no look-ahead), walk-forward splits.
- Ethics boundary: we observe public data and trade. We never manipulate (no wash trading,
  coordinated pumps, paid promotion, spoofing) and never coordinate with promoters.

## 2. The owner's vision (paraphrased)
Detect the hype/promotion process BEFORE a contract address (CA) is posted; monitor the
promoter accounts; when they post, enter fast and cheap if evidence says the interest is real
(humans, not bots) and the token is legitimate (not a rug). Keep intelligence on top crypto
influencers. Run a "wallet radar" on money flow: deep wallet history and performance, quickly
disqualify manufactured bot wallets, unless we can anticipate them buying to become our exit.
Track liquidity pools (incl. Jito bundles / ShredStream). Ultimately reverse-engineer market
makers, whales and manipulation playbooks to engineer a SAFE PASSAGE through them (not avoid
them): enter while operators still need price up, exit before/at their distribution. Exits:
first recoup cost + fees with a small ROI, then ladder limit sells to ride the run ("playing with
house money"). Target rare DOGE/PEPE/TRUMP-like breakouts with an increased position.

## 3. Our evidence so far (all measured by us; tapes = our own Geyser captures)
Program history: entry-side signals have repeatedly failed out-of-sample at retail latency:
following profitable wallets' BUYS (they mark local tops; all delayed-follow cells net
negative), entry-feature deciles (none beat cash), letsbonk venue (negative). The only validated
signal is an EXIT: when a wallet with >= 2 SOL strictly-prior realized profit sells into a coin,
the next 10/25/60 slots' median price moves -7..-13% / -11..-22% / -19..-37% vs -0.7..-2.9%
after ordinary sells, confirmed on 3 non-contiguous tapes (100k-190k events each). Replayed in
our real exit engine it improves median exit outcomes by ~2-3% of position but does not make
entries profitable.

Outcome study (27,553 SOL-paired coins, 3 tapes, read from chain 3-33 h after launch). "Alive" =
graduated to PumpSwap, pool traded in the last hour, pool >= 10 SOL. Base: 0.18% of launches,
8.3% of graduates. Wash check on the 50 alive pools (last 100 txs each): median 0% one-tx
round trips, 0 pools >= 50%, but 10/50 have < 10 distinct real traders (thin / separate-tx bots).
So ~40/50 are genuinely alive. Survival != trading profit.

- Launch score (known within ~60 s): first-ever launch by creator; creator did not sell in
  first 60 s; dev buy > 2 SOL; > 25 first-minute buyers with no wallet > 50% of that buying; not
  "mayhem" mode. Score >= 4: ~6% of launches but 72-84% of alive coins per tape (alive 2.2-2.4%
  vs 0.18%); third tape held out and matched. Score 0-2 (2/3 of launches): 1 alive in 18,053.
- Serial creators: 2 alive / 14,891. Creator sold within 60 s: 0 / 9,482. Mayhem: 0 / 10,351.
- Graduation timing: same-slot bundled graduation 12.7% alive (n=118, ~85 SOL by ~5 wallets);
  graduated 1-10 min after launch 2.6% (n=191); > 10 min 13.6% (n=59).
- X (posts containing the CA, first 2 h, 400-coin stratified sample, $1.39 via a third-party X
  search API; weighted back to all 603 graduates, base 8.3% alive): >= 5 distinct authors ->
  24.9% alive (3.0x, covers 15% of graduates); a 10k+ follower account posted -> 26.6% (3.2x);
  no CA posts -> 4.7%. Strictly before an entry 30 min after graduation, >= 3 authors -> ~21%
  (small n). Posting accounts are a median 3.5 years old, < 1% under 30 days (search may hide new
  accounts). Cashtag/name search collides with other assets.
- TIMING: the first CA post on X usually comes AFTER the on-chain move: for graduates that later
  died, 56% of first posts came after graduation and 83% after the curve's price peak; for
  alive coins 24% / 56%. First post median 4-7 min after launch.
- Promoters recur: of 316 posting accounts, 45% posted >= 2 different coins, 56 posted >= 5.
- Pre-launch footprint of the coin's OWN X account (48 h before launch): only 6/50 alive coins
  even link their own account; 25-27% of dead/non-graduated coins named the coin before launch
  (pre-announced launches mostly die); alive accounts had ~4.7k followers / 3.9k views per post
  vs 82-164 followers for losers (tiny n).
- LunarCrush: contract addresses are not indexed; cashtags are (76% of tickers already active
  before launch). Excess distinct creators on the cashtag in 6 h after launch: alive median 6.8
  (61% >= 5) vs graduated-then-died 0.7 (24%). Hourly granularity. X >= 5 authors AND
  LunarCrush excess >= 5 -> 30% alive (3.6x, 10.5% of graduates).
- Wallet radar first cut (11,315 coins; who supplied > 50% of first-minute buy SOL, judged by
  strictly-prior history): serial snipers (bought within 2 slots of >= 10 earlier coins) -> 0.5x
  survival; proven winners -> 0.9x; wallets never seen before -> 1.7x survival, 2.5x graduation;
  creation-slot bundle buys > 50% -> 0.4x survival (but 1.4x graduation). Counter-intuitive:
  coins with >= 10 first-minute buys of IDENTICAL size by different wallets -> 5.5x survival
  (likely retail clicking preset buttons in trading terminals, not scripted wallets; or volume
  bots: unresolved).
- Data-quality lessons: several "obvious" heuristics inverted; our own recorder silently lost
  post-graduation data twice (now fixed); a public arXiv study on pump.fun sniper cohorts was
  corrected because 43% of its "buys" were sells.

## 4. External research (summarized)
- Bitquery, 8.2M pump.fun launches Oct-2025..Oct-2026: someone other than the dev bought in
  block 0 on 38% of coins; dev's own bundled wallets (same Jito bundle as the launch, positions
  1-4 after the create tx) on 21%, taking a median 10% of supply; devs + bundles made >= $579M,
  sniper bots $31.5M ($15.2M counting unsold as worthless; 54% of sniper wallets profitable);
  devs sold within 1 minute on 65% of coins. Since Jul-2026 "manufactured graduations": bundled
  wallets buying 1,000+ SOL in the pool within ~1 s of launch, then passing tokens to fresh
  wallets that sell; >= 90% of the money taken back within 3 days on 98% of those launches.
  Wallets fed by transfer took out 35-83% as much as the bundles themselves.
- Bitquery, Solana volume bots (30 days): 58% of DEX volume in their pools was not real; 95% of
  that was a buy and a sell in ONE transaction signed by two wallets; look-alike wallet groups
  (20 and 50 wallets) did $26B; mostly PumpSwap.
- "Meme Coin Factories" (CMU/EPFL, 15M coins): five manipulation classes (wash trading,
  creator-address obfuscation, coordinated sells, copycat coins, social-media manipulation) and
  "market-manipulation-as-a-service" tooling.
- "A Midsummer Meme's Dream" (35k tokens, 4 chains): 82.8% of high-return (> 100%) tokens show
  artificial growth (wash trading, liquidity-pool price inflation); pump-and-dumps/rugs follow.
- KOL research: 36k tweets / 180 influencers (Review of Accounting Studies): +1.83% day-1
  (+3.86% small caps), -2.24% by day 10, -6.53% by day 30. Memecoin KOL data: > 65% of
  KOL-promoted memecoins went to zero; ~1% of influencers ever promoted a 10x; larger accounts
  did worse. Practitioner maxim: "a KOL post is marketing, a KOL wallet is evidence." Kolscan
  (owned by pump.fun) publishes known KOL wallets and their trades in real time.
- Clustering practice: group early buyers by shared SOL funder, walk funding chains back to
  exchanges; bundles = same block/bundle; clusters = funding/transfer links; bundlers often
  20-50 fresh wallets funded with similar amounts. Jito: bundles of <= 5 atomic ordered txs,
  public bundle explorer, ShredStream (raw shreds, earlier than full blocks), low-latency send.
- Fake-follower / coordinated-campaign detection: unsupervised detection of coordinated
  fake-follower campaigns (EPJ Data Science); classic fake-follower features.

## 5. Proposed architecture (draft; critique it)
1. Ingest: Geyser (all pump.fun + PumpSwap + main AMMs) and possibly Jito ShredStream; pool
   creation / liquidity add-remove; Jito bundle membership per launch.
2. Wallet intelligence: per-wallet strictly-prior realized PnL, holding times, sniping history,
   funding source clusters (SOL transfer graph), token hand-offs (transfer graph), bundle
   co-membership, volume-bot fingerprints (one-tx round trips, look-alike wallet groups).
3. Insider-inventory tracker per coin: dev + bundle + funding-linked + transfer-recipient
   wallets; their net position over time; first distribution events (generalizes the validated
   winner-sell exit).
4. Influencer intelligence: KOL accounts (X) + known KOL wallets (Kolscan); per KOL,
   strictly-prior record of: wallet buy time vs post time vs price path; follower authenticity.
5. Pre-launch watchlist: accounts building audiences; match new coins at creation via the
   coin's metadata X link / ticker / dev wallet to watched accounts.
6. Legitimacy/risk score at decision time: launch score, bundle share, insider inventory share,
   dev selling, wash share, holder concentration, funding-cluster share, social breadth.
7. Policy: "safe passage" entries (e.g., ride a KOL from their wallet buy into their post; enter
   while the insider cohort is net-holding and retail inflow accelerates); exits on first insider
   distribution or winner-sell, plus recoup-then-ladder.
8. Gates: capture -> walk-forward on >= 3 tapes -> paper -> small live.

## 6. Questions (answer each, numbered)
Q1. Is the core thesis viable at our latency against block-0 bundles and insiders? Which
    "safe passage" mechanism (if any) is the most plausible real edge, and why?
Q2. Which signals are likely robust and which are illusory or easy for adversaries to fake?
Q3. Outcome labels: how should we define "real community / longevity" and "profitable passage"
    to avoid wash contamination, survivorship and look-ahead?
Q4. Wallet radar: what is worth building first (funding clusters, bundle detection, transfer
    hand-offs, volume-bot detection)? Is "anticipate bots buying to create our exit" viable?
Q5. Influencer intelligence: how to score KOLs so it is predictive (strictly prior), and is
    "front-run the KOL's own wallet into the post" testable and plausible?
Q6. Exits: is recoup-then-ladder sound on bonding curves / thin PumpSwap pools? Better design?
Q7. Give a prioritized experiment plan (first 2 weeks), each with a success criterion and a
    kill criterion, using data we can capture. What should we NOT build?
Q8. Your probability that some version yields positive net EV after costs at 0.05-1 SOL
    within 3 months; and the single biggest risk.

Format: <= 900 words. Sections: Verdict (2-3 sentences) / Answers Q1-Q8 / Top 3 risks.
