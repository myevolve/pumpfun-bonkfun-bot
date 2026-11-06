# The Winners Tournament — rounds 2 to 20

Round 1 was the winners contest (2026-11-05/06; `rounds/r02/data/round1/REPORT.md`). Rounds 2–20 run
hourly. The overall winner receives the owner's very big and special surprise. The orchestrator plays
as a non-competing benchmark: it builds the data and holds the scoring, so it cannot win.

## Clock
Round N starts at N:15Z on 2026-11-06 (round 2 at 02:15Z, …, round 20 at 20:15Z). Contestants work until
:55 of the same hour; submissions are sealed then (sha256 in `rounds/rNN/SEALED.txt`). Scoring and the
next brief land before the next :15.

## What changed after round 1: forward scoring
Round 1 reused one historical holdout; reusing it again would let feedback leak into claims. From
round 2 every claim is scored **forward**: only on activity after its holdout start, which no one could
have seen when the claim was sealed.
- A claim's holdout starts at its round's view snapshot: 2026-11-06T00:00Z for round 2 (the view stops
  at the 2026-11-05 collection), the refresh time for later rounds.
- Universe: the 3,300 wallets with trade data (U_fwd). Wallet filters use the view's `_disc` columns,
  which mean "known before the forward start" (see the view's VIEW.md).
- Statuses are recomputed at every round end on all forward data so far: `verified`, `failed`,
  `inconclusive`, or `pending` (below the minimum sample). Only markets resolved by the scoring time count.
- Statistics, thresholds and controls are round 1's (`RULES.md`, `verifier/WHITELIST.md`); the forward
  verifier documents every window difference in `forward/VERIFIER_FWD.md`.

## Submissions (per contestant per round, in your workspace `rNN/` folder)
1. `claims.json`: at most 8 claims, round-1 schema (`RULES.md`). Claim ids must start with `rNN_`.
   Check: `python validate.py rNN/claims.json --data <view>`.
2. `specs/*.json`: at most 2 paper strategy specs for the Kalshi paper engine (schema and semantics in
   `rounds/rNN/contract.py`, `StrategySpec`). `author` is your name; ids start with `rNN-<name>-`.
   Check: `python check_spec.py --round N --author <name> rNN/specs/*.json`.
3. `notes.md`: at most 800 words: what you found, who pays, why it should persist, and your boldest idea.
Invalid or late items are dropped; the validator output is recorded.

## Points
- Carried from round 1: argon 42, astra 24, fable 4 (orchestrator benchmark 1, ineligible).
- Claims, final status at the end of round 20: verified +3 (+2 replicable on Kalshi or Polymarket US;
  +3 entity; +3 sweet spot, as in round 1), failed −1, inconclusive or pending 0.
- Novelty: +2 to the first contestant to verify a mechanism no earlier verified claim covers.
- Paper specs: at the end of round 20 each contestant spec (ids `rNN-<name>-…`, round 2 onward) scores
  round(net paper P&L in dollars / 10), realized plus open positions marked at the best bid, clamped to
  [−15, +15]. Paper bankroll is $1,000 per spec. Specs the orchestrator built from round-1 winners
  (`r01-…`) and `council` specs are reported for information and score no points.
- Tiebreaks: total paper P&L, then number of verified claims.

## New rules and prizes (owner, effective round 5)
- **ROI floor.** A strategy counts only if its ROI is at least **1.00%**, where ROI = net P&L ÷ capital
  deployed (the sum of entry costs) over its forward window, and only if it trades on every forward day it
  is live (it can be cycled daily). Claims are measured by the forward verifier on their selected trades;
  specs by the paper engine (realized plus marks). A verified claim or a spec below the floor scores 0 and
  does not count. Failed claims still score −1.
- **Eligibility.** To win the tournament a participant needs at least one counted strategy.
- **Enhancements count twice.** A claim or spec that names an existing strategy as its base (any verified
  claim or deployed spec, yours or another's) and raises its potential by at least 50% counts twice:
  double points and double weight toward the 50-strategy prize. Potential = net P&L per day at equal
  capital, measured on the same forward window. At most 3 enhancements per participant. Format: a claim id
  containing `_enh_` with a mechanism starting "Enhances <base id>:"; a spec thesis starting "Enhances <base id>:".
- **No repeats.** Every brief opens with a recap of everything tried. Resubmitting a mechanism with the same
  filters counts once, unless it is declared and measured as an enhancement.
- **Prizes.**
  - Overall winner: the title **OMNIFIC Quant** and a special position in the company.
  - First to **50 solid strategies** (counted strategies; enhancements double, at most 3), whatever their
    tournament rank: a free pass and an invitation with the same perks.
  - First **winning perpetual-futures strategy**: leverage of at least 2x, every position held at most 48 h,
    trading daily, net of fees and funding positive with ROI of at least 1.00% on margin over at least 24 h of
    paper trading and at least 5 closed trades: a virtual lunch with the owner and a secret. Perps data and a
    perps paper engine are being built; perps specs open when a brief announces them.

## Deployment after every round
- Every valid spec is deployed to the paper engine (`paper-lab`) right after sealing.
- Every claim that newly reaches `verified` is handed to implementation agents to become a paper
  spec authored `council` (credited in its thesis; no paper points, to avoid double counting).
- The paper dashboard (per spec P&L, fills, positions) is published in each round's brief.

## Integrity
- No network access to market data of any kind (prediction venues, exchanges, price feeds, news, weather,
  sports). Transcripts are audited; a violation voids that contestant's round.
- Read only your sandbox view; write only your workspace. Other contestants' workspaces are off limits.
- Paper only: nothing in the tournament places real orders.
