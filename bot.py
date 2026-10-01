"""
Trend bot: hold a 2x S&P 500 fund while the S&P 500 is above its 200-day average,
otherwise hold T-bills. Runs once per weekday during market hours.

Rule (matches the backtest in ../trading_research):
  signal = SPY's last completed daily close > average of its last 200 daily closes
  ON  -> whole account in RISK_SYMBOL (default SSO, 2x S&P 500)
  OFF -> whole account in SAFE_SYMBOL (default BIL, 1-3 month T-bills)

Every run also sweeps idle cash (deposits, dividends) into whatever the bot is holding.
Only the account's settled `cash` is invested — never margin.
"""
from __future__ import annotations

import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
MIN_ORDER_USD = 1.00      # Alpaca's smallest practical notional order
CASH_BUFFER_USD = 0.05    # leave a few cents so rounding never rejects an order


# ---------------------------------------------------------------- config
@dataclass
class Config:
    alpaca_key: str
    alpaca_secret: str
    paper: bool = True
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    signal_symbol: str = "SPY"
    risk_symbol: str = "SSO"
    safe_symbol: str = "BIL"
    sma_days: int = 200
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        def need(k):
            v = os.environ.get(k, "").strip()
            if not v:
                sys.exit(f"Missing required setting: {k}")
            return v
        return cls(
            alpaca_key=need("ALPACA_KEY"),
            alpaca_secret=need("ALPACA_SECRET"),
            paper=os.environ.get("ALPACA_PAPER", "true").lower() != "false",
            telegram_token=os.environ.get("TELEGRAM_TOKEN") or None,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
            signal_symbol=os.environ.get("SIGNAL_SYMBOL", "SPY"),
            risk_symbol=os.environ.get("RISK_SYMBOL", "SSO"),
            safe_symbol=os.environ.get("SAFE_SYMBOL", "BIL"),
            sma_days=int(os.environ.get("SMA_DAYS", "200")),
            dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
        )


# ---------------------------------------------------------------- signal
def trend_signal(closes: list[float], sma_days: int) -> tuple[bool, float, float]:
    """Returns (is_uptrend, last_close, sma). Needs at least sma_days closes."""
    if len(closes) < sma_days:
        raise ValueError(f"Need {sma_days} daily closes, got {len(closes)}")
    window = closes[-sma_days:]
    sma = sum(window) / sma_days
    last = closes[-1]
    return last > sma, last, sma


