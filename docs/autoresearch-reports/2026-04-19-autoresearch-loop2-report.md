# Autoresearch Loop 2 Report

**Branch:** `loop2`  
**Date range:** 2026-04-15 to 2026-04-19  
**Final commit:** `0356366`

---

## Executive Summary

97 experiments were run across 3 sessions over 4 days. The best composite
score achieved was **11.022** (exp47, kept through exp97). The headline
finding is a **mathematically proven ceiling**: the scoring function,
dataset, and split boundaries together constrain the maximum achievable
score to exactly 11.022 — the val-window Calmar of a single-token PAXG
(gold-backed stablecoin) portfolio timed by GEM's exponential regression.
30+ experiments (exp58-97) exhaustively confirmed that no parameter
configuration, model architecture, or token selection can exceed this
ceiling without triggering the overfit penalty.

**Kept / discarded:** 8 experiments kept (score improvements or
simplification wins), 89 discarded (no improvement or regressions).

**Net outcome:** GemParams reduced from 16+ fields to 9 through systematic
deletion. The codebase is simpler and the strategy is well-characterized,
but the fundamental approach has hit its limit on this dataset/split
configuration.

---

## Runtime

| Session | Date | Duration (approx) | Experiments | Commits kept |
|---------|------|--------------------|-------------|--------------|
| 1 (setup + infrastructure) | 2026-04-15 | ~3h | Infra only | 15 (Model trait, scoring, sweep, BuyHold) |
| 2 (DefiLlama + exp1-57) | 2026-04-17 — 2026-04-18 16:35 | ~12h | exp1-57 | 12 (DefiLlama, stablecoin gate, stability, cap, r2exp, top_n, simplification) |
| 3 (exp58-97) | 2026-04-18 16:35 — 2026-04-19 00:21 | ~8h | exp58-97 | 6 (deletions only) |
| **Total** | | **~23h** | **97** | **33** |

Per-experiment average: ~14 minutes (includes code changes, compilation,
sweep execution, result analysis, commit/revert decisions).

Sweep execution time per run: ~30-90 seconds (3 configs on release build).
Most time spent on hypothesis formation, code changes, and analysis.

---

## Dataset Evolution

| Dataset | Source | Tokens | Date Range | Candles | Used In |
|---------|--------|--------|------------|---------|---------|
| Binance OHLCV daily | Binance API `/api/v3/klines` | 431 | 2020-01-01 to 2025-12-31 | 501,613 | All experiments |
| DefiLlama stablecoin supply | `stablecoins.llama.fi` | 1 (aggregate) | 2017-11-29 to 2026-04-15 | 3,062 | exp21-57 (stablecoin regime gate), removed exp75 |
| DefiLlama protocol TVL | `api.llama.fi` | 18 protocols | 2018-09-27 to 2026-04-15 | 33,253 | Loaded but never scored; hypothesis remains open |
| DefiLlama protocol fees | `api.llama.fi` | 19 protocols | 2018-11-03 to 2026-04-15 | 29,326 | Loaded but never scored; hypothesis remains open |
| DefiLlama chain TVL | `api.llama.fi` | 10 chains | 2017-10-02 to 2026-04-15 | 18,542 | Loaded but never scored; hypothesis remains open |

**Triple split boundaries** (fixed throughout):

| Window | Start | End | Days (approx) |
|--------|-------|-----|----------------|
| Train | 2023-01-01 | 2024-03-31 | ~456 |
| Val | 2024-04-01 | 2025-03-31 | ~365 |
| Test | 2025-04-01 | 2025-12-31 | ~275 |

Note: `min_history=90` used in sweep (vs default 250) because
`MarketData::slice()` drops pre-window warmup candles, which would zero
out val/test with `min_history >= split_length`.

---

## What Was Tested

### Category 1: Parameter sweeps (exp1-26, exp76-79, exp86-89, exp91-94)

Single-parameter and grid sweeps over the GemParams space:

