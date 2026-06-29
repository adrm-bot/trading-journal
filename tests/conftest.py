"""골든 회귀테스트 공용 팩토리/헬퍼. app.engine만 의존(키·DB·네트워크 불필요)."""
from app import engine

DAY = 86_400_000
T0 = 1_700_000_000_000  # 고정 기준 ms (UTC)


def fill(tid, side, price, qty, t, *, pnl=0.0, fee=0.0, pos_side="BOTH"):
    """Binance/Gate userTrades fill (reconstruct_walk 입력 스키마)."""
    return {"id": str(tid), "time": int(t), "side": side, "price": float(price),
            "qty": float(qty), "commission": float(fee), "realizedPnl": float(pnl),
            "positionSide": pos_side}


def cpnl(order_id, symbol, side, entry, exit_, size, pnl, created, updated=None, *,
         open_fee=0.0, close_fee=0.0, leverage=10, exec_type="Trade", fill_count=1):
    """Bybit closed-pnl 레코드 (reconstruct_bybit 입력 스키마). side: Sell=롱청산 / Buy=숏청산."""
    return {"orderId": str(order_id), "symbol": symbol, "side": side,
            "avgEntryPrice": entry, "avgExitPrice": exit_, "closedSize": size,
            "qty": size, "closedPnl": pnl, "createdTime": str(created),
            "updatedTime": str(updated if updated is not None else created),
            "openFee": open_fee, "closeFee": close_fee, "leverage": leverage,
            "execType": exec_type, "fillCount": fill_count}


def ts(ms):
    return engine._ts_str(ms)


def assert_pnl_sum(rows, expected, tol=1e-6):
    assert abs(sum(r["pnl"] for r in rows) - expected) <= tol