# ---------------------------------------------------------------- broker
class Broker:
    """Thin wrapper over alpaca-py so the trading logic can be tested with a fake."""

    def __init__(self, cfg: Config):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.paper)
        self.data = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)

    def market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def daily_closes(self, symbol: str) -> list[float]:
        """Completed daily closes up to yesterday (today's partial bar excluded)."""
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment, DataFeed
        today_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_midnight - timedelta(days=420)
        last_err = None
        for feed in (DataFeed.SIP, DataFeed.IEX):  # SIP = full-market closes; IEX fallback
            try:
                req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                       start=start, end=today_midnight,
                                       adjustment=Adjustment.ALL, feed=feed)
                bars = self.data.get_stock_bars(req).data.get(symbol, [])
                closes = [float(b.close) for b in bars
                          if b.timestamp.astimezone(ET).date() < today_midnight.date()]
                if closes:
                    return closes
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"Could not load price history for {symbol}: {last_err}")

    def cash(self) -> float:
        return float(self.trading.get_account().cash)

    def equity(self) -> float:
        return float(self.trading.get_account().equity)

    def positions(self) -> dict[str, float]:
        """symbol -> market value"""
        return {p.symbol: float(p.market_value) for p in self.trading.get_all_positions()}

    def fractionable(self, symbol: str) -> bool:
        return bool(self.trading.get_asset(symbol).fractionable)

    def last_price(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest
        t = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(t[symbol].price)

    def sell_all(self, symbol: str) -> str:
        order = self.trading.close_position(symbol)
        return self._wait(order.id)

    def buy(self, symbol: str, notional: float | None = None, qty: int | None = None) -> str:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = MarketOrderRequest(symbol=symbol, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                 notional=round(notional, 2) if notional else None, qty=qty)
        order = self.trading.submit_order(req)
        return self._wait(order.id)

    def open_orders(self) -> int:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        return len(self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def week_change(self) -> float | None:
        """Account % change over the last week from Alpaca's portfolio history."""
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = self.trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1W", timeframe="1D"))
        eq = [e for e in (h.equity or []) if e]
        return (eq[-1] / eq[0] - 1) if len(eq) >= 2 else None

    def _wait(self, order_id, timeout=90) -> str:
        deadline = time.time() + timeout
        status = "unknown"
        while time.time() < deadline:
            o = self.trading.get_order_by_id(order_id)
            status = str(o.status.value if hasattr(o.status, "value") else o.status)
            if status in ("filled", "canceled", "expired", "rejected"):
                return status
            time.sleep(2)
        return status


# ---------------------------------------------------------------- telegram
def notify(cfg: Config, text: str) -> None:
    print(text)
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                      json={"chat_id": cfg.telegram_chat_id, "text": text,
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e:  # noqa: BLE001  (never let a Telegram outage stop trading)
        print(f"Telegram send failed: {e}")


# ---------------------------------------------------------------- core logic
@dataclass
class RunResult:
    uptrend: bool
    target: str
    actions: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def rebalance(broker, cfg: Config, now: datetime | None = None) -> RunResult:
    now = now or datetime.now(ET)
    closes = broker.daily_closes(cfg.signal_symbol)
    up, last, sma = trend_signal(closes, cfg.sma_days)
    target = cfg.risk_symbol if up else cfg.safe_symbol
    other = cfg.safe_symbol if up else cfg.risk_symbol
    res = RunResult(uptrend=up, target=target)
    pct_vs_sma = (last / sma - 1) * 100
    mode = "PAPER" if cfg.paper else "LIVE"
    tag = " (dry run)" if cfg.dry_run else ""

    if broker.open_orders():
        res.messages.append(f"⚠️ [{mode}] Orders from an earlier run are still pending. "
                            "Skipping today so nothing gets doubled up.")
        return res

    # 1) Switch: sell everything not in the target.
    held = broker.positions()
    switched = False
    for sym, value in held.items():
        if sym != target and value > 0:
            if sym not in (cfg.risk_symbol, cfg.safe_symbol):
                res.messages.append(f"⚠️ [{mode}] Found {sym} (${value:,.2f}) in the account. "
                                    "The bot only manages its own two funds, so it left this alone.")
                continue
            res.actions.append(f"SELL all {sym} (${value:,.2f})")
            if not cfg.dry_run:
                status = broker.sell_all(sym)
                if status != "filled":
                    res.messages.append(f"❌ [{mode}] Sell of {sym} ended as '{status}'. "
                                        "No buy placed today; will retry tomorrow.")
                    return res
            switched = sym == other or switched

    # 2) Invest all settled cash into the target (this is also the reinvest/deposit sweep).
    cash = broker.cash() - CASH_BUFFER_USD
    bought = 0.0
    if cash >= MIN_ORDER_USD:
        if broker.fractionable(target):
            res.actions.append(f"BUY ${cash:,.2f} of {target}")
            if not cfg.dry_run:
                status = broker.buy(target, notional=cash)
                if status != "filled":
                    res.messages.append(f"❌ [{mode}] Buy of {target} ended as '{status}'.")
            bought = cash
        else:
            price = broker.last_price(target)
            qty = math.floor(cash / price)
            if qty >= 1:
                res.actions.append(f"BUY {qty} shares of {target} (~${qty * price:,.2f})")
                if not cfg.dry_run:
                    status = broker.buy(target, qty=qty)
                    if status != "filled":
                        res.messages.append(f"❌ [{mode}] Buy of {target} ended as '{status}'.")
                bought = qty * price
            else:
                res.messages.append(f"ℹ️ ${cash:,.2f} cash is waiting: {target} can't be bought "
                                    f"in fractions and one share costs ${price:,.2f}.")

    # 3) Messages
    trend_word = "UP ✅" if up else "DOWN 🛑"
    if switched:
        res.messages.insert(0, f"🔁 [{mode}] SWITCHED to {target}{tag}\n"
                               f"S&P trend is {trend_word}: {cfg.signal_symbol} {last:,.2f} vs "
                               f"200-day avg {sma:,.2f} ({pct_vs_sma:+.1f}%)\n" + "\n".join(res.actions))
    elif bought > 0:
        res.messages.insert(0, f"💵 [{mode}] Invested ${bought:,.2f} of new cash into {target}{tag}")

    equity = broker.equity()
    res.messages.append(f"📊 [{mode}] {now:%a %b %d}: holding {target}. Trend {trend_word} "
                        f"({pct_vs_sma:+.1f}% vs 200-day avg). Account ${equity:,.2f}")

    if now.weekday() == 4:  # Friday weekly summary
        wk = broker.week_change()
        spy_wk = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else None
        if wk is not None and spy_wk is not None:
            res.messages.append(f"🗓 Last 5 trading days: account {wk * 100:+.2f}% vs S&P {spy_wk * 100:+.2f}% "
                                "(new deposits show up as gains in the account number)")
    return res


def main() -> int:
    cfg = Config.from_env()
    try:
        broker = Broker(cfg)
        if not broker.market_open():
            print("Market closed today; nothing to do.")
            return 0
        res = rebalance(broker, cfg)
        for m in res.messages:
            notify(cfg, m)
        return 1 if any(m.startswith("❌") for m in res.messages) else 0
    except Exception as e:  # noqa: BLE001
        notify(cfg, f"❌ Bot error: {e}\n\nIt will try again on its next run. Details are in the GitHub run log.")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
