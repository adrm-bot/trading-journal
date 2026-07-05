"""Deterministic synthetic market scenarios for the axis/state tests.

Each returns a build_dataset-shaped frame: ~800 context bars (so past-only percentile windows
have a distribution to rank against) followed by ~400 scenario bars; assertions read the tail.
"""
import numpy as np

from conftest import make_df, rng

N_CTX, N_SCN = 800, 400

# Assertion window: 60..140 bars AFTER scenario onset. Indicators (incl. BBW's 20-bar window
# holding context vol) have re-converged by +60, but the past-only percentile window is not
# yet saturated by the scenario itself — a regime that persists longer than the rolling window
# gradually re-normalizes (inherent property of adaptive thresholds, accepted at design time;
# real 15m regimes last << the 30d window).
WIN = slice(N_CTX + 60, N_CTX + 128)


def _walk(g, n, vol, drift=0.0, start=100.0):
    steps = 1.0 + drift + g.normal(0.0, vol, n)
    return start * np.cumprod(steps)


def trend_up(oi_mode: str = "rising"):
    g = rng(1)
    ctx = _walk(g, N_CTX, 0.004)
    scn = _walk(g, N_SCN, 0.001, drift=0.003, start=ctx[-1])
    closes = np.concatenate([ctx, scn])
    oi = _oi(g, oi_mode, len(closes))
    return make_df(closes, oi)


def trend_down(oi_mode: str = "rising"):
    g = rng(2)
    ctx = _walk(g, N_CTX, 0.004)
    scn = _walk(g, N_SCN, 0.001, drift=-0.003, start=ctx[-1])
    closes = np.concatenate([ctx, scn])
    oi = _oi(g, oi_mode, len(closes))
    return make_df(closes, oi)


def squeeze():
    """Contracting low-vol convergence: vol keeps shrinking, so the current bar always ranks
    at the BOTTOM of the past window (a static low-vol block would self-normalize instead)."""
    g = rng(3)
    ctx = _walk(g, N_CTX, 0.006)
    vols = 0.0006 * 0.99 ** np.arange(N_SCN)  # decay must dominate realized-vol noise
    steps = 1.0 + g.normal(0.0, 1.0, N_SCN) * vols
    scn = ctx[-1] * np.cumprod(steps)
    return make_df(np.concatenate([ctx, scn]))


def chop():
    """Expanding violent alternation, no net drift: current bar always ranks at the TOP."""
    g = rng(4)
    ctx = _walk(g, N_CTX, 0.002)
    signs = np.tile([1.0, -1.0], N_SCN // 2)
    amp = 0.02 * 1.004 ** np.arange(N_SCN)
    scn = ctx[-1] * np.cumprod(1.0 + signs * (amp + g.normal(0, 0.002, N_SCN)))
    return make_df(np.concatenate([ctx, scn]))


def range_mid():
    g = rng(5)
    closes = _walk(g, N_CTX + N_SCN, 0.004)  # stationary vol -> vol_pct ~ 0.5
    return make_df(closes)


def _oi(g, mode: str, n: int):
    base = 1_000_000.0
    noise = g.normal(0, 0.0002, n)
    if mode == "rising":
        rate = np.full(n, 0.002)
    elif mode == "falling":
        rate = np.full(n, -0.002)
    elif mode == "flat":
        rate = np.zeros(n)
        noise[N_CTX:] = 0.0  # exactly-zero dOI: unambiguous deadzone
    else:
        raise ValueError(mode)
    rate[:N_CTX] = 0.0  # OI moves only in the scenario segment
    return base * np.cumprod(1.0 + rate + noise)