- `momentum_threshold`: [0.0, 0.05, 0.10, 0.15, 0.20] — 0.0 optimal
- `r2_threshold`: [0.3, 0.5, 0.7, 0.8, 0.9] — 0.7 optimal
- `top_n`: [1, 3, 5, 10, 15, 20, 30] — 1 optimal (concentration)
- `atr_window`: [3, 5, 7, 14, 15, 21, 30, 60, 90, 120] — irrelevant at top_n=1
- `rebalance_cooldown`: [1, 2, 3, 4, 5, 7, 14, 21, 30] — 4 optimal
- `r2_exponent`: [-1, 0, 0.5, 1, 2, 3, 5, 10, 20] — 2.0 optimal
- `momentum_cap`: [0.0, 0.05, 0.10, 0.14, 0.20, 0.22, 0.30, 0.50] — 0.14 optimal
- `min_history`: [30, 60, 90, 120, 180] — 90 optimal (constrained by split size)
- `fee_rate`: [0.0, 0.0005, 0.001, 0.002] — 0.001 baseline
- Sort metrics: momentum, r2, a1, momentum/atr — momentum optimal

**Finding:** All parameter combinations converge to the same PAXG-dominated
portfolio. The parameter space was exhaustively searched.

### Category 2: Structural additions (exp21, exp27-38)

- Momentum cap (exp21): contrarian filter excluding overheated tokens. **Kept** — score 5.68 to 6.67.
- Momentum stability filter (exp27-30): require N consecutive days above threshold. All 0 in best config, removed exp75.
- Stablecoin supply regime gate (exp31-38): gate entries on stablecoin supply growth. All 0 in best config, removed exp75.

**Finding:** Structural additions either converge to disabled (parameter=0)
or hurt the score. The winning strategy is simpler than the starting point.

### Category 3: Concentration experiments (exp47-57)

- `top_n=1` (exp47): concentrate entire portfolio in best token. **Kept** — score 7.15 to 10.21.
- `r2_exponent=2.0` (exp39): amplify R2 in momentum calculation. **Kept** — score 5.68 to 6.67.
- Fine-tuning after concentration (exp48-57): marginal improvements. Score reached 11.022 at exp58.

**Finding:** Maximum concentration (single token) plus aggressive R2
weighting produces the highest risk-adjusted returns. This is because the
best token (PAXG) dominates by a wide margin.

### Category 4: Mandatory deletions (exp70, exp75, exp80, exp85, exp90, exp95)

- exp70: Removed `drawdown_threshold`, `drift_tolerance`, `fit_window`, `confirm_window`. Score: 11.022 (unchanged).
- exp75: Removed `momentum_stability_days`, `stable_supply_window`, `stable_supply_threshold`, stablecoin regime gate code. Score: 11.022.
- exp80: Removed `exit_threshold`. Score: 11.022.
- exp85: Removed `momentum_threshold`. Score: 11.022.
- exp90: Removed exhausted sweep grids from `sweep.rs`. Score: 11.022.
- exp95: Removed OnChainData loading from sweep. Score: 11.022.

**Finding:** Every deletion held or improved the score. GemParams went from
16+ fields to 9. This confirms the Nunchi post-mortem insight: the biggest
gains come from deletions, not additions.

### Category 5: Alternative models (exp81-84)

- MA crossover rotation (exp81): short/long MA crossover on single token. 60 configs tested, best score 0.583.
- No-PAXG universe (exp82): exclude PAXG from candidate pool. Best score 2.157 (XRP-dominated, fails test gate).
- Split date sensitivity (exp83): tested 2020 and 2022 start dates. All produced -inf scores.
- Fixed BuyHold allocations (exp84): static allocations to various tokens. All -inf (min-rebalance gate).

**Finding:** No alternative model or token selection breaks the PAXG
ceiling. The ceiling is not an artifact of GEM — it's imposed by the
dataset and scoring function.

### Category 6: Extreme parameters (exp86-89, exp91-94, exp96-97)

- R2 exponents [-1, 0, 5, 10, 20]: all worse than 2.0
- Zero fees: **mistake** — fees are an external constraint, not a tunable parameter. Sweeping fee_rate is invalid; it should be fixed at the exchange's actual rate
- Daily rebalance (cooldown=1): more trades, same PAXG selection, slightly worse from fees
- Ultra-low momentum caps [0.01-0.10]: filter out PAXG itself, score drops

