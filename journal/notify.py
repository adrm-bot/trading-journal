#!/usr/bin/env python3
"""
notify.py — 일지에서 행동교정 다이제스트를 만들어 Telegram으로 푸시.

단순 손익이 아니라 '무계획·집중·연패·근접 재진입'을 들이댄다(behaviors.py).
토큰 없으면 콘솔 출력(dry-run). 활성 백엔드(Excel/Notion)에서 읽는다.

사용:
  python notify.py            # 전체 요약
  python notify.py --days 7   # 최근 7일만
"""
import os, sys
from datetime import datetime, timedelta

import journal_io
import behaviors

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): return False

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def build_digest(days=None) -> str:
    rows = journal_io.load_journal()
    if days:
        cut = datetime.utcnow() - timedelta(days=days)
        rows = [r for r in rows if isinstance(r.get("청산시각"), datetime) and r["청산시각"] >= cut]
    a = behaviors.analyze(rows)
    scope = f"최근 {days}일" if days else "전체"
    if not a["n"]:
        return f"📓 매매일지({scope}): 거래 없음."

    lines = [
        f"📓 *매매일지 다이제스트* ({scope})",
        "─────────────",
        f"총 {a['n']}건 | 손익 *{a['total_pnl']:+.2f}* USDT | 승률 {a['wins']}/{a['n']} ({a['win_rate']*100:.0f}%)",
    ]
    lines += a["flags"]
    recent = sorted([r for r in rows if r.get("청산시각")], key=lambda r: r["청산시각"], reverse=True)[:5]
    if recent:
        lines.append("\n최근:")
        for r in recent:
            flag = " ⚠️" if r.get("상태") == "의도 미기입" else ""
            pnl = r.get("실현손익(USDT)") or 0.0
            lines.append(f" • {r['청산시각'].strftime('%m-%d %H:%M')} {r.get('심볼')} {r.get('방향') or ''} {pnl:+.2f}{flag}")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    import requests
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"}, timeout=20)
    if r.status_code != 200:
        sys.exit(f"Telegram 전송 실패: {r.status_code} {r.text}")
    print("Telegram 전송 완료.")


def main():
    days = None
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    digest = build_digest(days)
    if TG_TOKEN and TG_CHAT:
        send_telegram(digest)
    else:
        print("[dry-run — TELEGRAM_BOT_TOKEN/CHAT_ID 미설정, 콘솔 출력]\n")
        print(digest)


if __name__ == "__main__":
    main()
