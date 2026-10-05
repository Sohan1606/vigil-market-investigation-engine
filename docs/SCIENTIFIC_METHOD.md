# Scientific method, leakage defences, and how to read VIGIL's results

## The one rule
A model may only ever see information that existed before the moment it is predicting. Everything
below exists to enforce that rule and to prove it is enforced.

## Point-in-time correctness
- Features at session *t* use only sessions ≤ *t*; the label is `close(t+h) > close(t)`, observed
  at *t+h*. `assert_no_future_columns()` rejects any column named `fwd_`, `label_`, `target_`,
  `future_` or `next_` from a feature set.
- News is joined on **publication timestamp**, never on article date-of-event: an article
  published at 18:40 cannot appear in that session's features.
- Every forecast carries an explicit `information_cutoff` and is rejected by the audit if the
  cutoff is not strictly before the target.

## Validation protocol
- **Walk-forward (rolling origin) only.** 3-year initial train window, 12-month test windows,
  14 folds. No random splits anywhere — `tournament_h1.json` records
  `protocol.random_split_used: false` and a test asserts it.
- A **purge gap of `horizon` sessions** sits between train and test, so a label that overlaps the
  training window cannot be learned.
- Scaling and imputation statistics are fit **inside the training fold**. Sequence windows for the
  deep models are built inside a fold and never cross its boundary.

## Automated leakage tests (`tests/test_leakage.py`)
| Test | What would fail it |
|---|---|
| no future-named columns | a `fwd_*` column in the feature set |
| no feature correlates \|r\|>0.30 with the forward return | a leaked target |
| the pre-trade edge contains no `fwd_ret`/`y_true` term | the v0.9 defect where the abstention gate used the future return's magnitude |
| label equals next-session direction | an off-by-one label |
| folds chronological + purged | a shuffled or overlapping split |
| the guard rejects a deliberately shuffled split | a guard that does nothing |
| tournament declares no random split | a protocol regression |
| future-dated news produces zero news features | a timestamp join bug |

`tests/test_pit_and_replay.py` additionally proves REPLAY uses **zero** sessions after its anchor
and hides every headline published after the cutoff.

## How to read the numbers
Three pairs of ideas are deliberately kept apart, in the UI and in the code:

1. **Data reliability** (is the input trustworthy?) vs **forecast confidence** (how sharp is the
   probability?) vs **decision trust** (should we act?). A perfect probability on quarantined data
   still fails the gate.
2. **Forecast accuracy** vs **policy behaviour** vs **outcome quality**. These are reported as
   separate numbers and never combined into one score: abstaining is a behaviour to be reported,
   not an achievement to be rewarded (see the table below).
3. **VIGIL health** (is the system working?) vs **forecast trust** (is this prediction usable?).

## Execution convention (one convention, used everywhere)
Signals, backtests and research arms all obey the same accounting, stated here once:

- features and the calibrated probability for session *t* use information up to the **close of session t**;
- the position is opened at the **close of session t** and closed at the **close of session t+h**;
- the realised return is exactly the label: `fwd_ret_1[t] = close[t+1] / close[t] - 1`;
- every position pays the full **round trip** = 2 x (commission + slippage + impact) = **18.0 bps**;
- positions are equal-weighted across the symbols traded in a session; no leverage, no compounding
  of overlapping signals, no financing, no taxes, no borrow constraint on shorts.

The one optimism in this convention is disclosed rather than hidden: it assumes the session-*t*
close is obtainable for a signal computed from that same close. **ADAPTIVE_GATE_T1** re-runs the
identical policy with execution delayed one session and is published next to it
(-8.928% → -12.964% annualised).

## How decision quality is reported (and what was withdrawn)
v0.9 published a single `good_decision_rate_pct` of 99.18%. It counted every abstention below the
cost threshold as a good decision, so a system that never acted would have scored 100%. **It has
been deleted.** The replacement set separates what was previously conflated:

| Metric | Value (h=1) | Definition |
|---|---|---|
| Forecast accuracy | 51.32% | directional hit rate over all 23331 evaluated opportunities |
| Selective accuracy | 53.2% | hit rate on the 953 opportunities the gate acted on |
| Action coverage | 4.08% | share of opportunities acted on |
| Abstention rate | 95.92% | reported, never scored |
| Net return when acted | -20.77 bps | mean signed forward return minus the round trip |
| **Decision utility** | **-0.848 bps** | coverage x net return, i.e. value per evaluated opportunity — abstaining contributes exactly 0 |
| Downside avoidance | 12.4 bps | minus the net return the refused trades would have produced |
| Outcome quality | net win rate 49.42%, median -2.77 bps | realised outcome of the acted subset only |

Every definition is also embedded in `reports/results/decision_quality_h1.json` under
`metric_definitions`, and `tests/test_decision_metrics.py` fails the build if the withdrawn metric
reappears or if the utility identity breaks.

## The results, as measured
- Random Forest wins the h=1 tournament: Brier **0.24973**,
  ROC-AUC **0.5208**, accuracy 51.74% — a real but very small edge over a 0.25 coin.
- The adaptive ensemble (0.25013) is **worse** than its best member. Reported, not hidden.
- Calibration improved ECE 0.01740 → 0.01216 but worsened Brier
  0.25013 → 0.25036. Reported.
- Conformal 80% intervals achieved **80.24%** empirical coverage at
  4.03% mean width. Honest win.
- Cost-aware backtest, net of 18.0 bps round-trip: buy & hold
  **13.022%** ann (Sharpe 0.371);
  always-long-signal -27.907%;
  model -16.229%;
  adaptive gate **-8.928%** with 82.13% session abstention;
  the same gate executed one session late (**ADAPTIVE_GATE_T1**) -12.964%.
  **The daily directional edge does not survive costs.**
- Experiments A–E (adding context, news, regime awareness, delayed data) are all **NOT SUPPORTED**
  (A_PRICE_ONLY=REFERENCE, B_PRICE_CONTEXT=NOT SUPPORTED, C_WITH_NEWS=NOT SUPPORTED, D_REGIME_AWARE=NOT SUPPORTED, E_DELAYED_DATA=NOT SUPPORTED, F_ABSTENTION_POLICY=SUPPORTED).
  AUC rises while Brier worsens from A to D — sharpness bought at the cost of calibration.
- Experiment F (abstention policy) is **SUPPORTED**: trading everything returns
  -13.98 bps per trade, the
  cost-gated policy 2.02 bps per trade
  (0.401 bps per evaluated
  opportunity at 80.15% abstention). The gate
  uses only pre-trade information. This is a single-model policy study — the portfolio backtest above
  still loses money.
- Failure Lab: 48.68% miss rate over 23,331 predictions.

## News coverage, reported as two numbers
- **DIRECT SESSION COVERAGE 2.0933%** — (symbol, session) pairs with a headline published on that session.
- **ROLLING 5-DAY CONTEXT ROWS 5.675%** — modelling rows with a headline anywhere in the trailing 5 sessions (1,878 of 33,661 rows). This is what the news features see.

The aggregation is **timestamp-aware** (only headlines published at or before the session close),
which is a statement about information availability, not about causation.

## Negative results are results
VIGIL is built to be able to say "this did not work". Five of six hypotheses are not supported,
the ensemble lost to its own member, and the strategy loses money after costs. None of it was
tuned away, and `tests/test_integrity.py` fails the build if the experiment set ever contains no
NOT SUPPORTED verdict or the backtest ever contains no losing strategy.


Rolling 5-day context (5.675%) covers trailing sessions with news context.
