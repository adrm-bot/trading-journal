import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features import FeatureParams  # noqa: E402

# small windows so synthetic series don't need 35 days of warmup
FP_SMALL = FeatureParams(q_window=192, q_min_periods_fuel=96, burn_in=400)


def make_df(closes: np.ndarray, oi: np.ndarray | None = None,
            spread: float = 0.001, start: str = "2024-01-01") -> pd.DataFrame:
    """Synthetic build_dataset-shaped frame from a close series."""
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC", unit="ns")
    c = pd.Series(closes, index=idx, dtype="float64")
    df = pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c * (1 + spread),
        "low": c * (1 - spread),
        "close": c,
        "volume": 100.0,
        "bar_missing": False,
    }, index=idx)
    df.index.name = "ts"
    if oi is None:
        oi = np.full(n, 1_000_000.0)
    df["oi"] = np.asarray(oi, dtype="float64")
    df["oi_lag1"] = df["oi"].shift(1)
    df["oi_fresh"] = True
    df["funding"] = 0.0001
    return df


def rng(seed: int = 7) -> np.random.Generator:
    return np.random.default_rng(seed)


@pytest.fixture(scope="session")
def btc_df():
    p = Path(__file__).resolve().parents[1] / "data" / "BTCUSDT_15m.parquet"
    if not p.exists():
        pytest.skip("BTCUSDT parquet not built")
    return pd.read_parquet(p)
