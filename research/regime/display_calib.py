#!/usr/bin/env python3
"""표시층(v1.6) 캘리브레이션 — 그라인드 감사·표류 태그·근접 2위 임계의 재현 스크립트.

NOTES.md R7과 Pine v1.6 / current.py / app/regime.py 주석·툴팁이 인용하는 수치의 정본.
실행: python display_calib.py  → results/display_calib.json 갱신 + 콘솔 요약.

측정 목록 (BTC 15m 파케이, 동결 분류기 v1 라벨 기준):
  A. 그라인드(|24h이동|>=3% & ADX<20) 라벨 분포 + 전방 방향지속/정렬수익 (vs 고ADX·횡보 대조군)
  B. ER(효율비 24h) 판별력 + 임계 스캔 (튜닝 era 2022-23만 — 분할표본 규율)
  C. 표류 게이트(on |24h|>=2% AND ER>=0.10 / off <1.5% or <0.08) 발화율·전환수
     + **표류-on 코호트 자체의 전방 방향지속** (그라인드 측정치 차용 금지 — 자기 코호트 실측)
  D. 근접 2위(runnerUp/agreement >= 0.85) 발화율 + 표기 후 8봉 내 라벨 전환율 vs 기저율
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from features import compute, DEFAULT_FP                       # noqa: E402
from classifier import classify, DEFAULT_CP, REGIMES           # noqa: E402

HERE = Path(__file__).parent
H = 96  # 24h @ 15m


def er_series(c: pd.Series, n: int) -> pd.Series:
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum().replace(0, np.nan)


def drift_fsm(net: pd.Series, er: pd.Series) -> np.ndarray:
    on, out = False, np.zeros(len(net), dtype=bool)
    nv, ev = net.to_numpy(float), er.to_numpy(float)
    for i in range(len(nv)):
        if np.isnan(nv[i]) or np.isnan(ev[i]):
            on = False
        elif not on and abs(nv[i]) >= 0.02 and ev[i] >= 0.10:
            on = True
        elif on and (abs(nv[i]) < 0.015 or ev[i] < 0.08):
            on = False
        out[i] = on
    return out


def fwd_stats(c: pd.Series, past_dir: pd.Series, mask: pd.Series, h: int) -> dict:
    fwd = c.shift(-h) / c - 1
    m = mask & fwd.notna() & past_dir.notna()
    cont = float((np.sign(fwd[m]) == np.sign(past_dir[m])).mean())
    med = float(np.median(np.sign(past_dir[m]) * fwd[m]) * 100)
    return {"n": int(m.sum()), "continuation": round(cont, 3), "aligned_fwd_med_pct": round(med, 3)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    df = pd.read_parquet(HERE / "data" / "BTCUSDT_15m.parquet")
    feat = compute(df, DEFAULT_FP)
    res = classify(feat, DEFAULT_CP, debug=True)
    b = DEFAULT_FP.burn_in
    feat, res = feat.iloc[b:], res.iloc[b:]
    c, adx, lab = feat["close"], feat["adx"], res["regime"]
    mv = c / c.shift(H) - 1
    er = er_series(c, H)
    ok = lab.notna() & mv.notna() & adx.notna() & er.notna()
    era_tune = feat.index < "2024-01-01"

    out = {"symbol": "BTCUSDT", "tf": "15m",
           "data_range": [str(feat.index.min()), str(feat.index.max())],
           "note": "표시층 캘리브 정본 — NOTES.md R7 인용 수치의 재현 소스"}

    # A. 그라인드 감사
    grind = ok & (mv.abs() >= 0.03) & (adx < 20)
    strong = ok & (mv.abs() >= 0.03) & (adx >= 20)
    flat = ok & (mv.abs() < 0.01)
    out["grind"] = {
        "definition": "|24h move|>=3% AND ADX<20",
        "n": int(grind.sum()), "share_of_scored": round(float(grind.sum() / ok.sum()), 4),
        "label_dist": {k: round(v, 3) for k, v in
                       lab[grind].value_counts(normalize=True).items()},
        "forward": {f"h{h}": fwd_stats(c, mv, grind, h) for h in (16, 96)},
        "control_strong_adx_forward": {f"h{h}": fwd_stats(c, mv, strong, h) for h in (16, 96)},
        "control_flat_forward": {f"h{h}": fwd_stats(c, mv, flat, h) for h in (16, 96)},
    }

    # B. ER 판별력 (임계 스캔은 튜닝 era만 — 분할표본)
    out["er"] = {
        "grind_q25_50_75": [round(float(x), 3) for x in er[grind].quantile([.25, .5, .75])],
        "flat_q25_50_75": [round(float(x), 3) for x in er[flat].quantile([.25, .5, .75])],
        "scan_tune_era": {str(t): {"grind_capture": round(float((er[grind & era_tune] >= t).mean()), 3),
                                   "flat_fp": round(float((er[flat & era_tune] >= t).mean()), 3),
                                   "chop_fire": round(float((er[(lab == "CHOP") & ok & era_tune] >= t).mean()), 3)}
                          for t in (0.08, 0.10, 0.12, 0.14)},
    }

    # C. 표류 게이트 — 발화율/전환수 + 자기 코호트 전방 지속
    nt = lab.isin(["RANGE", "SQUEEZE"]) & mv.notna() & er.notna()
    don = pd.Series(drift_fsm(mv, er), index=lab.index)
    fire = don & nt
    g = fire[nt].to_numpy()
    out["drift_gate"] = {
        "definition": "on |24h|>=2% AND ER>=0.10 / off <1.5% or <0.08 (hysteresis)",
        "fire_rate_nontrend": round(float(g.mean()), 3),
        "transitions": int(np.abs(np.diff(g.astype(int))).sum()),
        "drift_on_forward": {f"h{h}": fwd_stats(c, mv, fire, h) for h in (16, 96)},
    }

    # D. 근접 2위 캘리브
    sc = res[[f"score_{r}" for r in REGIMES]].to_numpy()
    li = np.array([REGIMES.index(x) if x in REGIMES else -1 for x in lab])
    valid = li >= 0
    agree = np.where(valid, sc[np.arange(len(li)), np.clip(li, 0, 4)], np.nan)
    sc2 = sc.copy()
    sc2[np.arange(len(li)), np.clip(li, 0, 4)] = -1
    ratio = sc2.max(axis=1) / agree
    labv = lab.to_numpy()
    sw8 = np.zeros(len(labv), dtype=bool)
    for k in range(1, 9):
        sw8[:-k] |= (labv[k:] != labv[:-k])
    sw8[-8:] = False
    base = float(sw8[valid].mean())
    out["near_second"] = {"definition": "runnerUp/agreement >= 0.85 (확정봉 스냅샷)",
                          "base_switch_within8": round(base, 3), "thresholds": {}}
    for t in (0.80, 0.85, 0.90):
        m = valid & (ratio >= t)
        out["near_second"]["thresholds"][str(t)] = {
            "fire_rate": round(float(m.mean()), 3),
            "switch_within8": round(float(sw8[m].mean()), 3)}

    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "display_calib.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
