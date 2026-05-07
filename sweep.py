"""Deterministic encoding of the autoresearch contract over the bear portfolio.

This script is the public, runnable counterpart to the historical Karpathy-style
autoresearch loop runs that produced the post-mortems in
``docs/autoresearch-reports/``. The historical runs were LLM-driven against a
private Rust framework over 437 tokens; this script is hand-driven Python over
the 4-token bear portfolio used in ``notebooks/gem_bear_models.org``. Same
scoring shape, same deletion mandate, same council mode -- smaller universe,
deterministic hypothesis selection.

The point is to make the contract executable and citable. Every claim in the
companion blog post can be reproduced by running this file.

Usage:
    python sweep.py [--max-experiments N] [--seed S]

Outputs:
    results/bear_sweep_results.tsv -- one row per experiment, append-only.
    stdout                         -- per-experiment summary line.

Design notes:
    * Parameter axes mirror Loop 3 Phase 2 (``top_n`` x ``r2_threshold`` x
      ``rebalance_cooldown``), pinned to the bear-portfolio universe.
    * The mandatory deletion rule and council mode are encoded as a small
      hand-written vocabulary -- in the original loop the LLM picked from a
      similar shortlist; here the choice is deterministic so the run is
      reproducible.
    * Scoring follows ``program.md`` (annualized return * drawdown dampener *
      diversification bonus), with the hard-rejection gate evaluated under a
      1.5x fee stress.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(REPO_ROOT, "data", "bear_portfolio_candles.csv")
RESULTS_PATH = os.path.join(REPO_ROOT, "results", "bear_sweep_results.tsv")


# ---------------------------------------------------------------------------
# GEM primitives -- mirror notebooks/gem_bear_models.org for a self-contained
# script. If the notebook diverges, the notebook is the source of truth and
# this file should be brought into sync.
# ---------------------------------------------------------------------------


def r_squared(y: np.ndarray, y_hat: np.ndarray) -> float:
    ss_total = np.sum((y - np.mean(y)) ** 2)
    ss_residual = np.sum((y - y_hat) ** 2)
    if ss_total < 1e-12:
        return 0.0
    return 1.0 - ss_residual / ss_total


def average_true_range(close: np.ndarray, high: np.ndarray, low: np.ndarray) -> float:
    n = len(close)
    if n < 2:
        return 0.0
    tr_sum = 0.0
    for i in range(1, n):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        tr_sum += tr
    return tr_sum / (n - 1)


def fit_linear_logspace(x: np.ndarray, y: np.ndarray):
    ln_y = np.log(y)
    X = np.column_stack([np.ones_like(x), x])
    beta, _, _, _ = np.linalg.lstsq(X, ln_y, rcond=None)
    return beta[0], beta[1]


def exponential_model(x, a0, a1):
    return a0 * np.power(a1, x)


def fit_exponential(x: np.ndarray, y: np.ndarray, initial: tuple):
    try:
        popt, _ = curve_fit(
            exponential_model, x, y, p0=initial, method="lm", maxfev=5000
        )
        a0, a1 = popt
        if np.isfinite(a0) and np.isfinite(a1):
            return a0, a1
    except (RuntimeError, ValueError):
        pass
    return initial


@dataclass
class Position:
    token: str
    weight: float = 0.0
    momentum: float = 0.0
    r2: float = 0.0
    a1: float = 0.0
    atr: float = 0.0


def fit_token_exponential(
    candles: pd.DataFrame, atr_window: int = 15, r2_exponent: float = 1.0
) -> Optional[Position]:
    if candles.empty:
        return None

    token = candles.iloc[0]["token"]
    x = np.arange(len(candles), dtype=float)
    closes = candles["close"].values
    highs = candles["high"].values
    lows = candles["low"].values

    if np.any(closes <= 0):
        return None

    try:
        b0, b1 = fit_linear_logspace(x, closes)
    except Exception:
        return None

    initial = (np.exp(b0), np.exp(b1))
    a0, a1 = fit_exponential(x, closes, initial)

    y_hat = exponential_model(x, a0, a1)
    r2 = r_squared(closes, y_hat)
    momentum = (r2 ** r2_exponent) * (a1 - 1.0) * 100.0

    lookback = min(len(candles), atr_window)
    mean_close = np.mean(closes)
    if lookback > 1 and mean_close > 0:
        atr = (
            average_true_range(
                closes[-lookback:], highs[-lookback:], lows[-lookback:]
            )
            / mean_close
        )
    else:
        atr = 0.0

    return Position(token=token, momentum=momentum, r2=r2, a1=a1, atr=atr)


# ---------------------------------------------------------------------------
# Strategy parameters and ablation flags
# ---------------------------------------------------------------------------


@dataclass
class GemParams:
    r2_threshold: float = 0.5
    top_n: int = 5
    atr_window: int = 15
    fit_window: int = 30
    momentum_cap: float = 0.14
    r2_exponent: float = 2.0
    initial_capital: float = 10_000.0
    fee_rate: float = 0.003
    rebalance_cooldown: int = 7

    # Ablation flags. The deletion mandate flips these to disable
    # currently-active components of the strategy. Defaults reflect the
    # production stack for the bear specialist.
    use_r2_filter: bool = True
    use_growth_filter: bool = True
    use_momentum_cap: bool = True
    use_inverse_vol_weighting: bool = True
    pick_lowest_momentum: bool = False  # contrarian council move


def build_portfolio(candidates: list[Position], params: GemParams) -> list[Position]:
    filtered = list(candidates)

    if params.use_growth_filter:
        filtered = [p for p in filtered if p.a1 > 1.0]
    if params.use_r2_filter:
        filtered = [p for p in filtered if p.r2 > params.r2_threshold]
    if params.use_momentum_cap and params.momentum_cap > 0.0:
        filtered = [p for p in filtered if p.momentum < params.momentum_cap]

    filtered.sort(key=lambda p: p.momentum, reverse=not params.pick_lowest_momentum)
    filtered = filtered[: params.top_n]

    filtered = [p for p in filtered if p.atr > 0.0]
    if not filtered:
        return []

    if params.use_inverse_vol_weighting:
        inv_vols = [1.0 / p.atr for p in filtered]
        total = sum(inv_vols)
        for p, iv in zip(filtered, inv_vols):
            p.weight = iv / total
    else:
        equal = 1.0 / len(filtered)
        for p in filtered:
            p.weight = equal

    return filtered


# ---------------------------------------------------------------------------
# Portfolio bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class HeldPosition:
    quantity: float
    entry_price: float


@dataclass
class DaySnapshot:
    total_value: float
    positions: dict


class PortfolioState:
    def __init__(self, initial_capital: float):
        self.cash = initial_capital
        self.positions: dict[str, HeldPosition] = {}
        self.peak_value = initial_capital
        self.history: list[DaySnapshot] = []

    def buy(self, token: str, price: float, amount: float, fee_rate: float):
        fee = amount * fee_rate
        net_amount = amount - fee
        quantity = net_amount / price
        self.cash -= amount
        if token in self.positions:
            self.positions[token].quantity += quantity
        else:
            self.positions[token] = HeldPosition(quantity=quantity, entry_price=price)

    def sell(self, token: str, price: float, quantity: float, fee_rate: float):
        gross = quantity * price
        fee = gross * fee_rate
        self.cash += gross - fee
        if token in self.positions:
            self.positions[token].quantity -= quantity
            if self.positions[token].quantity <= 1e-12:
                del self.positions[token]

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            pos.quantity * prices.get(token, pos.entry_price)
            for token, pos in self.positions.items()
        )
        return self.cash + pos_value

    def actual_weights(self, prices: dict[str, float]) -> dict[str, float]:
        total = self.total_value(prices)
        if total <= 0:
            return {}
        return {
            token: (pos.quantity * prices.get(token, pos.entry_price)) / total
            for token, pos in self.positions.items()
        }

    def record_snapshot(self, prices: dict[str, float]):
        total = self.total_value(prices)
        self.peak_value = max(self.peak_value, total)
        pos_values = {
            token: pos.quantity * prices.get(token, pos.entry_price)
            for token, pos in self.positions.items()
        }
        self.history.append(DaySnapshot(total_value=total, positions=pos_values))


def should_rebalance(
    current_holdings: dict[str, float],
    momentums: dict[str, float],
    entry_threshold: float = 0.0,
    exit_threshold: float = 0.0,
) -> bool:
    for token, momentum in momentums.items():
        is_held = token in current_holdings
        if not is_held and momentum > entry_threshold:
            return True
        if is_held and momentum < exit_threshold:
            return True
    return False


def is_in_cooldown(
    current_day: int, last_rebalance_day: Optional[int], cooldown_days: int
) -> bool:
    if last_rebalance_day is None:
        return False
    return cooldown_days > 0 and (current_day - last_rebalance_day) < cooldown_days


def rebalance_to_targets(
    state: PortfolioState,
    target_weights: dict[str, float],
    prices: dict[str, float],
    fee_rate: float,
):
    total_value = state.total_value(prices)
    min_trade_value = total_value * 0.001

    for token in list(state.positions.keys()):
        price = prices.get(token, state.positions[token].entry_price)
        current_qty = state.positions[token].quantity
        current_value = current_qty * price
        target_value = target_weights.get(token, 0.0) * total_value
        sell_value = current_value - target_value
        if sell_value > min_trade_value:
            sell_qty = sell_value / price
            state.sell(token, price, sell_qty, fee_rate)

    total_value = state.total_value(prices)

    for token, target_weight in target_weights.items():
        price = prices.get(token)
        if price is None:
            continue
        target_value = total_value * target_weight
        current_value = 0.0
        if token in state.positions:
            current_value = state.positions[token].quantity * price
        buy_amount = target_value - current_value
        if buy_amount > min_trade_value:
            buy_amount = min(buy_amount, state.cash)
            if buy_amount > 0:
                state.buy(token, price, buy_amount, fee_rate)


# ---------------------------------------------------------------------------
# Backtest + metrics
# ---------------------------------------------------------------------------


def gem_backtest(
    params: GemParams,
    candles_by_token: dict[str, pd.DataFrame],
    fit_fn: Callable = fit_token_exponential,
) -> dict:
    eligible = candles_by_token

    all_timestamps = sorted(
        set(ts for c in eligible.values() for ts in c["timestamp"].values)
    )
    if not all_timestamps:
        return _empty_metrics()

    price_index = {
        token: dict(zip(c["timestamp"].values, c["close"].values))
        for token, c in eligible.items()
    }

    state = PortfolioState(params.initial_capital)
    rebalance_count = 0
    last_rebalance_day = None
    target_weights: dict[str, float] = {}

    for day_idx, timestamp in enumerate(all_timestamps):
        prices = {
            token: ts_map[timestamp]
            for token, ts_map in price_index.items()
            if timestamp in ts_map
        }

        fit_results = {}
        for token, candles in eligible.items():
            up_to = candles[candles["timestamp"] <= timestamp]
            if params.fit_window > 0 and len(up_to) > params.fit_window:
                up_to = up_to.iloc[-params.fit_window :]
            if len(up_to) >= 2:
                pos = fit_fn(up_to, params.atr_window, params.r2_exponent)
                if pos is not None:
                    fit_results[token] = pos

        need_rebalance = False
        if day_idx == 0:
            need_rebalance = True
        elif not is_in_cooldown(day_idx, last_rebalance_day, params.rebalance_cooldown):
            momentums = {t: p.momentum for t, p in fit_results.items()}
            holdings = {t: p.quantity for t, p in state.positions.items()}
            need_rebalance = should_rebalance(holdings, momentums)

        if need_rebalance:
            candidates = list(fit_results.values())
            portfolio = build_portfolio(candidates, params)
            target_weights = {p.token: p.weight for p in portfolio}
            rebalance_to_targets(state, target_weights, prices, params.fee_rate)
            rebalance_count += 1
            last_rebalance_day = day_idx

        state.record_snapshot(prices)

    return _compute_metrics(state, rebalance_count)


def _empty_metrics() -> dict:
    return {
        "total_return_pct": 0.0,
        "annualized_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "calmar_ratio": 0.0,
        "rebalance_count": 0,
        "hhi": 1.0,
        "final_value": 0.0,
    }


def _compute_metrics(state: PortfolioState, rebalance_count: int) -> dict:
    history = state.history
    if len(history) < 2:
        return _empty_metrics()

    values = np.array([s.total_value for s in history])
    initial_value = values[0]
    final_value = values[-1]
    total_return = (final_value - initial_value) / initial_value

    days = len(history)
    years = days / 365.0
    annualized_return = (1.0 + total_return) ** (1.0 / max(years, 1e-9)) - 1.0

    peak = np.maximum.accumulate(values)
    drawdowns = (peak - values) / peak
    max_drawdown = float(np.max(drawdowns))

    daily_returns = np.diff(values) / values[:-1]
    mean_r = float(np.mean(daily_returns))
    std_r = float(np.std(daily_returns))
    sharpe = (mean_r / std_r * np.sqrt(252)) if std_r > 1e-12 else 0.0

    downside = daily_returns[daily_returns < 0]
    downside_dev = (
        np.sqrt(np.sum(downside ** 2) / len(daily_returns))
        if len(daily_returns) > 0
        else 0.0
    )
    sortino = (mean_r / downside_dev * np.sqrt(252)) if downside_dev > 1e-12 else 0.0
    calmar = (annualized_return / max_drawdown) if max_drawdown > 1e-12 else 0.0

    # Average HHI over the final-week of holdings, as a stand-in for the
    # production "avg over rebalance days" metric. Bear portfolio rebalances
    # rarely; this is close enough for the diversification bonus.
    last_snap = history[-1]
    last_total = last_snap.total_value if last_snap.total_value > 0 else 1.0
    weights = [v / last_total for v in last_snap.positions.values()]
    cash_weight = 1.0 - sum(weights)
    if cash_weight > 1e-9:
        weights.append(cash_weight)
    hhi = float(sum(w * w for w in weights)) if weights else 1.0

    return {
        "total_return_pct": total_return * 100,
        "annualized_return_pct": annualized_return * 100,
        "max_drawdown_pct": -max_drawdown * 100,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "rebalance_count": rebalance_count,
        "hhi": hhi,
        "final_value": final_value,
    }


def ensemble_score(base: dict, stress: dict) -> float:
    """Match program.md: return * dd_dampener * div_bonus, with hard gates."""
    annualized = base["annualized_return_pct"] / 100.0
    if annualized < -0.50:
        return float("-inf")
    if stress["calmar_ratio"] < 0.0:
        return float("-inf")

    dd = abs(base["max_drawdown_pct"]) / 100.0
    dampener = 1.0 / (1.0 + max(0.0, dd - 0.15)) ** 2
    div_bonus = 1.0 + 0.1 * (1.0 - base["hhi"])
    return annualized * dampener * div_bonus


def evaluate(params: GemParams, candles_by_token) -> tuple[float, dict, dict]:
    base = gem_backtest(params, candles_by_token)
    stress_params = replace(params, fee_rate=params.fee_rate * 1.5)
    stress = gem_backtest(stress_params, candles_by_token)
    score = ensemble_score(base, stress)
    return score, base, stress


# ---------------------------------------------------------------------------
# Experiment plan -- streaming hypothesis selector that honors the contract
# ---------------------------------------------------------------------------


@dataclass
class Experiment:
    idx: int
    kind: str  # "grid" | "deletion" | "council"
    philosophy: str  # short tag, e.g. "scale_change", "contrarian"
    params: GemParams
    description: str


# Components that the deletion mandate is allowed to toggle off, in the order
# they're cycled through. Mirrors the "currently-active component" wording in
# program.md.
DELETION_TARGETS = [
    ("use_r2_filter", "delete R^2 filter"),
    ("use_growth_filter", "delete a1>1 growth filter"),
    ("use_momentum_cap", "delete momentum cap"),
    ("use_inverse_vol_weighting", "swap inverse-vol -> equal weights"),
]


COUNCIL_PHILOSOPHIES = [
    "scale_change",
    "contrarian",
    "regime_shift",
    "simplification",
]


def make_grid(rng: random.Random) -> list[GemParams]:
    """Phase-2-style grid: top_n x r2_threshold x rebalance_cooldown."""
    top_ns = [1, 3, 5]
    r2s = [0.3, 0.5, 0.7]
    cds = [5, 7, 10]
    base = GemParams()
    grid = []
    for top_n in top_ns:
        for r2 in r2s:
            for cd in cds:
                grid.append(replace(base, top_n=top_n, r2_threshold=r2, rebalance_cooldown=cd))
    rng.shuffle(grid)
    return grid


def deletion_experiment(idx: int, baseline: GemParams, deletion_round: int) -> Experiment:
    flag, label = DELETION_TARGETS[deletion_round % len(DELETION_TARGETS)]
    p = replace(baseline, **{flag: False})
    return Experiment(
        idx=idx,
        kind="deletion",
        philosophy="simplification",
        params=p,
        description=f"deletion: {label}",
    )


def council_experiment(
    idx: int, baseline: GemParams, council_round: int, last_philosophy: str
) -> Experiment:
    """Pick a non-repeating philosophy and apply a structural change."""
    candidates = [p for p in COUNCIL_PHILOSOPHIES if p != last_philosophy]
    philosophy = candidates[council_round % len(candidates)]

    if philosophy == "scale_change":
        p = replace(baseline, top_n=max(1, baseline.top_n * 10))
        desc = f"council/scale_change: 10x top_n -> {p.top_n}"
    elif philosophy == "contrarian":
        p = replace(baseline, pick_lowest_momentum=True)
        desc = "council/contrarian: pick lowest momentum (mean-reversion)"
    elif philosophy == "regime_shift":
        # Stand-in regime gate for a 4-token universe: widen the fit window
        # so the strategy reacts to the slower trend instead of the recent
        # bounce. Matches the "BTC 200d MA risk-off" spirit of the original
        # rule on a portfolio without BTC.
        p = replace(baseline, fit_window=90)
        desc = "council/regime_shift: switch to slow trend (fit_window=90)"
    elif philosophy == "simplification":
        p = replace(
            baseline,
            use_r2_filter=False,
            use_momentum_cap=False,
            use_inverse_vol_weighting=False,
        )
        desc = "council/simplification: strip optional filters"
    else:
        p = baseline
        desc = "council/noop"

    return Experiment(
        idx=idx,
        kind="council",
        philosophy=philosophy,
        params=p,
        description=desc,
    )


def plan_iter(
    grid: list[GemParams], max_experiments: int
) -> Iterable[Experiment]:
    """Yield experiments honoring the deletion mandate and council mode.

    Streaming generator that needs feedback from the runner via send() to
    detect stagnation. We approximate that by yielding placeholder
    experiments and letting the runner override the kind via mutation; the
    cleaner shape is for the runner to drive the state machine directly.
    """
    raise NotImplementedError("driven by run() instead")


def run(
    candles_by_token: dict[str, pd.DataFrame],
    max_experiments: int,
    seed: int,
) -> list[dict]:
    rng = random.Random(seed)
    grid = make_grid(rng)

    rows: list[dict] = []
    best_score = float("-inf")
    best_params: Optional[GemParams] = None
    consecutive_stagnant = 0
    deletion_round = 0
    council_round = 0
    last_philosophy = ""

    grid_idx = 0
    exp_idx = 0
    started = time.time()

    while exp_idx < max_experiments:
        # Selection rules, in priority:
        # 1. Council mode after 5 stagnant non-improvements (overrides #2 + #3
        #    for one experiment).
        # 2. Mandatory deletion every 5th experiment.
        # 3. Otherwise, walk the grid.
        baseline = best_params if best_params is not None else GemParams()

        if consecutive_stagnant >= 5:
            exp = council_experiment(exp_idx, baseline, council_round, last_philosophy)
            council_round += 1
            consecutive_stagnant = 0
        elif exp_idx > 0 and exp_idx % 5 == 0:
            exp = deletion_experiment(exp_idx, baseline, deletion_round)
            deletion_round += 1
        else:
            if grid_idx >= len(grid):
                # Grid exhausted -- fall back to council to keep exploring.
                exp = council_experiment(
                    exp_idx, baseline, council_round, last_philosophy
                )
                council_round += 1
            else:
                p = grid[grid_idx]
                grid_idx += 1
                exp = Experiment(
                    idx=exp_idx,
                    kind="grid",
                    philosophy="grid",
                    params=p,
                    description=(
                        f"grid: top_n={p.top_n} r2={p.r2_threshold} cd={p.rebalance_cooldown}"
                    ),
                )

        score, base, stress = evaluate(exp.params, candles_by_token)

        if math.isfinite(score) and score > best_score:
            best_score = score
            best_params = exp.params
            outcome = "kept"
            consecutive_stagnant = 0
        else:
            outcome = "discarded"
            consecutive_stagnant += 1

        last_philosophy = exp.philosophy

        row = {
            "exp": exp.idx,
            "kind": exp.kind,
            "philosophy": exp.philosophy,
            "score": score,
            "annualized_return_pct": base["annualized_return_pct"],
            "max_drawdown_pct": base["max_drawdown_pct"],
            "sharpe": base["sharpe_ratio"],
            "sortino": base["sortino_ratio"],
            "calmar": base["calmar_ratio"],
            "calmar_stress": stress["calmar_ratio"],
            "hhi": base["hhi"],
            "rebalance_count": base["rebalance_count"],
            "best_so_far": best_score,
            "outcome": outcome,
            "description": exp.description,
            "top_n": exp.params.top_n,
            "r2_threshold": exp.params.r2_threshold,
            "rebalance_cooldown": exp.params.rebalance_cooldown,
            "fit_window": exp.params.fit_window,
            "use_r2_filter": exp.params.use_r2_filter,
            "use_growth_filter": exp.params.use_growth_filter,
            "use_momentum_cap": exp.params.use_momentum_cap,
            "use_inverse_vol_weighting": exp.params.use_inverse_vol_weighting,
            "pick_lowest_momentum": exp.params.pick_lowest_momentum,
        }
        rows.append(row)
        print(_format_row(row))

        exp_idx += 1

    elapsed = time.time() - started
    print(
        f"\nDone. {len(rows)} experiments in {elapsed:.1f}s. "
        f"Best score: {best_score:.4f} ({_describe(best_params)})"
    )
    return rows


def _format_row(row: dict) -> str:
    score_str = "-inf" if not math.isfinite(row["score"]) else f"{row['score']:8.3f}"
    return (
        f"#{row['exp']:>3} {row['kind']:<8} {row['philosophy']:<14} "
        f"score={score_str} "
        f"ann={row['annualized_return_pct']:>7.2f}% "
        f"dd={row['max_drawdown_pct']:>7.2f}% "
        f"calmar_stress={row['calmar_stress']:>6.2f} "
        f"best={row['best_so_far']:8.3f} {row['outcome']:<10} "
        f"-- {row['description']}"
    )


def _describe(params: Optional[GemParams]) -> str:
    if params is None:
        return "no winner"
    return (
        f"top_n={params.top_n} r2={params.r2_threshold} "
        f"cd={params.rebalance_cooldown} fit_window={params.fit_window}"
    )


def write_tsv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_candles(path: str = DATA_PATH) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(path)
    return {
        token: g.sort_values("timestamp").reset_index(drop=True)
        for token, g in df.groupby("token")
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--max-experiments",
        type=int,
        default=30,
        help="Total experiments to run. Default 30 covers the grid plus several deletion + council slots.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for grid order. Same seed = same plan.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Skip writing the TSV (useful for smoke tests).",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(DATA_PATH):
        print(
            f"error: missing {DATA_PATH}. Did you commit data/bear_portfolio_candles.csv?",
            file=sys.stderr,
        )
        return 1

    candles_by_token = load_candles(DATA_PATH)
    rows = run(candles_by_token, args.max_experiments, args.seed)

    if not args.no_write:
        write_tsv(rows, RESULTS_PATH)

    return 0


if __name__ == "__main__":
    sys.exit(main())
