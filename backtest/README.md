# Gold stop-grid backtest

Tests the "straddle grid" from the XAUUSD reel: buy-stops stacked above price, sell-stops stacked below, closed as one basket.

## Strategy rules (what is simulated)

1. **Session start** (default: every hour, `--session-minutes 60`): at the bar's open, place `--levels` buy-stops above the ask and `--levels` sell-stops below the bid. First order `--first-offset` away, then every `--spacing` dollars. Each order `--lot` (0.01 lot = 1 oz → $1 per $1 move).
2. **Fills**: price crossing a stop fills it (plus `--slippage`). A trend fills one side progressively — position grows with the move. Chop fills both sides — you hold longs and shorts together and pay spread on each.
3. **Exit** the whole basket when floating P/L ≥ `--basket-tp` dollars, ≤ −`--basket-sl` dollars, or the session ends. Unfilled orders cancelled.
4. **Re-arm**: after a TP/SL, a new grid goes on at the next bar (`--no-rearm` waits for the next session).

The reel shows no exit rules, so TP/SL/session are choices you test, not facts about the original.

## Get the data (MT5)

1. MT5 desktop → **View → Symbols** (Ctrl+U) → **Bars** tab.
2. Symbol `XAUUSD.ecn` (your broker's name), timeframe **M1**, date range (6–12 months+).
3. **Request**, then **Export Bars** → saves a `.csv`.

Use **M1**. The engine guesses the path inside each bar (open→low→high→close for up bars); on H1 bars that guess decides the result, on M1 it barely matters. The file's `<SPREAD>` column (in points) is used for spread; gold point = `0.01` (`--point`).

## Run

```bash
cd backtest
python3 grid_backtest.py XAUUSD_M1.csv                                   # reel defaults, no TP/SL, hourly reset
python3 grid_backtest.py XAUUSD_M1.csv --basket-tp 10 --basket-sl 20 --commission 7
python3 grid_backtest.py XAUUSD_M1.csv --from 2025-01-01 --to 2025-06-30
python3 grid_backtest.py XAUUSD_M1.csv --sweep "spacing=0.3,0.5,1;basket_tp=5,10,20;basket_sl=10,20,40"
```

Output: net P/L, fills, win rate, profit factor, max drawdown, worst basket, and `results/baskets.csv` (one row per basket).

Set `--commission` to your broker's $ per lot round turn (ECN gold often $5–7). Without it results look better than reality.

## Reading results honestly

- **Profit factor < 1.0** = loses money. Below ~1.3 after costs = not worth the risk.
- **Sweep overfits.** The best row on past data usually fails going forward. Pick parameters on one period (`--to`), confirm on a later one (`--from`) you never tuned on.
- **Max drawdown vs account size**: drawdown you can't sit through = strategy you'll abandon at the worst time.
- Not simulated: broker min stop distance, max order count, margin calls, news spikes beyond M1 detail, swap for overnight holds.

## Tests

```bash
python3 -m unittest discover -s tests
```
