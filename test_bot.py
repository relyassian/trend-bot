"""Offline tests: a fake broker stands in for Alpaca so every path can be checked without keys."""
from datetime import datetime

import pytest

from bot import Config, rebalance, trend_signal, ET

UP = [100.0] * 199 + [110.0]      # last close above 200-day avg
DOWN = [100.0] * 199 + [90.0]     # last close below


class FakeBroker:
    def __init__(self, closes, cash=0.0, positions=None, fractionable=True, prices=None,
                 sell_status="filled", open_orders=0):
        self.closes = closes
        self._cash = cash
        self._pos = dict(positions or {})
        self._frac = fractionable
        self._prices = prices or {"SSO": 100.0, "BIL": 91.5}
        self.sell_status = sell_status
        self._open = open_orders
        self.orders = []

    def daily_closes(self, symbol): return self.closes
    def cash(self): return self._cash
    def equity(self): return self._cash + sum(self._pos.values())
    def positions(self): return dict(self._pos)
    def fractionable(self, symbol): return self._frac
    def last_price(self, symbol): return self._prices[symbol]
    def open_orders(self): return self._open
    def week_change(self): return 0.01

    def sell_all(self, symbol):
        self.orders.append(("sell", symbol))
        if self.sell_status == "filled":
            self._cash += self._pos.pop(symbol)
        return self.sell_status

    def buy(self, symbol, notional=None, qty=None):
        amt = notional if notional is not None else qty * self._prices[symbol]
        self.orders.append(("buy", symbol, round(amt, 2)))
        self._cash -= amt
        self._pos[symbol] = self._pos.get(symbol, 0) + amt
        return "filled"


CFG = Config(alpaca_key="k", alpaca_secret="s")
WED = datetime(2026, 10, 7, 10, 45, tzinfo=ET)
FRI = datetime(2026, 10, 9, 10, 45, tzinfo=ET)


def test_signal_math():
    assert trend_signal(UP, 200)[0] is True
    assert trend_signal(DOWN, 200)[0] is False
    with pytest.raises(ValueError):
        trend_signal([1.0] * 50, 200)


def test_first_run_uptrend_invests_all_cash_in_sso():
    b = FakeBroker(UP, cash=1000)
    r = rebalance(b, CFG, WED)
    assert r.target == "SSO"
    assert b.orders == [("buy", "SSO", 999.95)]


def test_switch_out_sells_sso_buys_bil():
    b = FakeBroker(DOWN, positions={"SSO": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == [("sell", "SSO"), ("buy", "BIL", 499.95)]
    assert "SWITCHED to BIL" in r.messages[0]


def test_switch_in_sells_bil_buys_sso():
    b = FakeBroker(UP, positions={"BIL": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == [("sell", "BIL"), ("buy", "SSO", 499.95)]
    assert "SWITCHED to SSO" in r.messages[0]


def test_deposit_is_swept_into_current_holding():
    b = FakeBroker(UP, cash=20.0, positions={"SSO": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == [("buy", "SSO", 19.95)]
    assert "Invested $19.95" in r.messages[0]


def test_nothing_to_do_places_no_orders():
    b = FakeBroker(UP, cash=0.03, positions={"SSO": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == []
    assert any("holding SSO" in m for m in r.messages)


def test_not_fractionable_buys_whole_shares_and_reports_leftover():
    b = FakeBroker(UP, cash=250.0, fractionable=False)
    rebalance(b, CFG, WED)
    assert b.orders == [("buy", "SSO", 200.0)]          # 2 shares at $100
    b2 = FakeBroker(UP, cash=20.0, fractionable=False)  # can't afford 1 share
    r2 = rebalance(b2, CFG, WED)
    assert b2.orders == []
    assert any("waiting" in m for m in r2.messages)


def test_failed_sell_does_not_buy():
    b = FakeBroker(DOWN, positions={"SSO": 500.0}, sell_status="rejected")
    r = rebalance(b, CFG, WED)
    assert b.orders == [("sell", "SSO")]
    assert any(m.startswith("❌") for m in r.messages)


def test_pending_orders_skip_the_day():
    b = FakeBroker(DOWN, positions={"SSO": 500.0}, open_orders=1)
    rebalance(b, CFG, WED)
    assert b.orders == []


def test_unrelated_positions_are_left_alone():
    b = FakeBroker(UP, positions={"AAPL": 300.0, "SSO": 100.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == []
    assert any("AAPL" in m for m in r.messages)


def test_dry_run_places_no_orders_but_reports():
    cfg = Config(alpaca_key="k", alpaca_secret="s", dry_run=True)
    b = FakeBroker(DOWN, positions={"SSO": 500.0})
    r = rebalance(b, cfg, WED)
    assert b.orders == []
    assert "dry run" in r.messages[0]


def test_friday_adds_weekly_summary():
    b = FakeBroker(UP, positions={"SSO": 500.0})
    r = rebalance(b, CFG, FRI)
    assert any("Last 5 trading days" in m for m in r.messages)
