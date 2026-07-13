from app import engine


def _position(direction="Long"):
    return {
        "symbol": "BTC/USDT:USDT",
        "info": {"positionSide": direction.upper(), "positionIdx": "1" if direction == "Long" else "2"},
    }


def _row(direction="Long"):
    return {"direction": direction, "entry": 100.0, "qty": 2.0}


def test_exit_orders_keep_only_matching_close_orders_and_classify_levels():
    orders = [
        {"id": "sl", "side": "sell", "type": "stop_market", "triggerPrice": 92,
         "amount": 2, "reduceOnly": True, "info": {"positionSide": "LONG", "positionIdx": "1"}},
        {"id": "tp", "side": "sell", "type": "limit", "price": 118,
         "remaining": 1, "reduceOnly": True, "info": {"positionSide": "LONG", "positionIdx": "1"}},
        {"id": "opens-short", "side": "sell", "type": "limit", "price": 125,
         "amount": 1, "reduceOnly": False, "info": {"positionSide": "BOTH"}},
        {"id": "wrong-side", "side": "buy", "type": "stop_market", "triggerPrice": 90,
         "amount": 2, "reduceOnly": True, "info": {"positionSide": "LONG", "positionIdx": "1"}},
        {"id": "wrong-hedge-leg", "side": "sell", "type": "take_profit_market", "triggerPrice": 130,
         "amount": 2, "reduceOnly": True, "info": {"positionSide": "SHORT", "positionIdx": "2"}},
    ]

    result = engine._normalise_exit_orders(_row(), _position(), orders)

    assert [(o["kind"], o["price"], o["qty"]) for o in result] == [
        ("sl", 92.0, 2.0),
        ("tp", 118.0, 1.0),
    ]


def test_position_embedded_stop_and_target_are_exposed_as_exchange_orders():
    position = _position()
    position["info"].update({"stopLoss": "91.5", "takeProfit": "121.25"})

    result = engine._normalise_exit_orders(_row(), position, [])

    assert [(o["kind"], o["price"]) for o in result] == [("sl", 91.5), ("tp", 121.25)]


def test_bybit_open_order_fetch_includes_conditional_orders_and_deduplicates():
    class Exchange:
        has = {"fetchOpenOrders": True}

        def __init__(self):
            self.params = []

        def fetch_open_orders(self, symbol, since, limit, params):
            self.params.append(params)
            return [{"id": "same", "side": "sell", "type": "stop_market", "triggerPrice": 90}]

    exchange = Exchange()
    orders, status = engine._fetch_position_orders(exchange, "bybit", "BTC/USDT:USDT")

    assert status == "ok"
    assert len(orders) == 1
    assert exchange.params == [
        {"category": "linear"},
        {"category": "linear", "orderFilter": "StopOrder"},
    ]


def test_open_order_fetch_marks_regular_only_result_as_partial():
    class Exchange:
        has = {"fetchOpenOrders": True}

        def fetch_open_orders(self, symbol, since, limit, params):
            if params.get("stop"):
                raise RuntimeError("conditional endpoint unavailable")
            return [{"id": "tp", "side": "sell", "type": "limit", "price": 120}]

    orders, status = engine._fetch_position_orders(Exchange(), "binance", "BTC/USDT:USDT")

    assert len(orders) == 1
    assert status == "partial"