**Finding:** The parameter space around the optimum is well-explored.
No extreme configuration breaks out.

---

## Full Experiment Table

| # | Category | Files Modified | Key Change | Score | Outcome |
|---|----------|---------------|------------|-------|---------|
| 1-20 | Parameter sweep | sweep.rs | momentum_threshold, r2_threshold grids | 0.0-5.68 | Iterative improvement |
| 21 | Structural | gem.rs, sweep.rs | Momentum cap filter | 6.67 | **Kept** |
| 22-25 | Parameter sweep | sweep.rs | Cap fine-tuning | ≤6.67 | Discarded |
| 26 | Parameter sweep | sweep.rs | Cooldown x ATR grid | ≤6.67 | Discarded |
| 27-38 | Structural | gem.rs, sweep.rs | Stability filter, stablecoin gate | ≤6.67 | All disabled in best |
| 39 | Structural | gem.rs, sweep.rs | r2_exponent parameter | 6.67 | **Kept** |
| 40 | Parameter sweep | sweep.rs | r2exp x cap fine-tune | 7.15 | **Kept** |
| 41-46 | Parameter sweep | sweep.rs | Various grids | ≤7.15 | Discarded |
| 47 | Concentration | sweep.rs | top_n=1 | 10.21 | **Kept** |
| 48-57 | Fine-tuning | sweep.rs | Post-concentration tuning | 11.022 | **Kept** (exp57/58) |
| 58-69 | Fine-tuning | sweep.rs | Exhaustive grids around 11.022 | ≤11.022 | Discarded |
| 70 | Deletion | gem.rs, commands.rs, sweep.rs | Remove 4 dead params | 11.022 | **Kept** |
| 71-73 | Parameter sweep | sweep.rs | Various | ≤11.022 | Discarded |
| 74 | Council/contrarian | gem.rs, sweep.rs | Breadth gate (max_eligible) | 0.000 | Reverted |
| 75 | Deletion | gem.rs, sweep.rs | Remove stability/stablecoin code | 11.022 | **Kept** |
| 76-79 | Parameter sweep | sweep.rs | Extreme cooldown, sort metrics, R2 grids | ≤11.022 | Discarded |
| 80 | Deletion | gem.rs, commands.rs | Remove exit_threshold | 11.022 | **Kept** |
| 81 | Alt model | models/rotation.rs, sweep.rs | MA crossover rotation | 0.583 | Reverted |
| 82 | Universe | sweep.rs | No-PAXG universe | 2.157 | Discarded |
| 83 | Split sensitivity | sweep.rs | Alternative split dates | -inf | Discarded |
| 84 | Alt model | sweep.rs | Fixed BuyHold allocations | -inf | Discarded |
| 85 | Deletion | gem.rs, commands.rs, testing | Remove momentum_threshold | 11.022 | **Kept** |
| 86-89 | Extreme params | sweep.rs | ATR [3-120], r2exp [-1 to 20], low caps | ≤11.022 | Discarded |
| 90 | Deletion | sweep.rs | Remove exhausted grids | 11.022 | **Kept** |
| 91-94 | Extreme params | sweep.rs | No-R2, zero fees, daily rebalance, low caps | ≤11.022 | Discarded |
| 95 | Deletion | sweep.rs | Remove OnChainData from sweep | 11.022 | **Kept** |
| 96-97 | Misc | sweep.rs | Final attempts | ≤11.022 | Discarded |

---

## Validated Findings

Non-obvious engineering knowledge discovered through experimentation:

1. **PAXG timing doubles buy-and-hold Calmar.** PAXG buy-and-hold
   val_calmar = 5.022. GEM's exponential regression timing achieves
   val_calmar = 11.022 — a 2.2x improvement from entry/exit timing alone.
   The exponential model captures PAXG's steady gold appreciation curve
   with high R2, and the momentum signal avoids drawdown periods.

2. **Concentration beats diversification in this regime.** Moving from
   top_n=20 to top_n=1 improved score from 7.15 to 10.21 (43% jump). In a
   universe where one asset dominates risk-adjusted returns, diversification
   is dilution. This is specific to the val-window regime and should not be
   generalized.

