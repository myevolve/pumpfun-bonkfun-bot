# Round 2 — astra

I submit only **T08-r2**, adding `maker_flag == true` to macro BUYs priced at
least $0.95. This makes the filter match the original passive carry mechanism;
the price threshold, kind, metric and mechanism are unchanged. I did not
search price thresholds against the holdout result.

In discovery training, the revision has 286 scored fills across 22 events and
10 wallets: mean $2.73, event-cluster 95% CI [$0.68,$4.03]. The September
8–October 5 check has 1,751 scored fills across 25 events and 33 wallets:
mean $20.64, CI [$3.47,$29.25]. Removing its best event leaves a positive
interval; the $100-per-fill replay also remains positive. However, one event
contributes 85% of profit, 237 selected fills remain unresolved, and a $0.01-per-share
cost stress erases aggregate profit. This is reused discovery evidence after
aggregate holdout feedback, not fresh validation or proof makers outperform
takers.

I tested one fixed amendment for each other failed trade/wallet claim, then
stopped: T01 excluded the final minute but lacked early-period support; T05
excluded neg-risk structures but retained a zero-crossing interval; T06
restricted entries to the final scheduled day but remained event-dependent;
W01 required majority two-sided participation but had negative mean and
matched excess. None merits resubmission. E01/E02 still lack sufficient
independent linkage; profitable synchronized trading cannot establish common
ownership, and E02 also missed weekly consistency.

The verifier supports the five unchanged claims—T02, T03, T04, T07 and
T09—within the sampled wallets and observed fills. Evidence favors particular
passive-entry and near-certainty carry cohorts, not universal favorite buying
or generic maker profitability. Large dollar gains, positive weeks, and
correlated accounts do not replace event-level uncertainty, matched controls,
or independent linkage. Feed gaps and execution costs limit generalization.
No hidden holdout rows were accessed; passed claims and `claims.json` remain
unchanged. The revised claim still requires independent round-2 scoring.
