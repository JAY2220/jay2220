#!/usr/bin/env python3
"""Backtest a stop-order grid ("straddle grid") on gold (XAUUSD) bar data.

The strategy, as seen in the reel:
  * At the start of each session, place N buy-stop orders above price and
    N sell-stop orders below price, `spacing` apart, `lot` each.
  * A move in one direction fills more and more orders on that side, so the
    position grows with the move.
  * The whole basket is closed when its total profit hits `basket_tp`, its
    total loss hits `basket_sl`, or the session ends. Unfilled orders are
    cancelled and a fresh grid is placed.

Only the standard library is used. Input is a CSV of bars, MT5 export format
or any CSV with date/time + open/high/low/close columns. Prices are bid
prices (MT5 default); ask = bid + spread.

Intrabar path: a bar is walked open -> low -> high -> close when it closed up,
open -> high -> low -> close when it closed down. That is the usual
assumption, but it is a guess: on M1 data the error is small, on H1 it is not.
Use the smallest timeframe you have (M1, or ticks exported as bars).

Usage:
  python3 grid_backtest.py data.csv
  python3 grid_backtest.py data.csv --spacing 0.5 --levels 10 --basket-tp 10 --basket-sl 20
  python3 grid_backtest.py data.csv --sweep "spacing=0.3,0.5,1;basket_tp=5,10;basket_sl=10,20"
"""

import argparse
import csv
import itertools
import math
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

DATE_FORMATS = (
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y%m%d %H:%M:%S", "%Y%m%d %H%M%S", "%Y.%m.%d", "%Y-%m-%d",
)


def parse_time(text):
    text = text.strip()
    if text.replace(".", "", 1).isdigit() and len(text) >= 9:  # unix seconds / ms
        v = float(text)
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"unrecognised date/time: {text!r}")


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    spread: float  # in price units, or NaN when the file has none


def load_bars(path, point):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel_tab if "\t" in sample else csv.excel
        rows = [r for r in csv.reader(fh, dialect) if r and any(c.strip() for c in r)]
    if not rows:
        raise SystemExit(f"{path}: empty file")

    head = [c.strip().strip("<>").lower() for c in rows[0]]
    try:
        parse_time(rows[0][0] + (" " + rows[0][1] if len(rows[0]) > 1 else ""))
        has_header = False
    except ValueError:
        try:
            parse_time(rows[0][0])
            has_header = False
        except ValueError:
            has_header = True

    if has_header:
        col = {name: i for i, name in enumerate(head)}
        body = rows[1:]

        def find(*names):
            for n in names:
                if n in col:
                    return col[n]
            return None

        i_date = find("date", "datetime", "timestamp", "time", "gmt time", "local time")
        i_time = col.get("time") if "date" in col and "time" in col else None
        i_o, i_h, i_l, i_c = find("open", "o"), find("high", "h"), find("low", "l"), find("close", "c")
        i_spread = find("spread")
        if None in (i_date, i_o, i_h, i_l, i_c):
            raise SystemExit(f"{path}: need date/time, open, high, low, close columns; got {head}")
    else:
        # headerless MT4/MT5 style: date, time, open, high, low, close, [volume...]
        body = rows
        first = rows[0]
        try:
            parse_time(first[0] + " " + first[1])
            i_date, i_time, i_o, i_h, i_l, i_c = 0, 1, 2, 3, 4, 5
        except ValueError:
            i_date, i_time, i_o, i_h, i_l, i_c = 0, None, 1, 2, 3, 4
        i_spread = None

    bars = []
    for r in body:
        stamp = r[i_date] if i_time is None else f"{r[i_date]} {r[i_time]}"
        spread = float("nan")
        if i_spread is not None and r[i_spread].strip():
            spread = float(r[i_spread]) * point
        bars.append(Bar(parse_time(stamp), float(r[i_o]), float(r[i_h]),
                        float(r[i_l]), float(r[i_c]), spread))
    bars.sort(key=lambda b: b.time)
    return bars


# --------------------------------------------------------------------------
# strategy + engine
# --------------------------------------------------------------------------

@dataclass
class Params:
    spacing: float = 0.30        # $ between grid orders
    levels: int = 10             # orders on each side
    first_offset: float = 0.30   # $ from price to the first order on each side
    lot: float = 0.01            # lots per order
    contract: float = 100.0      # oz per 1.00 lot
    spread: float = float("nan")  # fixed spread in $; NaN = use the file's spread column
    default_spread: float = 0.10  # used when neither is available
    commission: float = 0.0      # $ per 1.00 lot, round turn
    slippage: float = 0.02       # $ worse fill on every stop order and stop-out
    basket_tp: float = 0.0       # close basket at +$X (0 = off)
    basket_sl: float = 0.0       # close basket at -$X (0 = off)
    session_minutes: int = 60    # rebuild grid every N minutes (0 = only after TP/SL)
    rearm: bool = True           # after TP/SL, place a new grid on the next bar