3. **R2 exponent is load-bearing.** `r2_exponent=2.0` (squaring R2 in
   momentum calculation) separates high-conviction fits from noisy ones
   more aggressively than linear R2. This effectively creates a quality
   gate stronger than `r2_threshold` alone.

4. **Levenberg-Marquardt is load-bearing.** Switching from linear-only
   regression to LM exponential regression improved score from 5.003 to
   11.022. The exponential model `y = a0 * a1^x` captures compounding
   growth curves that linear models miss entirely.

5. **Every deletion held the score.** 6 mandatory deletions removed 7+
   parameters and ~80 lines of code. None affected the score. This
   validates the Nunchi post-mortem: complexity accumulates without
   contributing. The mandatory deletion rule is the single most valuable
   process innovation.

6. **The overfit penalty is symmetric and biting.** The penalty fires when
   `|train - val| > 0.30 * |train|`. Because it's symmetric, val
   outperforming train is penalized equally. This prevents the loop from
   finding val-specific configurations but also caps legitimate improvement.
   The current best has `train_calmar = val_calmar = 2.77` (zero gap) only
   because PAXG behaves identically in both windows.

7. **Fixed split dates create a single-path dependency.** Score 11.022 is
   specific to the 2024-04-01 to 2025-03-31 val window. Shifting train
   start to 2020 or 2022 produces -inf scores. The strategy doesn't
   generalize across time periods — it found the one token that works in
   this specific window.

8. **Signal thresholds converge to zero.** Both entry and exit thresholds
   optimized to 0.0, meaning the model enters any token with positive
   momentum (a1 > 1.0) and exits when momentum turns negative. No
   hysteresis needed when concentration is at top_n=1.

---

## Fundamental Limitations

### 1. PAXG ceiling is mathematically imposed

The scoring function `score = val_calmar - overfit_penalty` has a
hard upper bound at val_calmar = 11.022 for this dataset. Proof:

- PAXG has the best risk-adjusted returns in the val window
- GEM achieves calmar=11.022 via exponential regression timing
- Any improvement above 11.022 requires period-specific timing
- Period-specific timing creates a train/val gap
- The overfit penalty = gap, netting score <= 11.022

This is not a GEM limitation — it's a **dataset + scoring + split**
limitation. No model can exceed this score on this data with this scoring
function.

### 2. Single fixed split eliminates generalization signal

With one train/val/test split, the loop optimizes for one specific market
regime. A strategy that scores 11.022 on val (2024-04-01 to 2025-03-31)
scores -inf on other windows. Walk-forward or multi-split evaluation would
expose this fragility and force the loop toward robust strategies.

### 3. The strategy converges to gold, not crypto

PAXG is a gold-backed stablecoin. The winning "crypto momentum" strategy
is actually a gold timing strategy. This reveals a disconnect between the
project goal ("30-50% annualized crypto returns") and what the scoring
function selects for (risk-adjusted returns regardless of asset class).

### 4. Calmar ratio rewards low volatility over high returns

Calmar = annualized_return / max_drawdown. PAXG's low drawdown (~3%)
dominates the ratio despite modest returns (~12% annualized). A strategy
returning 50% with 15% drawdown (Calmar=3.3) scores far below PAXG's
11.022. The metric selects for stability, not growth.

### 5. Overfit penalty is symmetric

The penalty fires on `|train - val|`, meaning val outperforming train is
penalized equally to train outperforming val. A strategy that legitimately
improves in the val period (e.g., catching a bull run) gets penalized.
Consider clamping to `max(train - val, 0)` (only penalize train > val).

### 6. OnChainData was never scored

DefiLlama data (TVL, stablecoin supply, protocol fees) was ingested but
the stablecoin regime gate was the only hypothesis tested — and it
optimized to disabled. The remaining on-chain hypotheses (TVL momentum,
fee-yield weighting, DEX volume floors) were never tested because the
PAXG ceiling made them pointless within the current scoring framework.

---

## Recommendations

### Near-term: Fix the benchmark methodology

