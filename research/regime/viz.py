#!/usr/bin/env python3
"""Three purpose-separated visualizations (self-contained Plotly HTML in results/):

1. <SYM>_ribbon.html      - price + regime ribbon (color=state, opacity=confidence);
                            a fading band IS a transition warning
2. <SYM>_scorecard.html   - THE SCORECARD: B1 (open triangles) vs F (filled) enter-episode
                            tracks, connectors = matched pairs (horizontal span = lead),
                            confidence line with collapse verticals below
3. <SYM>_transitions.html - 5-node state diagram, arrow width = transition frequency
                            (CHOP<->RANGE hammering = churn -> raise dwell/confirm)

Usage: python viz.py BTCUSDT
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import classifier
import leadlag
import run_all

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

COLORS = {"TREND_UP": "#2ca02c", "TREND_DOWN": "#d62728", "SQUEEZE": "#9467bd",
          "RANGE": "#7f7f7f", "CHOP": "#ff7f0e"}


def ribbon(symbol, df, f):
    idx = f.index
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
                        vertical_spacing=0.02)
    fig.add_trace(go.Scattergl(x=idx, y=df["close"].reindex(idx), name="close",
                               line=dict(color="#222", width=1)), row=1, col=1)
    conf = f["confidence"].to_numpy(float)
    for state, color in COLORS.items():
        mask = (f["regime"] == state).to_numpy(bool)
        if not mask.any():
            continue
        fig.add_trace(go.Scattergl(
            x=idx[mask], y=np.zeros(mask.sum()), mode="markers", name=state,
            marker=dict(color=color, symbol="square", size=6,
                        opacity=np.clip(conf[mask] / 0.6, 0.15, 1.0)),
            hovertemplate=f"{state}<br>%{{x}}<br>conf=%{{customdata:.3f}}<extra></extra>",
            customdata=conf[mask]), row=2, col=1)
    fig.update_yaxes(visible=False, row=2, col=1)
    fig.update_layout(title=f"{symbol} 15m — regime ribbon (색=상태, 불투명도=confidence; "
                            f"흐려지는 띠 = 전환 진행 중)",
                      height=650, template="plotly_white", legend=dict(orientation="h"))
    fig.write_html(RESULTS / f"{symbol}_ribbon.html", include_plotlyjs=True)


def scorecard(symbol, df, f, b1, excluded):
    idx = f.index
    sup_f = leadlag.to_super(f["regime"])
    sup_b = leadlag.to_super(b1)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.45, 0.25, 0.3],
                        vertical_spacing=0.03,
                        subplot_titles=["price", "enter-TREND events: B1(빈 삼각) vs F(채운 삼각), "
                                                 "연결선 길이 = lead", "confidence (수직선 = collapse)"])
    fig.add_trace(go.Scattergl(x=idx, y=df["close"].reindex(idx), name="close",
                               line=dict(color="#222", width=1), showlegend=False), row=1, col=1)

    mp = leadlag.DEFAULT_MP
    for cls, col in (("UP", COLORS["TREND_UP"]), ("DOWN", COLORS["TREND_DOWN"])):
        be = leadlag.episodes(sup_b, cls, mp.min_len)
        fe = leadlag.episodes(sup_f, cls, mp.min_len)
        pairs, _, _ = leadlag.match(be, fe, mp.min_overlap)
        sym_dir = "triangle-up" if cls == "UP" else "triangle-down"
        y_b, y_f = (1.0, 0.6) if cls == "UP" else (-1.0, -0.6)
        fig.add_trace(go.Scattergl(x=[idx[s] for s, _ in be], y=[y_b] * len(be), mode="markers",
                                   name=f"B1 enter {cls}",
                                   marker=dict(symbol=f"{sym_dir}-open", size=9, color=col)),
                      row=2, col=1)
        fig.add_trace(go.Scattergl(x=[idx[s] for s, _ in fe], y=[y_f] * len(fe), mode="markers",
                                   name=f"F enter {cls}",
                                   marker=dict(symbol=sym_dir, size=9, color=col)),
                      row=2, col=1)
        xs, ys = [], []
        for bi, fi, _ in pairs:
            xs += [idx[be[bi][0]], idx[fe[fi][0]], None]
            ys += [y_b, y_f, None]
        fig.add_trace(go.Scattergl(x=xs, y=ys, mode="lines", showlegend=False,
                                   line=dict(color=col, width=0.7), opacity=0.5), row=2, col=1)
    fig.update_yaxes(range=[-1.4, 1.4], tickvals=[-1, -0.6, 0.6, 1],
                     ticktext=["B1 DOWN", "F DOWN", "F UP", "B1 UP"], row=2, col=1)

    conf = f["confidence"]
    fig.add_trace(go.Scattergl(x=idx, y=conf, name="confidence",
                               line=dict(color="#1f77b4", width=1)), row=3, col=1)
    thr = conf.rolling(2880, min_periods=2880).quantile(0.2).shift(1)
    fig.add_trace(go.Scattergl(x=idx, y=thr, name="rolling q20",
                               line=dict(color="#aaa", width=1, dash="dot")), row=3, col=1)
    ev = run_all.collapse_events(conf, f["fuel_available"], excluded)
    fig.add_trace(go.Scattergl(x=idx[ev], y=conf.iloc[ev], mode="markers", name="collapse",
                               marker=dict(color="#d62728", size=5, symbol="x")), row=3, col=1)
    fig.update_layout(title=f"{symbol} — lead-lag scorecard (이 패널이 채점표)",
                      height=900, template="plotly_white", legend=dict(orientation="h"))
    fig.write_html(RESULTS / f"{symbol}_scorecard.html", include_plotlyjs=True)


def transitions(symbol, f):
    lab = f["regime"].dropna().to_numpy(object)
    states = list(classifier.REGIMES)
    counts = {(a, b): 0 for a in states for b in states if a != b}
    for a, b in zip(lab, lab[1:]):
        if a != b:
            counts[(a, b)] += 1
    total = sum(counts.values())
    ang = {s: np.pi / 2 - 2 * np.pi * i / 5 for i, s in enumerate(states)}
    pos = {s: (np.cos(a), np.sin(a)) for s, a in ang.items()}
    fig = go.Figure()
    mx = max(counts.values())
    for (a, b), c in counts.items():
        if c == 0:
            continue
        x0, y0 = pos[a]
        x1, y1 = pos[b]
        # offset each direction sideways so a->b and b->a don't overlap
        dx, dy = x1 - x0, y1 - y0
        norm = np.hypot(dx, dy)
        ox, oy = -dy / norm * 0.05, dx / norm * 0.05
        fig.add_annotation(x=x1 * 0.88 + ox, y=y1 * 0.88 + oy,
                           ax=x0 * 1.02 + ox, ay=y0 * 1.02 + oy,
                           xref="x", yref="y", axref="x", ayref="y",
                           arrowwidth=max(0.5, c / mx * 10), arrowhead=3, arrowsize=0.8,
                           arrowcolor=COLORS[a], opacity=0.75,
                           hovertext=f"{a}→{b}: {c} ({c / total:.1%})")
    for s, (x, y) in pos.items():
        fig.add_trace(go.Scatter(x=[x * 1.15], y=[y * 1.15], mode="markers+text", text=[s],
                                 textposition="middle center", name=s, showlegend=False,
                                 marker=dict(size=58, color=COLORS[s], opacity=0.9),
                                 textfont=dict(size=9, color="white")))
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    top_txt = "  ·  ".join(f"{a}→{b} {c}" for (a, b), c in top)
    fig.update_layout(title=f"{symbol} — state transitions (화살표 굵기=빈도)<br>"
                            f"<sub>top: {top_txt} — CHOP↔RANGE 왕복이 굵으면 churn, dwell/confirm ↑ 고려</sub>",
                      xaxis=dict(visible=False, range=[-1.6, 1.6]),
                      yaxis=dict(visible=False, range=[-1.5, 1.5], scaleanchor="x"),
                      height=700, width=780, template="plotly_white")
    fig.write_html(RESULTS / f"{symbol}_transitions.html", include_plotlyjs=True)


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    df, excluded, _ = run_all.load(symbol)
    feat, f, b2, b1 = run_all.tracks(df)
    f, b1 = run_all.trim(f), run_all.trim(b1)
    exc = excluded[run_all.BURN:]
    RESULTS.mkdir(exist_ok=True)
    ribbon(symbol, df, f)
    scorecard(symbol, df, f, b1, exc)
    transitions(symbol, f)
    print(f"wrote {RESULTS}/{symbol}_{{ribbon,scorecard,transitions}}.html")


if __name__ == "__main__":
    main()
