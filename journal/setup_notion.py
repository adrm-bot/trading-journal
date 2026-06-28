#!/usr/bin/env python3
"""
setup_notion.py — 네 개인 Notion 계정에 '매매일지' DB를 1회 생성.

준비(journal/.env):
  NOTION_TOKEN=...            # bapk14 계정에서 만든 internal integration secret
  NOTION_PARENT_PAGE_ID=...   # bapk14 의 아무 페이지 id (그 페이지를 integration에 Connections로 연결해둘 것)

실행:
  python setup_notion.py
  → 생성된 database id 가 출력됨. 그걸 .env 의 NOTION_DB_ID 에 붙여넣어라.
"""
import os, sys, requests

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): return False

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("NOTION_TOKEN", "").strip()
PARENT = os.getenv("NOTION_PARENT_PAGE_ID", "").strip()
if not (TOKEN and PARENT):
    sys.exit("NOTION_TOKEN / NOTION_PARENT_PAGE_ID 미설정 (journal/.env)")

H = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

PROPS = {
    "거래": {"title": {}},
    "거래ID": {"rich_text": {}},
    "거래소": {"select": {"options": [{"name": "bybit", "color": "blue"}, {"name": "binance", "color": "yellow"}]}},
    "심볼": {"rich_text": {}},
    "방향": {"select": {"options": [{"name": "Long", "color": "green"}, {"name": "Short", "color": "red"}]}},
    "진입가": {"number": {}},
    "청산가": {"number": {}},
    "수량": {"number": {}},
    "실현손익(USDT)": {"number": {"format": "dollar"}},
    "R": {"number": {}},
    "수수료(USDT)": {"number": {}},
    "보유시간(분)": {"number": {}},
    "청산시각": {"date": {}},
    "상태": {"select": {"options": [{"name": "의도 미기입", "color": "red"}, {"name": "기록완료", "color": "gray"}, {"name": "검토완료", "color": "green"}]}},
    "계획/의도": {"rich_text": {}},
    "셋업": {"rich_text": {}},
    "무효선(SL의도)": {"number": {}},
    "감정": {"select": {"options": [{"name": n, "color": c} for n, c in [("차분", "blue"), ("확신", "green"), ("조급", "yellow"), ("공포", "purple"), ("탐욕", "orange"), ("지루함", "gray")]]}},
    "메모": {"rich_text": {}},
    "출처": {"select": {"options": [{"name": "manual", "color": "default"}, {"name": "auto", "color": "gray"}]}},
}

payload = {
    "parent": {"type": "page_id", "page_id": PARENT},
    "title": [{"type": "text", "text": {"content": "매매일지 (Trade Journal)"}}],
    "properties": PROPS,
}
r = requests.post("https://api.notion.com/v1/databases", headers=H, json=payload, timeout=30)
if r.status_code != 200:
    sys.exit(f"생성 실패: {r.status_code}\n{r.text}\n(integration이 부모 페이지에 Connections로 연결됐는지 확인)")

db_id = r.json()["id"]
print("✅ 매매일지 DB 생성 완료")
print("database id:", db_id)
print("→ journal/.env 의 NOTION_DB_ID 에 이 값을 붙여넣어라.")
print("→ 그다음 JOURNAL_BACKEND=notion 으로 두고 python pull_trades.py")