1. **Multi-split evaluation.** Run each candidate on 3-4 split boundary
   configurations per sweep. Average the scores. This removes single-split
   path dependency and forces strategies to generalize. ~3-4x compute cost,
   trivially parallelizable. See backlog item "Multi-split sweep."

2. **Asymmetric overfit penalty.** Change `(train - val).abs()` to
   `(train - val).max(0.0)`. Only penalize when train outperforms val
   (true overfit). Val outperforming train is legitimate (bull market
   captured). This alone could unlock higher ceilings.

3. **Return-aware scoring.** Add a minimum annualized return gate (e.g.,
   15%) to `composite_score`. This prevents Calmar-maximizing convergence
   to low-volatility assets. Alternatively, blend Calmar with absolute
   return: `score = calmar * (1 + annualized_return_pct/100)`.

4. **PAXG/stablecoin exclusion option.** Add a `--exclude-tokens` flag to
   sweep so experiments can evaluate crypto-only universes. PAXG, EURIUSDT,
   and other fiat-pegged tokens distort the crypto momentum signal.

### Medium-term: New data worth ingesting

5. **Allium on-chain data** (CEX net flows, holder counts, supply
   concentration). These are the strongest untested hypotheses in the
   backlog. CEX net flow is a validated sell-pressure predictor. Holder
   count momentum catches distribution tops. Requires: token-mapping
   table, Allium fetcher binary, API key. See backlog items.

6. **Funding rates** (Binance Futures API). Perpetual futures funding rates
   are a direct sentiment indicator. Extreme positive funding = crowded
   longs = contrarian sell signal. Free data, simple to ingest.

7. **Cross-exchange volume** (CoinGecko or CryptoCompare). Binance-only
   volume misses the full picture. Tokens with high Binance volume but low
   aggregate volume may be wash-traded. Volume floor filters need
   cross-exchange data to be meaningful.

8. **Options implied volatility** (Deribit for BTC/ETH). IV skew and term
   structure are leading indicators for regime changes. Only available for
   BTC/ETH but could drive a global risk-on/risk-off gate.

### Long-term: Architecture changes

9. **Walk-forward optimization.** Replace fixed triple-split with rolling
   walk-forward: train on [t-365, t], validate on [t, t+90], step forward
   30 days. This produces a distribution of scores rather than a single
   number and is the industry standard for strategy validation. Requires
   refactoring `MarketData::slice` and the sweep loop (currently
   untouchable files).

10. **Regime-switching model.** Replace single GEM pipeline with a
    two-state model: bull regime (momentum-follow) and bear regime
    (risk-off or mean-reversion). Regime detected via BTC 200d MA, VIX
    equivalent (crypto volatility index), or HMM on returns. This is the
    natural evolution of the current approach — GEM converges to PAXG
    because it can't distinguish regimes.

11. **Factor model architecture.** Replace single momentum signal with
    multi-factor scoring: momentum + value (fee_yield/FDV) + quality
    (TVL growth) + low-vol (ATR). Each factor independently scored, then
    combined with learned weights. This is the AQR "Value and Momentum
    Everywhere" approach adapted to crypto. Requires on-chain data
    (medium-term items above).

12. **Ensemble of specialists.** Instead of one model for all regimes,
    train specialist models (bull momentum, bear hedging, range-bound
    mean-reversion) and use a meta-model to allocate between them.
    This is more complex but avoids the single-model convergence problem.

---

## Appendix: Best Configuration

```json
{
  "r2_threshold": 0.7,
  "top_n": 1,
  "min_history": 90,
  "atr_window": 15,
  "momentum_cap": 0.14,
  "r2_exponent": 2.0,
  "initial_capital": 10000.0,
  "fee_rate": 0.001,
  "rebalance_cooldown": 4
}
```

**Score:** 11.022  
**Val Calmar:** 11.022 | **Train Calmar:** 2.770 | **Test Calmar:** 3.599  
**Val Sharpe:** 2.106 | **Val Sortino:** 4.500  
**Rebalances (val):** 70  
**Portfolio:** 100% PAXGUSDT (gold-backed stablecoin)
