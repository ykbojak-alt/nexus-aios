"""portfolio_manager.py — ребалансировка портфеля позиций MarginPad.

Сравнивает открытые позиции с топом скринера: выпавший из топа актив
помечается на закрытие, новый сильный — на открытие. Лимиты:
максимум 3 позиции, суммарная маржа ≤ 20% equity, на сделку ≤ 5% equity.
"""

from __future__ import annotations

MAX_POSITIONS = 3
MAX_TOTAL_MARGIN_PCT = 20.0     # % equity — потолок суммарной маржи
MAX_TRADE_PCT = 5.0             # % equity — потолок маржи на сделку


def _base(symbol: str) -> str:
    return str(symbol).split("/")[0]


def decide_rebalance(positions: list[dict], top_assets: list[dict], *,
                     equity_usd: float,
                     max_positions: int = MAX_POSITIONS,
                     max_total_margin_pct: float = MAX_TOTAL_MARGIN_PCT,
                     max_trade_pct: float = MAX_TRADE_PCT) -> dict:
    """План ребалансировки.

    positions — открытые позиции MarginPad (symbol, margin_usd, id, side).
    top_assets — отсортированный список скринера (symbol 'SOL/USDT'…).
    Возвращает {"close": [positions…], "open": [{"symbol","margin"}…],
                "margin_used", "budget", "equity_usd"}.
    """
    open_by_sym: dict[str, dict] = {}
    for p in positions or []:
        open_by_sym.setdefault(_base(p.get("symbol", "")), p)
    top_syms = [_base(a["symbol"]) for a in top_assets or []]

    to_close = [p for sym, p in open_by_sym.items()
                if sym not in top_syms]
    closing = {_base(p.get("symbol", "")) for p in to_close}
    staying = [p for sym, p in open_by_sym.items()
               if sym not in closing]

    budget = equity_usd * max_total_margin_pct / 100.0
    margin_used = sum(float(p.get("margin_usd") or 0) for p in staying)
    per_trade_cap = equity_usd * max_trade_pct / 100.0
    slots = max(0, max_positions - len(staying))

    to_open: list[dict] = []
    for a in top_assets or []:
        if slots <= 0 or margin_used >= budget:
            break
        sym = _base(a["symbol"])
        if sym in open_by_sym and sym not in closing:
            continue                            # уже держим — ок
        margin = min(per_trade_cap, budget - margin_used)
        if margin <= 0:
            break
        to_open.append({"symbol": a["symbol"], "margin": round(margin, 2),
                        "volatility": a.get("volatility"),
                        "hot": a.get("hot", False)})
        margin_used += margin
        slots -= 1
    return {"close": to_close, "open": to_open,
            "margin_used": round(margin_used, 2),
            "budget": round(budget, 2), "equity_usd": equity_usd}
