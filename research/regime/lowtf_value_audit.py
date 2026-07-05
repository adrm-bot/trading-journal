# -*- coding: utf-8 -*-
"""하위 TF 밴드의 실효 가치 측정: 5m 레짐이 15m 레짐 대비 뭘 더 주는가.

측정 3종 (라이브 REST ~40일):
  1. 일치율: 15m 종가 시점에 5m 라벨 == 15m 라벨 비율 (같으면 정보 중복)
  2. 플리커: 하루당 라벨 전환수 5m vs 15m (노이즈 비용)
  3. 선행성: 15m 라벨 전환 시, '직전 15m 종가'에 5m가 이미 새 라벨이었던 비율
     (= 하위 밴드가 전환을 미리 보여줬는가 — 밴드의 존재 이유 검정)
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"C:\Users\user\Desktop\Auto-trade-system")
import numpy as np
import pandas as pd
import requests
from app.regime import classify2, _fetch_klines, FAPI

# 5m 클라인 40일 (app _grid에 5m이 없어 직접 페치)
rows, end = [], None
need = 40 * 288
s = requests.Session()
while len(rows) < need:
    p = {"symbol": "BTCUSDT", "interval": "5m", "limit": 1500}
    if end:
        p["endTime"] = end
    ch = s.get(f"{FAPI}/fapi/v1/klines", params=p, timeout=30).json()
    if not ch:
        break
    rows = ch + rows
    end = ch[0][0] - 1
df = pd.DataFrame(rows, columns=["ot","o","h","l","c","v","ct","qv","n","tb","tq","ig"])
df = df.drop_duplicates("ot").sort_values("ot")
for col, name in [("o","open"),("h","high"),("l","low"),("c","close")]:
    df[name] = df[col].astype("float64")
idx = pd.to_datetime(df["ot"].astype("int64"), unit="ms", utc=True) + pd.Timedelta(minutes=5)
b5 = df[["open","high","low","close"]].set_axis(idx, axis=0)
b5 = b5[b5.index <= pd.Timestamp.now(tz="UTC")]

r5, c5, _ = classify2(b5, 5)          # qw = 4000봉 캡(~14일) — Pine 5m 캡과 동일 근사
b15 = _fetch_klines("BTCUSDT", "15m", days=60)
r15, c15, _ = classify2(b15, 15)

ok5, ok15 = r5.notna(), r15.notna()
r5v, r15v = r5[ok5], r15[ok15]
common = r15v.index.intersection(r5v.index)   # 15m 종가 시각(5m 그리드의 부분집합)
a5, a15 = r5v.loc[common], r15v.loc[common]
print(f"표본: 15m 종가 {len(common)}개 ({common.min():%m-%d} ~ {common.max():%m-%d})")
print(f"1) 일치율: {float((a5 == a15).mean())*100:.1f}%  (5m 라벨 == 15m 라벨)")

days5 = (r5v.index[-1] - r5v.index[0]).total_seconds() / 86400
days15 = (r15v.index[-1] - r15v.index[0]).total_seconds() / 86400
tr5 = int((r5v != r5v.shift()).sum()) - 1
tr15 = int((r15v != r15v.shift()).sum()) - 1
print(f"2) 전환/일: 5m {tr5/days5:.1f} vs 15m {tr15/days15:.1f}  (플리커 {tr5/days5/(tr15/days15):.1f}배)")

# 3) 선행성: 15m 전환봉에서, 직전 15m 종가의 5m 라벨이 이미 '새 라벨'이었나
sw = (a15 != a15.shift()) & a15.shift().notna()
sw_idx = a15.index[sw]
lead = 0; tot = 0
for t in sw_idx:
    pos = common.get_loc(t)
    if pos == 0:
        continue
    prev_t = common[pos - 1]
    tot += 1
    if a5.loc[prev_t] == a15.loc[t]:
        lead += 1
print(f"3) 선행성: 15m 전환 {tot}회 중 5m이 한 봉 먼저 새 라벨이었던 경우 {lead}회 ({lead/max(1,tot)*100:.0f}%)")
# 기저율: 아무 시점에나 5m 라벨이 '다음 15m 라벨'과 우연히 같을 확률 근사 = 일치율
print(f"   (기저: 임의 시점 5m==15m 일치율 {float((a5 == a15).mean())*100:.0f}% — 이보다 높아야 진짜 선행)")
