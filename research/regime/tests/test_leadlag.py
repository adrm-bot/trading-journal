"""The measuring instrument is validated BEFORE it touches real data.

Three mandatory gates (a clean known-lead recovery alone would also pass a matcher that is
gameable by churn — that is exactly the failure mode episode matching exists to close):
  (a) noisy NON-leader: extra spurious episodes, zero injected lead -> median lead ~ 0,
      false alarms reported
  (b) noisy leader: injected lead k + spurious episodes -> recovers k without inflation
  (c) gap case: episodes near excluded bars are dropped and counted, remaining leads intact
"""
import numpy as np

import leadlag
from leadlag import MatchParams


def build_track(n=20000, ep_len=40, gap_len=80, seed=3):
    """Baseline: alternating NT blocks and UP/DOWN episodes."""
    g = np.random.default_rng(seed)
    sup = np.array(["NT"] * n, dtype=object)
    pos, cls = gap_len, 0
    starts = []
    while pos + ep_len < n - gap_len:
        c = "UP" if cls % 2 == 0 else "DOWN"
        sup[pos:pos + ep_len] = c
        starts.append((pos, c))
        pos += ep_len + gap_len
        cls += 1
    return sup, starts


def shift_earlier(sup, k):
    """Fused track that enters (and exits) every episode k bars EARLIER."""
    out = np.roll(sup.copy(), -k)
    out[-k:] = "NT"
    return out


def inject_spurious(sup, n_spur, seed=9, length=6):
    g = np.random.default_rng(seed)
    out = sup.copy()
    n = len(out)
    added = 0
    tries = 0
    while added < n_spur and tries < n_spur * 50:
        tries += 1
        s = int(g.integers(0, n - length))
        seg = out[s - 2:s + length + 2]
        if all(x == "NT" for x in seg):  # only inside NT zones, not touching real episodes
            out[s:s + length] = g.choice(["UP", "DOWN"])
            added += 1
    assert added == n_spur
    return out


def median_lead(base, fused, mp=MatchParams(), excluded=None):
    leads = np.concatenate([
        leadlag.class_stats(base, fused, c, mp, excluded)["leads"] for c in ("UP", "DOWN")])
    return np.median(leads), leads


def test_clean_known_lead_recovered():
    base, _ = build_track()
    for k in (2, 5, 10):
        fused = shift_earlier(base, k)
        med, leads = median_lead(base, fused)
        assert med == k, f"expected lead {k}, got {med}"
        assert len(leads) > 100


def test_noisy_non_leader_reports_zero():
    base, _ = build_track()
    fused = inject_spurious(base, n_spur=120)  # ~75% extra episodes, ZERO real lead
    med, leads = median_lead(base, fused)
    assert abs(med) <= 1, f"churny non-leader must not fake lead, got median {med}"
    st = leadlag.class_stats(base, fused, "UP")
    assert st["n_false"] > 20, "spurious episodes must surface as false alarms"


def test_noisy_leader_recovers_k_without_inflation():
    base, _ = build_track()
    k = 5
    fused = inject_spurious(shift_earlier(base, k), n_spur=120)
    med, _ = median_lead(base, fused)
    assert abs(med - k) <= 1, f"expected ~{k}, got {med}"


def test_gap_exclusion():
    base, starts = build_track()
    k = 5
    fused = shift_earlier(base, k)
    excluded = np.zeros(len(base), dtype=bool)
    # kill the zone around the 3rd and 4th episode starts
    for s, _ in starts[2:4]:
        excluded[s - 10:s + 10] = True
    mp = MatchParams()
    st_up = leadlag.class_stats(base, fused, "UP", mp, excluded)
    st_dn = leadlag.class_stats(base, fused, "DOWN", mp, excluded)
    assert st_up["n_excluded_base"] + st_dn["n_excluded_base"] >= 2
    med = np.median(np.concatenate([st_up["leads"], st_dn["leads"]]))
    assert med == k, "exclusion must not distort surviving leads"


def test_direct_flip_is_two_episodes():
    sup = np.array(["NT"] * 20 + ["UP"] * 10 + ["DOWN"] * 10 + ["NT"] * 20, dtype=object)
    ups = leadlag.episodes(sup, "UP", 4)
    dns = leadlag.episodes(sup, "DOWN", 4)
    assert ups == [(20, 29)] and dns == [(30, 39)]


def test_blips_are_not_episodes():
    sup = np.array(["NT"] * 50, dtype=object)
    sup[10:12] = "UP"  # 2-bar blip < min_len 4
    assert leadlag.episodes(sup, "UP", 4) == []


def test_churn_and_flap():
    sup = np.array(["NT", "UP", "NT", "UP", "NT", "NT", "NT", "NT"], dtype=object)
    assert leadlag.churn3(sup) > 0
    assert 0.0 <= leadlag.flap_rate(sup) <= 1.0
