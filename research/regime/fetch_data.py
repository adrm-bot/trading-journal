#!/usr/bin/env python3
"""Binance Vision archive fetcher (USDT-M futures) for the regime classifier.

Per symbol, downloads into data/raw/<SYMBOL>/:
  klines/   monthly 15m kline zips + daily zips for the current partial month
  metrics/  daily metrics zips (5-minute open-interest snapshots)
  funding/  monthly fundingRate zips

- Already-cached files are skipped (resumable).
- Each zip is verified against its published .CHECKSUM (sha256); mismatch -> one re-download.
- 404s are recorded in data/raw/<SYMBOL>/missing.json (the archive has known gap days) and are
  NOT errors; anything else raises after retries.

Usage: python fetch_data.py BTCUSDT [ETHUSDT ...] [--start 2022-01-01]
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "https://data.binance.vision/data/futures/um"
ROOT = Path(__file__).resolve().parent / "data" / "raw"
WORKERS = 12
RETRIES = 4

session = requests.Session()
session.headers["User-Agent"] = "regime-research/1.0"


def month_range(start: dt.date, end: dt.date):
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = dt.date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)


def day_range(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def fetch(url: str, dest: Path, missing: set) -> str:
    if dest.exists():
        return "cached"
    last_err = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                missing.add(url.rsplit("/", 1)[-1])
                return "404"
            r.raise_for_status()
            blob = r.content
            cr = session.get(url + ".CHECKSUM", timeout=30)
            if cr.status_code == 200:
                want = cr.text.split()[0].strip().lower()
                got = hashlib.sha256(blob).hexdigest()
                if want != got:
                    last_err = f"checksum mismatch {dest.name}"
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(blob)
            tmp.replace(dest)
            return "ok"
        except Exception as e:  # noqa: BLE001 - retry then re-raise below
            last_err = str(e)
    raise RuntimeError(f"download failed after {RETRIES} tries: {url} ({last_err})")


def plan_symbol(symbol: str, start: dt.date, end: dt.date):
    """Yield (url, dest) pairs for one symbol."""
    sd = ROOT / symbol
    last_full_month_end = dt.date(end.year, end.month, 1) - dt.timedelta(days=1)
    for m in month_range(start, last_full_month_end):
        tag = f"{m:%Y-%m}"
        yield (f"{BASE}/monthly/klines/{symbol}/15m/{symbol}-15m-{tag}.zip",
               sd / "klines" / f"{symbol}-15m-{tag}.zip")
        yield (f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{tag}.zip",
               sd / "funding" / f"{symbol}-fundingRate-{tag}.zip")
    # daily klines for the current partial month
    for d in day_range(dt.date(end.year, end.month, 1), end):
        tag = f"{d:%Y-%m-%d}"
        yield (f"{BASE}/daily/klines/{symbol}/15m/{symbol}-15m-{tag}.zip",
               sd / "klines" / f"{symbol}-15m-{tag}.zip")
    # daily metrics for the whole span
    for d in day_range(start, end):
        tag = f"{d:%Y-%m-%d}"
        yield (f"{BASE}/daily/metrics/{symbol}/{symbol}-metrics-{tag}.zip",
               sd / "metrics" / f"{symbol}-metrics-{tag}.zip")


def fetch_symbol(symbol: str, start: dt.date, end: dt.date):
    jobs = list(plan_symbol(symbol, start, end))
    missing: set = set()
    counts = {"ok": 0, "cached": 0, "404": 0}
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(fetch, url, dest, missing): url for url, dest in jobs}
        done = 0
        for fut in as_completed(futs):
            counts[fut.result()] += 1
            done += 1
            if done % 200 == 0:
                print(f"  {symbol}: {done}/{len(jobs)}", flush=True)
    mfile = ROOT / symbol / "missing.json"
    mfile.parent.mkdir(parents=True, exist_ok=True)
    mfile.write_text(json.dumps(sorted(missing), indent=1))
    print(f"{symbol}: {counts['ok']} downloaded, {counts['cached']} cached, "
          f"{counts['404']} archive-missing (see missing.json)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--start", default="2022-01-01")
    args = ap.parse_args()
    start = dt.date.fromisoformat(args.start)
    # Vision publishes with ~1 day lag; stay 2 days behind today (UTC)
    end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)
    for sym in args.symbols:
        print(f"=== {sym} ({start} .. {end}) ===", flush=True)
        fetch_symbol(sym, start, end)


if __name__ == "__main__":
    sys.exit(main())
