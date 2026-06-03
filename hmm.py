"""Self-contained Gaussian / Student's-t HMM in log-space.

Extracted verbatim (behaviour-preserving) from
``notebooks/regime_student_t_drawdown_2022.org`` so research and the
dashboard build (`scripts/build_feed.py`) share one source of truth.

Contents:
  - forward_backward            log-space E-step (gamma, xi, log-likelihood)
  - viterbi                     MAP state path
  - gaussian_log_emissions      log p(x | mu, sigma)
  - student_t_log_emissions     log p(x | mu, sigma, nu)
  - kmeans_init                 shared parameter initialisation
  - m_step_init_trans           initial-state + transition M-step
  - m_step_gaussian             Gaussian emission M-step
  - m_step_student_t            Student's-t emission M-step (ECME)
  - baum_welch                  EM driver
  - fit_with_restarts           multi-restart wrapper, returns best HMMFit
  - HMMFit                      result dataclass

No external HMM dependency; only numpy / scipy / sklearn.
"""

from dataclasses import dataclass

import numpy as np
from scipy.special import logsumexp, digamma
from scipy.stats import t as student_t
from scipy.optimize import brentq
from sklearn.cluster import KMeans

# Default master seed for reproducible restarts; callers may override.
RNG_SEED = 20260603


# --------------------------------------------------------------------------- #
# Forward-backward (E-step) in log-space
# --------------------------------------------------------------------------- #
def forward_backward(log_pi, log_A, log_B):
    T, K = log_B.shape
    log_alpha = np.full((T, K), -np.inf)
    log_beta = np.full((T, K), -np.inf)

    log_alpha[0] = log_pi + log_B[0]
    for t in range(1, T):
        log_alpha[t] = logsumexp(log_alpha[t - 1][:, None] + log_A, axis=0) + log_B[t]

    log_beta[T - 1] = 0.0
    for t in range(T - 2, -1, -1):
        log_beta[t] = logsumexp(log_A + log_B[t + 1][None, :] + log_beta[t + 1][None, :], axis=1)

    log_likelihood = logsumexp(log_alpha[T - 1])
    log_gamma = log_alpha + log_beta - log_likelihood
    log_xi = (
        log_alpha[:-1, :, None]
        + log_A[None, :, :]
        + log_B[1:, None, :]
        + log_beta[1:, None, :]
        - log_likelihood
    )
    return log_gamma, log_xi, log_likelihood


# --------------------------------------------------------------------------- #
# Viterbi decoder (MAP path)
# --------------------------------------------------------------------------- #
def viterbi(log_pi, log_A, log_B):
    T, K = log_B.shape
    delta = np.full((T, K), -np.inf)
    psi = np.zeros((T, K), dtype=np.int64)
    delta[0] = log_pi + log_B[0]
    for t in range(1, T):
        scores = delta[t - 1][:, None] + log_A
        psi[t] = scores.argmax(axis=0)
        delta[t] = scores.max(axis=0) + log_B[t]
    path = np.empty(T, dtype=np.int64)
    path[-1] = int(delta[-1].argmax())
    for t in range(T - 2, -1, -1):
        path[t] = psi[t + 1, path[t + 1]]
    return path


# --------------------------------------------------------------------------- #
# Emission log-pdfs
# --------------------------------------------------------------------------- #
def gaussian_log_emissions(x, mu, sigma):
    z = (x[:, None] - mu[None, :]) / sigma[None, :]
    return -0.5 * np.log(2 * np.pi) - np.log(sigma)[None, :] - 0.5 * z * z


def student_t_log_emissions(x, mu, sigma, nu):
    K = len(mu)
    out = np.empty((len(x), K))
    for i in range(K):
        out[:, i] = student_t.logpdf(x, df=nu[i], loc=mu[i], scale=sigma[i])
    return out


# --------------------------------------------------------------------------- #
# Initialisation + transition / initial-state M-step
# --------------------------------------------------------------------------- #
def kmeans_init(x, K, rng, jitter=0.0):
    km = KMeans(n_clusters=K, n_init=10, random_state=int(rng.integers(1 << 31)))
    labels = km.fit_predict(x.reshape(-1, 1))
    mu = np.array([x[labels == i].mean() for i in range(K)])
    sigma = np.array([x[labels == i].std(ddof=0) + 1e-6 for i in range(K)])
    pi = np.array([(labels == i).mean() for i in range(K)])
    if jitter > 0:
        mu = mu + rng.normal(0, jitter * x.std(), size=K)
    A = np.full((K, K), 0.05 / (K - 1)) if K > 1 else np.ones((1, 1))
    np.fill_diagonal(A, 0.95)
    return mu, sigma, pi, A


def m_step_init_trans(log_gamma, log_xi):
    log_pi = log_gamma[0] - logsumexp(log_gamma[0])
    log_A = logsumexp(log_xi, axis=0) - logsumexp(log_xi, axis=(0, 2))[:, None]
    return log_pi, log_A


