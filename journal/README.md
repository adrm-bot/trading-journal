# 매매일지 (재량 거래 자동 기록 + 사후 의도 캡처 + 행동교정 다이제스트)

거래소(Bybit·Binance…) 청산 거래를 **read-only**로 긁어 일지(Excel 또는 개인 Notion)에 적재.
진입 때 선언 0(마찰 0) → 계획 없던 거래는 `상태=의도 미기입`으로 잡아두고 **사후에 채운다.**
매일 **행동교정 다이제스트**(무계획·집중·연패·근접 재진입)를 Telegram으로 푸시.

설계 배경: `~/.claude/plans/temporal-kindling-hellman.md` · 제품화 분석: task `wweyf85lc`.

## 파일
| 파일 | 역할 |
|---|---|
| `pull_trades.py` | 거래소 청산거래 → 일지 적재 (ccxt, 멀티거래소, 멱등) |
| `setup_notion.py` | 개인 Notion에 일지 DB 1회 생성 |
| `journal_io.py` | 활성 백엔드(Excel/Notion)에서 일지 읽기 |
| `behaviors.py` | 행동 패턴 계산 (무계획·집중·연패·근접재진입) |
| `notify.py` | 행동 다이제스트 → Telegram 푸시 |
| `run_all.py` / `run.bat` | 적재+알림 한 번에 (스케줄러용) |

## 설정
1. `pip install -r requirements.txt`
2. `.env.example` → `.env` 복사 후 채움.
3. **거래소 read-only 키**(주문/출금 OFF): `EXCHANGES=bybit,binance` + 각 `*_API_KEY/SECRET`.
4. **저장처**: `JOURNAL_BACKEND=excel`(로컬, 인증0) 또는 `notion`.
   - Notion(개인계정): ① notion.so/my-integrations에서 integration → `NOTION_TOKEN` ② Notion 페이지 하나 만들어 `⋯ → Connections`로 integration 연결 → 페이지 id를 `NOTION_PARENT_PAGE_ID` ③ `python setup_notion.py` → 나온 id를 `NOTION_DB_ID`.
5. **Telegram(선택)**: @BotFather 봇 → `TELEGRAM_BOT_TOKEN`, chat id → `TELEGRAM_CHAT_ID`.

## 실행
```
python pull_trades.py            # 적재 (멱등, 중복 0)
python notify.py                 # 행동 다이제스트 (토큰 없으면 콘솔)
python run_all.py                # 적재 + 다이제스트 한 번에
python pull_trades.py --pending  # 의도 미기입 목록 (excel)
```
초기 백필: `.env`의 `LOOKBACK_DAYS`를 30~180으로 잠깐 올려 1회.

## 매일 자동 (Windows 작업 스케줄러)
```
schtasks /Create /SC DAILY /ST 09:00 /TN "MaeMaeJournal" /TR "%~dp0run.bat"
```
(또는 작업 스케줄러 GUI에서 `journal\run.bat`을 매일 등록.)
`run.bat`은 Store 파이썬 스텁을 피해 실제 인터프리터 절대경로를 쓴다.

## 사후 의도 캡처
`상태=의도 미기입` 거래에 **계획/의도·셋업·무효선·감정**을 채운다 — Notion/Excel에 직접, 또는 Claude에게 "미기입 캡처하자".

## 범위 / 안전
- ✅ 멀티거래소 적재 · 멱등 · 행동 다이제스트 · 사후 의도 · 멀티 저장처
- ⏸️ 나중: 진입 물타기 정밀탐지(execution/list 필요) · R 자동계산(SL 필요) · 데스크톱앱(Tauri) · 친구 배포(OAuth)
- 🔒 **read-only 전용**(주문/출금 금지) · 키는 `.env`(미커밋, 기기에만) · 외부 push는 집계 숫자만
