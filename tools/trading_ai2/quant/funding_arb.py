"""funding_arb.py — арбитраж ставки финансирования.

Перпетуал с funding > threshold (0.03% за 8ч): лонг спот + шорт перп
равного объёма → дельта-нейтрально, собираем фандинг каждые 8ч.
Негативный funding — зеркально (шорт спот / лонг перп) — не делаем:
шорт-спот недоступен на спотовом testnet, честно не торгуем вниз.
"""

from __future__ import annotations

FUNDING_THRESHOLD = 0.0003        # 0.03% за 8ч
PAYOUTS_PER_DAY = 3               # каждые 8 часов


def fetch_funding(ex, perp_symbol: str) -> dict:
    """ccxt.fetch_funding_rate → {rate, next_ts}."""
    fr = ex.fetch_funding_rate(perp_symbol)
    return {"rate": float(fr.get("fundingRate") or 0.0),
            "next": fr.get("fundingTimestamp"),
            "symbol": perp_symbol}


def funding_signal(rate: float,
                   threshold: float = FUNDING_THRESHOLD) -> int:
    """+1 → лонг спот + шорт перп (собираем положительный фандинг)."""
    return 1 if rate > threshold else 0


def expected_yield_apy(rate: float) -> float:
    """Годовая доходность фандинга (простая, без сложного процента)."""
    return rate * PAYOUTS_PER_DAY * 365 * 100.0   # %


def fetch_funding_history(ex, perp_symbol: str, days: int = 730):
    """Публичная история funding rate Binance Futures (~3 записи/день)."""
    import time
    since = ex.milliseconds() - days * 86_400_000
    out = []
    while True:
        batch = ex.fetch_funding_rate_history(
            perp_symbol, since=since, limit=1000)
        if not batch:
            break
        out += batch
        since = int(batch[-1]["timestamp"]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.3)
    return [(int(r["timestamp"]), float(r["fundingRate"])) for r in out]


def funding_backtest(history: list[tuple[int, float]],
                     equity0: float = 10_000.0,
                     notional_frac: float = 0.5,
                     cost_roundtrip: float = 0.0048,
                     leverage: float = 1.0,
                     min_rate: float = 0.0) -> dict:
    """Дельта-нейтральная позиция на всём периоде: equity растёт на
    Σ(funding-выплаты × плечо) − издержки. Плечо умножает и доход,
    и просадку; собираем И платим funding — честно."""
    import numpy as np
    eq, curve = equity0 - equity0 * cost_roundtrip, []
    collected = payouts = neg = 0.0
    for ts, rate in history:
        collected += rate * equity0 * notional_frac * leverage
        payouts += 1
        neg += 1 if rate < 0 else 0
        eq = equity0 * (1 - cost_roundtrip) + collected
        curve.append(eq)
    curve = np.array(curve)
    rets = np.diff(curve) / curve[:-1]
    rets = rets[np.isfinite(rets)]
    # ~3 выплаты/день → annualization √1095
    sharpe = (rets.mean() / rets.std() * np.sqrt(1095)
              if rets.std() > 0 else 0.0)
    dd = (np.maximum.accumulate(curve) - curve).max() / equity0 * 100
    days = (history[-1][0] - history[0][0]) / 86_400_000 if history else 1
    ret_pct = (curve[-1] / equity0 - 1) * 100
    # Kelly-оптимум плеча: f* = mean(r)/var(r) по выплатам
    pay_rets = np.array([r_ for _, r_ in history])
    kelly_lev = (pay_rets.mean() / pay_rets.var()
                 if pay_rets.var() > 0 else 0.0)
    max_safe = 1.0 / (cost_roundtrip + 1e-9) * 100
    return {"sharpe": round(float(sharpe), 2),
            "negative_payouts": int(neg),
            "leverage": leverage,
            "kelly_leverage": round(float(max(0.0, kelly_lev)), 1),
            "max_dd_pct": round(float(dd), 3),
            "return_pct": round(ret_pct, 2),
            "apy_pct": round(ret_pct / days * 365, 2),
            "payouts": payouts, "days": round(days, 1),
            "final_equity": round(float(curve[-1]), 2)}


if __name__ == "__main__":
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True})
    try:
        for sym in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
            try:
                h = fetch_funding_history(ex, sym, days=730)
                for lev in (1, 3, 5):
                    r = funding_backtest(h, leverage=lev)
                    print(f"{sym:16} lev={lev}x days={r['days']} "
                          f"payouts={r['payouts']}(-{r['negative_payouts']}) "
                          f"ret={r['return_pct']}% apy={r['apy_pct']}% "
                          f"Sharpe={r['sharpe']} MaxDD={r['max_dd_pct']}% "
                          f"kellyLev={r['kelly_leverage']}x")
            except Exception as e:  # noqa: BLE001
                print(f"{sym:16} ERROR {e}")
    finally:
        ex.close()
