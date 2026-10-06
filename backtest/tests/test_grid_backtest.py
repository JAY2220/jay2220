import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_backtest as gb  # noqa: E402

T0 = datetime(2025, 7, 20, 15, 0)


def bar(i, o, h, l, c):
    return gb.Bar(T0 + timedelta(minutes=i), o, h, l, c, float("nan"))


def params(**kw):
    base = dict(spacing=0.3, levels=10, first_offset=0.3, spread=0.0, slippage=0.0)
    base.update(kw)
    return gb.Params(**base)


class Engine(unittest.TestCase):
    def test_trend_fills_whole_side(self):
        res = gb.run([bar(0, 100, 105, 100, 105)], params())
        b = res.baskets[0]
        self.assertEqual(b.fills, 10)
        # sum of (105 - entry) for entries 100.3 .. 103.0, 1 oz each
        self.assertAlmostEqual(b.pnl, 33.5, places=6)

    def test_basket_tp_exits_between_fills(self):
        res = gb.run([bar(0, 100, 105, 100, 105)], params(basket_tp=1.0))
        b = res.baskets[0]
        self.assertEqual((b.fills, b.reason), (3, "tp"))
        self.assertAlmostEqual(b.pnl, 1.0, places=6)

    def test_whipsaw_loses(self):
        res = gb.run([bar(0, 100, 100.65, 99.35, 99.35)], params())
        b = res.baskets[0]
        self.assertEqual(b.fills, 4)
        self.assertAlmostEqual(b.pnl, -1.8, places=6)

    def test_costs_reduce_pnl(self):
        clean = gb.run([bar(0, 100, 105, 100, 105)], params()).baskets[0].pnl
        costly = gb.run([bar(0, 100, 105, 100, 105)],
                        params(spread=0.1, slippage=0.02, commission=7.0)).baskets[0]
        # each of 10 fills pays 0.10 spread (grid sits above the ask) + 0.02 slippage + 0.07 commission
        self.assertAlmostEqual(costly.pnl, clean - 10 * (0.10 + 0.02 + 0.07), places=6)

    def test_basket_sl_and_session_reset(self):
        bars = [bar(0, 100, 100.4, 100, 100.4), bar(1, 100.4, 100.4, 98, 98)]
        res = gb.run(bars, params(basket_sl=0.5, session_minutes=60))
        self.assertEqual(res.baskets[0].reason, "sl")
        self.assertAlmostEqual(res.baskets[0].pnl, -0.5, places=6)
        # rearmed on the next bar? only one bar left after, and the grid there is new
        hourly = gb.run([bar(0, 100, 100.4, 100, 100.4), bar(61, 100, 100.4, 100, 100.4)], params())
        self.assertEqual(len(hourly.baskets), 2)
        self.assertEqual(hourly.baskets[0].reason, "session end")


class Loader(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write(text)
        try:
            return gb.load_bars(fh.name, 0.01)
        finally:
            os.unlink(fh.name)

    def test_mt5_export(self):
        bars = self.load("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                         "2025.07.20\t15:35:00\t4008.82\t4010.00\t4007.00\t4009.50\t120\t0\t8\n")
        self.assertEqual(bars[0].time, datetime(2025, 7, 20, 15, 35))
        self.assertAlmostEqual(bars[0].spread, 0.08)
        self.assertEqual(bars[0].close, 4009.50)

    def test_headerless_mt4(self):
        bars = self.load("2025.07.20,15:35,4008.82,4010.00,4007.00,4009.50,120\n")
        self.assertEqual(bars[0].open, 4008.82)

    def test_generic_header(self):
        bars = self.load("Datetime,Open,High,Low,Close,Volume\n2025-07-20 15:35:00,1,2,0.5,1.5,10\n")
        self.assertEqual((bars[0].high, bars[0].low), (2.0, 0.5))


if __name__ == "__main__":
    unittest.main()
