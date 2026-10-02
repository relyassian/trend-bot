"""Offline tests: a fake broker stands in for Alpaca so every path can be checked without keys."""
from datetime import datetime

import pytest
from datetime import date

from bot import Config, rebalance, trend_signal, compose, money_lines, ET

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
    deposits = None   # None, a number (one deposit on Oct 1), a list of (date, amount), or an Exception
    history = []
    def deposit_history(self):
        if isinstance(self.deposits, Exception):
            raise self.deposits
        if self.deposits is None:
            return []
        if isinstance(self.deposits, (int, float)):
            return [(date(2026, 10, 1), float(self.deposits))]
        return self.deposits
    def equity_history(self):
        return self.history

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
    assert "turned DOWN" in r.messages[0] and "T-bills" in r.messages[0]


def test_switch_in_sells_bil_buys_sso():
    b = FakeBroker(UP, positions={"BIL": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == [("sell", "BIL"), ("buy", "SSO", 499.95)]
    assert "turned UP" in r.messages[0]


def test_deposit_is_swept_into_current_holding():
    b = FakeBroker(UP, cash=20.0, positions={"SSO": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == [("buy", "SSO", 19.95)]
    assert "$19.95 invested" in r.messages[0]


def test_nothing_to_do_places_no_orders():
    b = FakeBroker(UP, cash=0.03, positions={"SSO": 500.0})
    r = rebalance(b, CFG, WED)
    assert b.orders == []
    assert len(r.messages) == 1 and "Account: $500.03" in r.messages[0]


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
    assert r.error and any(m.startswith("❌") for m in r.messages)


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
    assert "TEST ONLY" in compose(r, cfg)


def test_down_trend_daily_text_and_paper_header():
    b = FakeBroker(DOWN, positions={"BIL": 500.0})
    r = rebalance(b, CFG, WED)
    text = compose(r, CFG)
    assert text.startswith("🧪 PRACTICE")
    assert "Market: DOWN" in text and "buys back in" in text


def test_live_mode_has_no_practice_header():
    cfg = Config(alpaca_key="k", alpaca_secret="s", paper=False)
    b = FakeBroker(UP, cash=20.0)
    b.deposits = 20.0
    text = compose(rebalance(b, cfg, WED), cfg)
    assert "PRACTICE" not in text and "TEST ONLY" not in text
    print("\n----- LIVE, deposit day -----\n" + text)
    b2 = FakeBroker(DOWN, positions={"SSO": 500.0})
    print("\n----- LIVE, switch to safety (Friday) -----\n" + compose(rebalance(b2, cfg, FRI), cfg))


def test_profit_line():
    b = FakeBroker(UP, positions={"SSO": 26.0}, cash=0.0)
    b.deposits = 20.0
    msg = rebalance(b, CFG, WED).messages[-1]
    assert "Total: +$6.00 (+30.0%) on $20.00 put in" in msg
    b2 = FakeBroker(UP, positions={"SSO": 15.0})
    b2.deposits = 20.0
    assert "Total: -$5.00 (-25.0%)" in rebalance(b2, CFG, WED).messages[-1]


def test_profit_line_skipped_when_deposits_unavailable():
    b = FakeBroker(UP, positions={"SSO": 26.0})
    b.deposits = RuntimeError("api down")
    msg = rebalance(b, CFG, WED).messages[-1]
    assert "Total" not in msg and "Account: $26.00" in msg


D = date


def test_periods_only_show_when_account_is_old_enough():
    # Account funded Oct 1 with $20; today is Fri Oct 2; yesterday's close was $19.99.
    lines = money_lines(20.22, D(2026, 10, 2), [(D(2026, 9, 30), 0.0), (D(2026, 10, 1), 19.99)],
                        [(D(2026, 10, 1), 20.0)])
    assert lines == ["Total: +$0.22 (+1.1%) on $20.00 put in", "Since yesterday: +$0.23 (+1.2%)"]


def test_monday_says_since_friday_and_week_appears_after_7_days():
    hist = [(D(2026, 10, 1), 20.0), (D(2026, 10, 2), 20.2), (D(2026, 10, 9), 21.0)]
    lines = money_lines(21.5, D(2026, 10, 12), hist, [(D(2026, 10, 1), 20.0)])
    assert "Since Friday: +$0.50 (+2.4%)" in lines
    assert "Past week: +$1.30 (+6.4%)" in lines        # vs Oct 2 close (latest close on/before Oct 5)
    assert not any(l.startswith("Past month") for l in lines)


def test_deposits_are_not_counted_as_gains():
    # $20 on Oct 1, another $20 on Oct 5. Week-ago close $20.10, now $41.00 -> real gain $0.90.
    hist = [(D(2026, 10, 1), 20.0), (D(2026, 10, 2), 20.1), (D(2026, 10, 8), 40.6)]
    deps = [(D(2026, 10, 1), 20.0), (D(2026, 10, 5), 20.0)]
    lines = money_lines(41.0, D(2026, 10, 9), hist, deps)
    assert lines[0] == "Total: +$1.00 (+2.5%) on $40.00 put in"
    assert "Since yesterday: +$0.40 (+1.0%)" in lines
    assert "Past week: +$0.90 (+2.2%)" in lines


def test_month_and_year_appear_when_old_enough():
    hist = [(D(2025, 10, 1), 100.0), (D(2026, 9, 1), 150.0), (D(2026, 9, 30), 155.0), (D(2026, 10, 1), 160.0)]
    lines = money_lines(162.0, D(2026, 10, 2), hist, [(D(2025, 10, 1), 100.0)])
    assert "Past month: +$12.00 (+8.0%)" in lines      # vs Sep 1 close
    assert "Past year: +$62.00 (+62.0%)" in lines      # vs Oct 1, 2025 close


def test_full_message_sample():
    cfg = Config(alpaca_key="k", alpaca_secret="s", paper=False)
    b = FakeBroker(UP, positions={"SSO": 41.0})
    b.deposits = [(D(2026, 10, 1), 20.0), (D(2026, 10, 5), 20.0)]
    b.history = [(D(2026, 10, 1), 20.0), (D(2026, 10, 2), 20.1), (D(2026, 10, 8), 40.6)]
    text = compose(rebalance(b, cfg, datetime(2026, 10, 9, 10, 45, tzinfo=ET)), cfg)
    print("\n----- sample daily message -----\n" + text)
    assert "Since yesterday" in text and "Past week" in text