@dataclass
class Basket:
    start: datetime
    end: datetime = None
    fills: int = 0
    pnl: float = 0.0          # after commission
    commission: float = 0.0
    worst: float = 0.0        # lowest floating P/L seen
    reason: str = ""


@dataclass
class Result:
    params: Params
    baskets: list = field(default_factory=list)
    equity: list = field(default_factory=list)   # (time, closed P/L)


class Grid:
    """One basket: pending stop orders + open positions."""

    def __init__(self, p, bid, spread, start):
        self.p = p
        self.oz = p.lot * p.contract
        ask = bid + spread
        # trigger prices expressed on the bid scale (buy stop fires when ask >= level)
        self.buy_triggers = sorted(ask + p.first_offset + i * p.spacing - spread for i in range(p.levels))
        self.sell_triggers = sorted((bid - p.first_offset - i * p.spacing for i in range(p.levels)), reverse=True)
        self.nb = self.ns = 0
        self.sum_buy = self.sum_sell = 0.0   # sum of entry prices
        self.basket = Basket(start=start)

    def pnl(self, bid, spread):
        ask = bid + spread
        return self.oz * (self.nb * bid - self.sum_buy + self.sum_sell - self.ns * ask) - self.basket.commission

    def slope(self):
        return self.oz * (self.nb - self.ns)

    def open_buy(self, entry_ask):
        self.nb += 1
        self.sum_buy += entry_ask
        self._fill()

    def open_sell(self, entry_bid):
        self.ns += 1
        self.sum_sell += entry_bid
        self._fill()

    def _fill(self):
        self.basket.fills += 1
        self.basket.commission += self.p.commission * self.p.lot

    def close(self, bid, spread, when, reason):
        self.basket.pnl = self.pnl(bid, spread)
        self.basket.worst = min(self.basket.worst, self.basket.pnl)
        self.basket.end = when
        self.basket.reason = reason
        return self.basket

    def note(self, bid, spread):
        self.basket.worst = min(self.basket.worst, self.pnl(bid, spread))

    def hit(self, bid, spread):
        """Basket TP/SL reached at this bid?"""
        v = self.pnl(bid, spread)
        if self.p.basket_tp and v >= self.p.basket_tp - 1e-9:
            return "tp"
        if self.p.basket_sl and v <= -self.p.basket_sl + 1e-9:
            return "sl"
        return None

    def gap_to(self, bid, spread):
        """Price jumped (bar open): fill everything crossed at the new price."""
        while self.buy_triggers and bid >= self.buy_triggers[0]:
            self.buy_triggers.pop(0)
            self.open_buy(bid + spread + self.p.slippage)
        while self.sell_triggers and bid <= self.sell_triggers[0]:
            self.sell_triggers.pop(0)
            self.open_sell(bid - self.p.slippage)
        self.note(bid, spread)

    def walk(self, b0, b1, spread):
        """Move bid continuously from b0 to b1. Returns (exit_bid, reason) if TP/SL hit."""
        up = b1 > b0
        cur = b0
        while True:
            if up:
                nxt = self.buy_triggers[0] if self.buy_triggers and self.buy_triggers[0] <= b1 else None
            else:
                nxt = self.sell_triggers[0] if self.sell_triggers and self.sell_triggers[0] >= b1 else None
            stop = b1 if nxt is None else nxt

            exit_at = self._cross(cur, stop, spread)
            if exit_at is not None:
                return exit_at
            self.note(stop, spread)
            if nxt is None:
                return None
            cur = nxt
            if up:
                self.buy_triggers.pop(0)
                self.open_buy(nxt + spread + self.p.slippage)
            else:
                self.sell_triggers.pop(0)
                self.open_sell(nxt - self.p.slippage)
            reason = self.hit(cur, spread)   # a bad fill can push straight through the SL
            if reason:
                return cur, reason

    def _cross(self, a, b, spread):
        """First bid in [a, b] where floating P/L reaches TP or SL (P/L is linear here)."""
        s = self.slope()
        if s == 0:
            return None
        v0 = self.pnl(a, spread)
        best = None
        for target, reason in ((self.p.basket_tp, "tp"), (-self.p.basket_sl, "sl")):
            if not target:
                continue
            x = a + (target - v0) / s
            lo, hi = min(a, b), max(a, b)
            if lo - 1e-12 <= x <= hi + 1e-12:
                dist = abs(x - a)
                if best is None or dist < best[0]:
                    best = (dist, x, reason)
        if best is None:
            return None
        _, x, reason = best
        if reason == "sl":   # stop-outs fill worse than the level
            x += -self.p.slippage if s > 0 else self.p.slippage
        return x, reason


