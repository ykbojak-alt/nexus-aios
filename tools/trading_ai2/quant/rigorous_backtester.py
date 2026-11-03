"""rigorous_backtester.py — строгий бэктест pairs-стратегии.

2 года 1h-свечей (кэш в CSV). Учитывает: taker-фи 0.1% на ногу,
slippage 0.02% на ногу → ~0.48% за round-trip пары. Метрики:
Sharpe (годовой, √(24·365)), Max Drawdown, winrate, Kelly-размер.

  python -m tools.trading_ai2.quant.rigorous_backtester \
      --sym-a ETH/USDT --sym-b BTC/USDT --days 730 --timeframe 1h
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import numpy as np

from . import pairs_engine as pe
from .kelly_sizing import kelly_fraction, expectancy_report

CACHE = Path(__file__).resolve().parent / "cache"
TAKER_FEE = 0.001                 # 0.1% за ногу (Binance taker)
SLIPPAGE = 0.0002                 # 0.02% за ногу
COST_PER_LEG = TAKER_FEE + SLIPPAGE


def load_closes(ex, symbol: str, timeframe: str, days: int) -> np.ndarray:
    """OHLCV closes за N дней, кэш CSV — повтор без сети."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{symbol.replace('/', '')}_{timeframe}_{days}d.csv"
    if f.exists():
        return np.loadtxt(f, delimiter=",")
    limit = 1000
    ms_per_bar = {"1h": 3_600_000, "4h": 14_400_000,
                  "15m": 900_000, "1d": 86_400_000}[timeframe]
    since = ex.milliseconds() - days * 86_400_000
    out: list[list] = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since,
                               limit=limit)
        if not batch:
            break
        out += batch
        since = batch[-1][0] + ms_per_bar
        if len(batch) < limit:
            break
        time.sleep(0.3)
    closes = np.array([r[4] for r in out], dtype=float)
    with open(f, "w", newline="") as fh:
        csv.writer(fh).writerows([[c] for c in closes])
    return closes


def backtest(closes_a: np.ndarray, closes_b: np.ndarray,
             window: int = 48, equity0: float = 10_000.0,
             kelly: bool = True) -> dict:
    """Event-loop: одна пара-позиция за раз, market-neutral."""
    n = min(len(closes_a), len(closes_b))
    a, b = closes_a[:n], closes_b[:n]
    beta = pe.hedge_ratio(a[: max(window * 2, 200)],
                        b[: max(window * 2, 200)])
    spread = pe.spread_series(a, b, beta)
    z = pe.zscore(spread, window)

    equity, peak, pos = equity0, equity0, 0      # pos: ±1
    entry_a = entry_b = qty_a = qty_b = 0.0
    curve, trades = [], []
    for i in range(n):
        sig = pe.signal(z[i] if not np.isnan(z[i]) else 0.0,
                        in_position=pos != 0, side=pos)
        if pos == 0 and sig != 0:
            # размер ноги: Kelly на статистике уже закрытых сделок;
            # <3 сделок → консервативные 1% equity
            if kelly and len(trades) >= 3:
                w = [t for t in trades if t > 0]
                l = [-t for t in trades if t <= 0]
                pct = kelly_fraction(len(w) / len(trades),
                                     float(np.mean(w)) if w else 1,
                                     float(np.mean(l)) if l else 1)
            else:
                pct = 1.0
            size = equity * max(0.5, pct) / 100.0
            qty_a = size / a[i] * sig          # +long A / −short A
            qty_b = -size / b[i] * sig         # +long B / −short B
            entry_a, entry_b, pos = a[i], b[i], sig
            equity -= size * COST_PER_LEG * 2  # 2 ноги на входе
        elif pos != 0 and sig == 0:
            size = abs(qty_a) * entry_a
            pnl = qty_a * (a[i] - entry_a) + qty_b * (b[i] - entry_b)
            equity += pnl - size * COST_PER_LEG * 2
            trades.append(pnl)
            pos, qty_a, qty_b = 0, 0.0, 0.0
        mtm = (qty_a * (a[i] - entry_a) + qty_b * (b[i] - entry_b)
               if pos != 0 else 0.0)
        eq_now = equity + mtm
        peak = max(peak, eq_now)
        curve.append(eq_now)
    curve = np.array(curve)
    rets = np.diff(curve) / curve[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = (rets.mean() / rets.std() * np.sqrt(24 * 365)
              if rets.std() > 0 else 0.0)
    dd = (np.maximum.accumulate(curve) - curve).max() / equity0 * 100
    wins = [t for t in trades if t > 0]
    losses = [-t for t in trades if t <= 0]
    return {"sharpe": round(float(sharpe), 2),
            "max_dd_pct": round(float(dd), 2),
            "trades": len(trades),
            "winrate": round(len(wins) / len(trades), 3) if trades else 0,
            "final_equity": round(float(curve[-1]), 2),
            "return_pct": round((curve[-1] / equity0 - 1) * 100, 2),
            "cointegrated": pe.is_cointegrated(spread),
            "half_life_h": round(pe.half_life(spread), 1),
            "beta": round(beta, 4),
            "kelly_pct": kelly_fraction(
                len(wins) / len(trades) if trades else 0,
                float(np.mean(wins)) if wins else 0,
                float(np.mean(losses)) if losses else 1),
            "expectancy": expectancy_report(
                len(wins) / len(trades) if trades else 0,
                float(np.mean(wins)) if wins else 0,
                float(np.mean(losses)) if losses else 1)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sym-a", default="ETH/USDT")
    p.add_argument("--sym-b", default="BTC/USDT")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--window", type=int, default=48)
    a = p.parse_args()

    import ccxt
    ex = ccxt.binance({"enableRateLimit": True})
    try:
        print(f"[data] {a.sym_a} vs {a.sym_b} {a.timeframe} ×{a.days}d")
        ca = load_closes(ex, a.sym_a, a.timeframe, a.days)
        cb = load_closes(ex, a.sym_b, a.timeframe, a.days)
        r = backtest(ca, cb, window=a.window)
        print(f"[bt] cointegrated={r['cointegrated']} "
              f"half-life={r['half_life_h']}h beta={r['beta']}")
        print(f"[bt] trades={r['trades']} winrate={r['winrate']} "
              f"return={r['return_pct']}% final={r['final_equity']}")
        print(f"[bt] Sharpe={r['sharpe']} MaxDD={r['max_dd_pct']}%")
        print(f"[kelly] size={r['kelly_pct']}% expectancy={r['expectancy']}")
    finally:
        ex.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
