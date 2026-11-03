"""kelly_sizing.py — Критерий Келли для размера позиции.

f* = W − (1−W)/R, где W — винрейт, R — payoff (ср. выигрыш/ср. проигрыш).
Полный Келли агрессивен → используем fraction (0.5 = half-Kelly),
и жёсткий кап max_pct — Келли не отменяет Risk Manager.
"""

from __future__ import annotations


def kelly_fraction(winrate: float, avg_win: float, avg_loss: float,
                   fraction: float = 0.5, max_pct: float = 2.0) -> float:
    """Возвращает % депозита на сделку. 0 при отрицательном матожидании."""
    if winrate <= 0 or avg_win <= 0 or avg_loss <= 0:
        return 0.0
    payoff = avg_win / avg_loss               # R
    expectancy = winrate * payoff - (1.0 - winrate)
    if expectancy <= 0:
        return 0.0                            # EV ≤ 0 — не торгуем
    f = winrate - (1.0 - winrate) / payoff    # полный Келли
    return round(min(max_pct, max(0.0, f * fraction * 100.0)), 3)


def expectancy_report(winrate: float, avg_win: float,
                      avg_loss: float) -> dict:
    payoff = avg_win / avg_loss if avg_loss > 0 else float("inf")
    ev = winrate * avg_win - (1 - winrate) * avg_loss
    return {"winrate": winrate, "payoff": round(payoff, 3),
            "ev_per_trade": round(ev, 4)}
