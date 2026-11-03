"""correlation_checker.py — защита портфеля от системного риска.

Два актива с корреляцией цен > 0.8 за последние 24 часа — это по сути
одна позиция в двух обёртках: просядут вместе. `filter_uncorrelated`
оставляет из коррелирующей группы только самый горячий (наивысший score
— список приходит уже отсортированным скринером).
"""

from __future__ import annotations

import pandas as pd

from .market_screener import _exchange

CORR_THRESHOLD = 0.8


def _closes(rows, limit: int) -> list[float]:
    return [float(r[4]) for r in rows][-limit:]


def filter_uncorrelated(assets: list[dict], *,
                        threshold: float = CORR_THRESHOLD,
                        timeframe: str = "1h", limit: int = 24,
                        ohlcv=None) -> list[dict]:
    """Жадный фильтр: актив остаётся, если |corr| ≤ threshold со всеми
    уже оставленными. Актив без данных оставляем — невозможность
    проверить корреляцию не повод выбрасывать кандидата."""
    if len(assets) < 2:
        return list(assets)
    fetch = ohlcv or _exchange().fetch_ohlcv
    closes: dict[str, list[float]] = {}
    for a in assets:
        sym = a["symbol"]
        try:
            closes[sym] = _closes(fetch(sym, timeframe, limit=limit), limit)
        except Exception:                       # noqa: BLE001
            pass
    kept: list[dict] = []
    for a in assets:
        a_close = closes.get(a["symbol"])
        if a_close is None:
            kept.append(a)
            continue
        ok = True
        for k in kept:
            k_close = closes.get(k["symbol"])
            if k_close is None:
                continue
            n = min(len(a_close), len(k_close))
            if n < 3:
                continue
            corr = pd.Series(a_close[-n:]).corr(pd.Series(k_close[-n:]))
            # знаковая корреляция: >0.8 — падают вместе (риск), а
            # обратная корреляция — хедж, её портфелю резать нельзя
            if pd.notna(corr) and corr > threshold:
                ok = False
                break
        if ok:
            kept.append(a)
    return kept
