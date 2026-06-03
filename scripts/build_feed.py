"""Build the dashboard feed.

Fits the canonical drawdown-HMM (Gaussian, K=3) on the full BTCUSDT daily
series, names the states by ascending mu (bear / ranging / bull), and emits:

  dashboard/feed.json   public-API artifact (the JSON schema in
                        docs/plans/2026-06-03-drawdown-hmm-dashboard-design.md)
  dashboard/index.html  index.template.html with {{FEED_JSON}} substituted by
                        the feed contents (inline-at-build render mode)

Run:  python scripts/build_feed.py
or:   make feed

The page layer is model-agnostic: it introspects feed.model.states[]. To ship
a different model (different K / family / observation), change the fit below and
re-run; the dashboard renders any conforming feed without code changes.
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

# Make `import hmm` work regardless of CWD.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hmm import fit_with_restarts, gaussian_log_emissions, viterbi  # noqa: E402

DATA_PATH = os.path.join(REPO, "data", "btcusdt_daily.csv")
DASHBOARD_DIR = os.path.join(REPO, "dashboard")
FEED_PATH = os.path.join(DASHBOARD_DIR, "feed.json")
TEMPLATE_PATH = os.path.join(DASHBOARD_DIR, "index.template.html")
INDEX_PATH = os.path.join(DASHBOARD_DIR, "index.html")

# --------------------------------------------------------------------------- #
# Model definition (the one knob that changes when we ship a new model).
# --------------------------------------------------------------------------- #
ASSET = "BTCUSDT"
FREQUENCY = "1d"
FAMILY = "gaussian"
K = 3
N_RESTARTS = 5

MODEL_NAME = "drawdown-gaussian-k3"
MODEL_LABEL = "Drawdown HMM — Gaussian, K=3"
OBSERVATION_CODE = "drawdown_from_rolling_max"
OBSERVATION_LABEL = "d_t = log(p_t) − max_{s≤t} log(p_s)"

# Canonical regime names by ascending mu, plus their colors.
REGIME_NAMES = ("bear", "ranging", "bull")
REGIME_COLORS = {"bear": "#d62728", "ranging": "#7f7f7f", "bull": "#2ca02c"}

# Zoom windows surfaced in the dashboard (metadata only; the page slices the
# series itself). Ordered as they appear in the 2x2 grid.
WINDOWS = [
    {"name": "2018_bear",   "label": "2018 bear",      "start": "2017-12-17", "end": "2018-12-15"},
    {"name": "covid_2020",  "label": "COVID Q1 2020",  "start": "2020-01-01", "end": "2020-04-30"},
    {"name": "bull_2020_21","label": "2020–21 bull",   "start": "2020-10-01", "end": "2021-11-10"},
    {"name": "2022_bear",   "label": "2022 bear",      "start": "2021-11-10", "end": "2022-12-31"},
]


def load_series(data_path: str) -> pd.DataFrame:
    """Load candles and compute the causal drawdown-from-rolling-max feature."""
    df = pd.read_csv(data_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["log_price"] = np.log(df["close"])
    # Causal: running max uses only history up to t.
    df["drawdown"] = df["log_price"] - df["log_price"].cummax()
    df["log_return"] = df["log_price"].diff()
    # Drop the first row (no return); drawdown there is 0 by construction.
    df = df.dropna(subset=["log_return"]).reset_index(drop=True)
    return df


def fit_canonical(obs: np.ndarray):
    """Fit the model and return (fit, order) with states sorted by ascending mu."""
    fit = fit_with_restarts(obs, K, FAMILY, n_restarts=N_RESTARTS)
    if fit is None:
        raise RuntimeError("all restarts failed")
    order = np.argsort(fit.mu)
    return fit, order


def canonical_posterior(fit, order) -> np.ndarray:
    """gamma reordered to canonical (bear, ranging, bull) column order."""
    gamma = np.exp(fit.log_gamma)
    return gamma[:, order]


def canonical_viterbi(fit, obs, order) -> np.ndarray:
    """Viterbi path remapped to canonical state indices (0=bear..)."""
    log_B = gaussian_log_emissions(obs, fit.mu, fit.sigma)
    raw = viterbi(fit.log_pi, fit.log_A, log_B)
    remap = np.empty(K, dtype=np.int64)
    for canonical_idx, raw_idx in enumerate(order):
        remap[raw_idx] = canonical_idx
    return remap[raw]


def window_metrics(df: pd.DataFrame, vit_canonical: np.ndarray, win: dict) -> dict:
    """viterbi_flips + regime_shares for one zoom window."""
    mask = (df["date"] >= win["start"]) & (df["date"] <= win["end"])
    idx = np.where(mask.to_numpy())[0]
    if len(idx) == 0:
        shares = {name: 0.0 for name in REGIME_NAMES}
        return {"viterbi_flips": 0, "n_days": 0, "regime_shares": shares}
    seg = vit_canonical[idx]
    flips = int((np.diff(seg) != 0).sum())
    shares = {name: round(float((seg == i).mean()), 4)
              for i, name in enumerate(REGIME_NAMES)}
    return {"viterbi_flips": flips, "n_days": int(len(idx)), "regime_shares": shares}


def build_feed(df: pd.DataFrame, fit, order, generated_at: str) -> dict:
    obs = df["drawdown"].to_numpy()
    post = canonical_posterior(fit, order)                 # (T, K) canonical
    vit = canonical_viterbi(fit, obs, order)               # (T,) canonical idx

    mu_sorted = fit.mu[order]
    sigma_sorted = fit.sigma[order]
    nu_sorted = fit.nu[order]
    has_nu = np.all(np.isfinite(nu_sorted))

    states = []
    for i, name in enumerate(REGIME_NAMES):
        params = {"mu": round(float(mu_sorted[i]), 4),
                  "sigma": round(float(sigma_sorted[i]), 4)}
        if has_nu:
            params["nu"] = round(float(nu_sorted[i]), 2)
        states.append({"name": name, "color": REGIME_COLORS[name], "params": params})

    # Transition matrix reordered to canonical row/col order.
    A = np.exp(fit.log_A)
    A = A[np.ix_(order, order)]
    trans = [[round(float(v), 4) for v in row] for row in A]

    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    close = [round(float(c), 2) for c in df["close"].to_numpy()]
    observation = [round(float(d), 5) for d in obs]
    posterior = {name: [round(float(p), 4) for p in post[:, i]]
                 for i, name in enumerate(REGIME_NAMES)}
    viterbi_labels = [REGIME_NAMES[i] for i in vit]

    windows = []
    for win in WINDOWS:
        windows.append({**win, "metrics": window_metrics(df, vit, win)})

    feed = {
        "generated_at": generated_at,
        "asset": ASSET,
        "frequency": FREQUENCY,
        "model": {
            "name": MODEL_NAME,
            "label": MODEL_LABEL,
            "family": FAMILY,
            "K": K,
            "observation": {"code": OBSERVATION_CODE, "label": OBSERVATION_LABEL},
            "fit_window": {"start": dates[0], "end": dates[-1]},
            "log_likelihood": round(float(fit.log_likelihood), 2),
            "states": states,
            "transition_matrix": trans,
        },
        "series": {
            "dates": dates,
            "close": close,
            "observation": observation,
            "posterior": posterior,
            "viterbi": viterbi_labels,
        },
        "windows": windows,
    }
    return feed


def inline_template(feed_json: str) -> bool:
    """Substitute {{FEED_JSON}} in the template; write index.html. Returns True
    if the template exists and was processed."""
    if not os.path.exists(TEMPLATE_PATH):
        print(f"  (template {TEMPLATE_PATH} not found — skipping index.html inline)")
        return False
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()
    if "{{FEED_JSON}}" not in template:
        raise RuntimeError("template missing {{FEED_JSON}} placeholder")
    html = template.replace("{{FEED_JSON}}", feed_json)
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  wrote {INDEX_PATH} ({len(html)/1024:.0f} KB)")
    return True


def parse_args(argv):
    p = argparse.ArgumentParser(description="Build dashboard feed.json + index.html")
    p.add_argument("--data", default=DATA_PATH, help="input candles CSV")
    p.add_argument("--generated-at", default=None,
                   help="ISO timestamp for the feed (default: today UTC midnight)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    generated_at = args.generated_at or (
        pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT00:00:00Z"))

    print(f"Loading {args.data}")
    df = load_series(args.data)
    print(f"  {len(df)} candles, {df['date'].min().date()} → {df['date'].max().date()}")

    print(f"Fitting {FAMILY} K={K} ({N_RESTARTS} restarts)...")
    obs = df["drawdown"].to_numpy()
    fit, order = fit_canonical(obs)
    print(f"  LL = {fit.log_likelihood:.1f}, iters = {fit.n_iter}, "
          f"converged = {fit.converged}")
    for name, i in zip(REGIME_NAMES, order):
        print(f"  {name:8s} μ={fit.mu[i]:+.3f}  σ={fit.sigma[i]:.3f}")

    feed = build_feed(df, fit, order, generated_at)

    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    # Compact separators keep the inlined payload small (~daily resolution).
    feed_json = json.dumps(feed, separators=(",", ":"), ensure_ascii=False)
    with open(FEED_PATH, "w", encoding="utf-8") as fh:
        fh.write(feed_json)
    print(f"  wrote {FEED_PATH} ({len(feed_json)/1024:.0f} KB)")

    inline_template(feed_json)
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
