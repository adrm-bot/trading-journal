"""미국 주요 경제 일정 — 공식 발표 일정 집계 API를 정직하게 축약해 제공한다."""
from __future__ import annotations

import copy
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests


API_URL = os.getenv("ECON_CALENDAR_URL", "https://api.fxmacrodata.com/v1/calendar/usd")
CACHE_TTL = int(os.getenv("ECON_CALENDAR_TTL_SEC", "21600"))
KST = timezone(timedelta(hours=9))
_lock = threading.Lock()
_cache: dict = {}

# 같은 시각에 CPI 변형 4개, 고용지표 3개가 쏟아지는 원본을 의사결정 단위로 묶는다.
# value = (group, user label, priority within group)
EVENTS = {
    "inflation": ("cpi", "미국 CPI", 0),
    "inflation_mom": ("cpi", "미국 CPI", 1),
    "core_inflation": ("cpi", "미국 CPI", 2),
    "core_inflation_mom": ("cpi", "미국 CPI", 3),
    "ppi": ("ppi", "미국 PPI", 0),
    "retail_sales": ("retail", "미국 소매판매", 0),
    "policy_rate": ("fomc", "FOMC 금리결정", 0),
    "fomc_minutes": ("fomc_minutes", "FOMC 의사록", 0),
    "core_pce": ("pce", "미국 Core PCE", 0),
    "pce": ("pce", "미국 Core PCE", 1),
    "gdp": ("gdp", "미국 GDP", 0),
    "non_farm_payrolls": ("jobs", "미국 고용보고서", 0),
    "unemployment": ("jobs", "미국 고용보고서", 1),
    "employment": ("jobs", "미국 고용보고서", 2),
}


def build_events(rows: list[dict] | None, now: datetime | None = None, limit: int = 6) -> list[dict]:
    """원본 일정에서 향후 45일 핵심 이벤트만 선택하고 발표 묶음 중복을 제거한다."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ts = int(now.timestamp())
    end_ts = now_ts + 45 * 86_400
    chosen: dict[tuple[str, int], tuple[int, dict, str]] = {}
    for row in rows or []:
        release = str(row.get("release") or "").lower()
        spec = EVENTS.get(release)
        if not spec:
            continue
        try:
            ts = int(row.get("announcement_datetime") or 0)
        except (TypeError, ValueError):
            continue
        if ts < now_ts - 7_200 or ts > end_ts:
            continue
        group, label, priority = spec
        key = (group, ts)
        if key not in chosen or priority < chosen[key][0]:
            chosen[key] = (priority, row, label)

    today = now.astimezone(KST).date()
    out = []
    for (_, ts), (_, row, label) in sorted(chosen.items(), key=lambda x: x[0][1]):
        at = datetime.fromtimestamp(ts, timezone.utc).astimezone(KST)
        imp = "high" if str(row.get("event_importance") or "").lower() == "high" else "med"
        out.append({
            "id": f"{row.get('release') or 'event'}:{ts}",
            "title": label,
            "at": at.isoformat(),
            "date": at.strftime("%m.%d"),
            "time": at.strftime("%H:%M"),
            "dd": (at.date() - today).days,
            "imp": imp,
            "confirmed": bool(row.get("release_date_confirmed")),
            "source": row.get("source") or "Official release calendars",
            "source_url": row.get("source_url") or "",
        })
    return out[:max(1, int(limit))]


def calendar() -> dict:
    """6시간 캐시. 일시 장애 때는 마지막 성공값을 stale 표시로 보존한다."""
    now = time.time()
    with _lock:
        if _cache and now - _cache.get("at", 0) < CACHE_TTL:
            return copy.deepcopy(_cache["data"])
        previous = copy.deepcopy(_cache.get("data")) if _cache else None
        try:
            r = requests.get(API_URL, params={"timezone": "Asia/Seoul"}, timeout=8,
                             headers={"User-Agent": "TradingJournal/1.0"})
            r.raise_for_status()
            raw = r.json()
            events = build_events(raw.get("data") if isinstance(raw, dict) else [])
            quality = (raw.get("data_quality") or {}) if isinstance(raw, dict) else {}
            data = {
                "available": True,
                "events": events,
                "source": "FXMacroData · 공식 발표 일정 집계",
                "source_url": "https://fxmacrodata.com/documentation",
                "asof": datetime.now(timezone.utc).isoformat(),
                "stale": bool(quality.get("is_stale")),
                "quality": "official_schedule" if quality.get("is_official") else "aggregated_schedule",
            }
            _cache.clear()
            _cache.update({"at": now, "data": data})
            return copy.deepcopy(data)
        except (requests.RequestException, ValueError, TypeError):
            if previous:
                previous["stale"] = True
                previous["error"] = "경제 일정 갱신 지연"
                return previous
            return {"available": False, "events": [], "error": "경제 일정을 불러오지 못했습니다"}