def bar_path(bar):
    if bar.close >= bar.open:
        return [bar.open, bar.low, bar.high, bar.close]
    return [bar.open, bar.high, bar.low, bar.close]


def session_key(t, minutes):
    if minutes <= 0:
        return 0
    return int(t.replace(tzinfo=timezone.utc).timestamp() // (minutes * 60))


def run(bars, p):
    res = Result(params=p)
    closed = 0.0
    grid = None
    key = None
    prev_close = None
    blocked_key = None   # session in which a TP/SL fired and rearm is off

    def spread_of(bar):
        if not math.isnan(p.spread):
            return p.spread
        if not math.isnan(bar.spread) and bar.spread > 0:
            return bar.spread
        return p.default_spread

    def finish(basket):
        nonlocal closed
        closed += basket.pnl
        res.baskets.append(basket)
        res.equity.append((basket.end, closed))

    for bar in bars:
        spr = spread_of(bar)
        k = session_key(bar.time, p.session_minutes)

        if grid is not None and k != key and p.session_minutes > 0:
            finish(grid.close(prev_close, spr, bar.time, "session end"))
            grid = None

        if grid is None and blocked_key != k:
            grid = Grid(p, bar.open, spr, bar.time)
            key = k
            path = bar_path(bar)
        elif grid is not None:
            grid.gap_to(bar.open, spr)
            path = bar_path(bar)
            reason = grid.hit(bar.open, spr)
            if reason:
                finish(grid.close(bar.open, spr, bar.time, reason))
                grid = None
                if not p.rearm:
                    blocked_key = k
        else:
            path = None

        if grid is not None:
            for a, b in zip(path, path[1:]):
                if a == b:
                    continue
                out = grid.walk(a, b, spr)
                if out:
                    x, reason = out
                    finish(grid.close(x, spr, bar.time, reason))
                    grid = None
                    if not p.rearm:
                        blocked_key = k
                    break
        prev_close = bar.close

    if grid is not None:
        last = bars[-1]
        finish(grid.close(last.close, spread_of(last), last.time, "end of data"))
    return res


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def summarize(res):
    b = [x for x in res.baskets if x.fills > 0]
    pnl = [x.pnl for x in b]
    wins = [v for v in pnl if v > 0]
    losses = [v for v in pnl if v <= 0]
    peak = dd = eq = 0.0
    for v in pnl:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gross_win, gross_loss = sum(wins), -sum(losses)
    reasons = {}
    for x in b:
        reasons[x.reason] = reasons.get(x.reason, 0) + 1
    return {
        "net": sum(pnl),
        "baskets": len(b),
        "empty": len(res.baskets) - len(b),
        "fills": sum(x.fills for x in b),
        "commission": sum(x.commission for x in b),
        "win_rate": len(wins) / len(b) if b else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "max_dd": dd,
        "worst_basket": min(pnl) if pnl else 0.0,
        "worst_float": min((x.worst for x in b), default=0.0),
        "reasons": reasons,
    }


def print_report(res, bars):
    s = summarize(res)
    p = res.params
    days = max((bars[-1].time - bars[0].time).total_seconds() / 86400, 1e-9)
    print(f"Data: {len(bars)} bars, {bars[0].time} -> {bars[-1].time} ({days:.1f} days)")
    print(f"Grid: {p.levels}+{p.levels} stops, spacing ${p.spacing}, first ${p.first_offset}, "
          f"{p.lot} lot, TP ${p.basket_tp or 'off'}, SL ${p.basket_sl or 'off'}, "
          f"session {p.session_minutes or 'none'} min")
    spread = f"${p.spread}" if not math.isnan(p.spread) else "from file"
    print(f"Costs: spread {spread}, slippage ${p.slippage}, commission ${p.commission}/lot")
    print()
    print(f"  Net P/L          ${s['net']:,.2f}   (${s['net'] / days:,.2f}/day)")
    print(f"  Baskets traded   {s['baskets']}  ({s['empty']} with no fills)")
    print(f"  Orders filled    {s['fills']}  (commission ${s['commission']:,.2f})")
    print(f"  Win rate         {s['win_rate']:.1%}")
    print(f"  Avg win / loss   ${s['avg_win']:,.2f} / ${s['avg_loss']:,.2f}")
    print(f"  Profit factor    {s['profit_factor']:.2f}")
    print(f"  Max drawdown     ${s['max_dd']:,.2f}  (closed baskets)")
    print(f"  Worst basket     ${s['worst_basket']:,.2f}")
    print(f"  Worst floating   ${s['worst_float']:,.2f}  (deepest open loss inside a basket)")
    print(f"  Exits            " + ", ".join(f"{k}: {v}" for k, v in sorted(s["reasons"].items())))


def write_baskets(res, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["start", "end", "fills", "pnl", "commission", "worst_floating", "exit"])
        for b in res.baskets:
            if b.fills == 0:
                continue
            w.writerow([b.start, b.end, b.fills, f"{b.pnl:.2f}", f"{b.commission:.2f}", f"{b.worst:.2f}", b.reason])


def parse_sweep(text):
    grid = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        name, values = part.split("=", 1)
        name = name.strip().replace("-", "_")
        if name not in Params.__dataclass_fields__:
            raise SystemExit(f"--sweep: unknown parameter {name!r}")
        grid[name] = [float(v) for v in values.split(",")]
    return grid


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="bar data (MT5 export or any OHLC CSV)")
    d = Params()
    ap.add_argument("--spacing", type=float, default=d.spacing)
    ap.add_argument("--levels", type=int, default=d.levels)
    ap.add_argument("--first-offset", type=float, default=d.first_offset)
    ap.add_argument("--lot", type=float, default=d.lot)
    ap.add_argument("--contract", type=float, default=d.contract)
    ap.add_argument("--spread", type=float, default=d.spread, help="fixed spread in $ (default: file's column)")
    ap.add_argument("--default-spread", type=float, default=d.default_spread)
    ap.add_argument("--point", type=float, default=0.01, help="price of 1 point, for the file's spread column")
    ap.add_argument("--commission", type=float, default=d.commission, help="$ per lot round turn")
    ap.add_argument("--slippage", type=float, default=d.slippage)
    ap.add_argument("--basket-tp", type=float, default=d.basket_tp)
    ap.add_argument("--basket-sl", type=float, default=d.basket_sl)
    ap.add_argument("--session-minutes", type=int, default=d.session_minutes)
    ap.add_argument("--no-rearm", action="store_true")
    ap.add_argument("--from", dest="start", help="first date to test, e.g. 2025-01-01")
    ap.add_argument("--to", dest="end", help="last date to test")
    ap.add_argument("--sweep", help='e.g. "spacing=0.3,0.5;basket_tp=5,10"')
    ap.add_argument("--out", default="results", help="folder for baskets.csv")
    a = ap.parse_args(argv)

    bars = load_bars(a.csv, a.point)
    if a.start:
        bars = [b for b in bars if b.time >= parse_time(a.start)]
    if a.end:
        bars = [b for b in bars if b.time <= parse_time(a.end)]
    if len(bars) < 2:
        raise SystemExit("not enough bars in range")

    p = Params(spacing=a.spacing, levels=a.levels, first_offset=a.first_offset, lot=a.lot,
               contract=a.contract, spread=a.spread, default_spread=a.default_spread,
               commission=a.commission, slippage=a.slippage, basket_tp=a.basket_tp,
               basket_sl=a.basket_sl, session_minutes=a.session_minutes, rearm=not a.no_rearm)

    if a.sweep:
        space = parse_sweep(a.sweep)
        names = list(space)
        rows = []
        for combo in itertools.product(*(space[n] for n in names)):
            kw = {n: (int(v) if n in ("levels", "session_minutes") else v) for n, v in zip(names, combo)}
            s = summarize(run(bars, replace(p, **kw)))
            rows.append((kw, s))
        rows.sort(key=lambda r: r[1]["net"], reverse=True)
        print(f"{len(rows)} runs on {len(bars)} bars, best first. Best-on-past-data is not a forecast.\n")
        print(f"{'params':<45} {'net $':>10} {'fills':>7} {'win%':>6} {'PF':>6} {'maxDD $':>9}")
        for kw, s in rows:
            label = " ".join(f"{k}={v}" for k, v in kw.items())
            print(f"{label:<45} {s['net']:>10,.2f} {s['fills']:>7} {s['win_rate']:>6.1%} "
                  f"{s['profit_factor']:>6.2f} {s['max_dd']:>9,.2f}")
        return

    res = run(bars, p)
    print_report(res, bars)
    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, "baskets.csv")
    write_baskets(res, out)
    print(f"\nPer-basket log: {out}")


if __name__ == "__main__":
    main()