# --------------------------------------------------------------------------- #
# Gaussian M-step
# --------------------------------------------------------------------------- #
def m_step_gaussian(x, log_gamma):
    gamma = np.exp(log_gamma)
    Nk = gamma.sum(axis=0)
    mu = (gamma * x[:, None]).sum(axis=0) / Nk
    diff_sq = (x[:, None] - mu[None, :]) ** 2
    sigma = np.sqrt((gamma * diff_sq).sum(axis=0) / Nk) + 1e-6
    return mu, sigma


# --------------------------------------------------------------------------- #
# Student's-t M-step (ECME)
# --------------------------------------------------------------------------- #
def m_step_student_t(x, log_gamma, mu_prev, sigma_prev, nu_prev,
                     nu_lo=3.0, nu_hi=30.0):
    gamma = np.exp(log_gamma)
    K = gamma.shape[1]

    z2 = ((x[:, None] - mu_prev[None, :]) / sigma_prev[None, :]) ** 2
    w = (nu_prev[None, :] + 1.0) / (nu_prev[None, :] + z2)
    gw = gamma * w

    Nk = gamma.sum(axis=0)
    Sw = gw.sum(axis=0)

    mu = (gw * x[:, None]).sum(axis=0) / Sw
    diff_sq = (x[:, None] - mu[None, :]) ** 2
    sigma = np.sqrt((gw * diff_sq).sum(axis=0) / Nk) + 1e-6

    z2_new = ((x[:, None] - mu[None, :]) / sigma[None, :]) ** 2
    w_new = (nu_prev[None, :] + 1.0) / (nu_prev[None, :] + z2_new)
    const = ((gamma * (np.log(w_new) - w_new)).sum(axis=0) / Nk)
    const = const + digamma((nu_prev + 1) / 2.0) - np.log((nu_prev + 1) / 2.0)

    nu = np.empty(K)
    for i in range(K):
        c = const[i]
        def f(v, c=c):
            return np.log(v / 2.0) - digamma(v / 2.0) + 1.0 + c
        try:
            lo_val, hi_val = f(nu_lo), f(nu_hi)
            if lo_val * hi_val > 0:
                nu[i] = nu_lo if abs(lo_val) < abs(hi_val) else nu_hi
            else:
                nu[i] = brentq(f, nu_lo, nu_hi, xtol=1e-3)
        except Exception:
            nu[i] = nu_prev[i]
    return mu, sigma, nu


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class HMMFit:
    log_pi: np.ndarray
    log_A: np.ndarray
    mu: np.ndarray
    sigma: np.ndarray
    nu: np.ndarray
    log_gamma: np.ndarray
    log_likelihood: float
    n_iter: int
    converged: bool
    family: str
    K: int


# --------------------------------------------------------------------------- #
# EM driver + multi-restart wrapper
# --------------------------------------------------------------------------- #
def baum_welch(x, K, family, rng, max_iter=1000, tol=1e-5, nu0=8.0, jitter=0.0):
    mu, sigma, pi, A = kmeans_init(x, K, rng, jitter=jitter)
    log_pi = np.log(pi + 1e-12)
    log_A = np.log(A + 1e-12)
    nu = np.full(K, nu0) if family == "student_t" else np.full(K, np.inf)

    prev_ll = -np.inf
    converged = False
    it = 0
    for it in range(max_iter):
        if family == "gaussian":
            log_B = gaussian_log_emissions(x, mu, sigma)
        else:
            log_B = student_t_log_emissions(x, mu, sigma, nu)
        log_gamma, log_xi, ll = forward_backward(log_pi, log_A, log_B)
        log_pi, log_A = m_step_init_trans(log_gamma, log_xi)
        if family == "gaussian":
            mu, sigma = m_step_gaussian(x, log_gamma)
        else:
            mu, sigma, nu = m_step_student_t(x, log_gamma, mu, sigma, nu)
        if abs(ll - prev_ll) < tol:
            converged = True
            it += 1
            break
        prev_ll = ll
    return HMMFit(log_pi, log_A, mu, sigma, nu, log_gamma, ll, it, converged, family, K)


def fit_with_restarts(x, K, family, n_restarts=5, master_seed=RNG_SEED, **kw):
    best = None
    master = np.random.default_rng(master_seed)
    for r in range(n_restarts):
        rng = np.random.default_rng(master.integers(1 << 31))
        jitter = 0.0 if r == 0 else 0.15
        try:
            fit = baum_welch(x, K, family, rng, jitter=jitter, **kw)
        except Exception as e:
            print(f"  restart {r} ({family}, K={K}) failed: {e}")
            continue
        if best is None or fit.log_likelihood > best.log_likelihood:
            best = fit
    return best
