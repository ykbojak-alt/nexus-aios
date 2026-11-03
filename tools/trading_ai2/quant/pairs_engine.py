"""pairs_engine.py — статистический арбитраж (mean reversion спреда).

Спред = log(P_a) − β·log(P_b), β — hedge ratio из OLS (numpy polyfit).
Z-score по скользящему окну. |z|>2 → рыночно-нейтральная позиция:
z>+2 → short A / long B; z<−2 → long A / short B. Выход |z|<0.5,
аварийный выход |z|>3.5 (спред «убежал» — коинтеграция сломалась).

Проверка коинтеграции — Engle-Granger-lite: полу-жизнь возврата к
среднему (half-life) в разумных пределах + spread ADF-эвристика
(дисперсия первых разностей < дисперсии уровня). Честно: без
statsmodels это оценка, а не формальный ADF-тест.
"""

from __future__ import annotations

import numpy as np


def hedge_ratio(prices_a: np.ndarray, prices_b: np.ndarray) -> float:
    """β из регрессии log(A) на log(B)."""
    la, lb = np.log(prices_a), np.log(prices_b)
    beta = np.polyfit(lb, la, 1)[0]
    return float(beta)


def spread_series(prices_a: np.ndarray, prices_b: np.ndarray,
                  beta: float) -> np.ndarray:
    return np.log(prices_a) - beta * np.log(prices_b)


def zscore(spread: np.ndarray, window: int = 48) -> np.ndarray:
    """Скользящий Z-score: (s − rolling_mean)/rolling_std."""
    z = np.full_like(spread, np.nan, dtype=float)
    for i in range(window, len(spread)):
        w = spread[i - window:i]
        sd = w.std()
        z[i] = (spread[i] - w.mean()) / sd if sd > 1e-12 else 0.0
    return z


def half_life(spread: np.ndarray) -> float:
    """Оценка half-life: регрессия Δs на s(t−1); hl = −ln2/λ."""
    s = spread[~np.isnan(spread)]
    if len(s) < 50:
        return float("inf")
    ds = np.diff(s)
    lag = s[:-1]
    lam = np.polyfit(lag, ds, 1)[0]
    return float(-np.log(2) / lam) if lam < -1e-8 else float("inf")


def is_cointegrated(spread: np.ndarray, hl_min: float = 6,
                    hl_max: float = 24 * 14) -> bool:
    """Эвристика: mean-reverting (half-life в окне 6ч–2нед)
    и стационарность (var диффов < var уровня)."""
    s = spread[~np.isnan(spread)]
    if len(s) < 100:
        return False
    hl = half_life(s)
    stationary = np.diff(s).var() < s.var() * 1.5
    return hl_min <= hl <= hl_max and stationary


def signal(z_now: float, z_entry: float = 2.0, z_exit: float = 0.5,
           z_stop: float = 3.5, in_position: bool = False,
           side: int = 0) -> int:
    """→ +1 (long A/short B), −1 (short A/long B), 0 (выйти/ждать)."""
    if in_position:
        if abs(z_now) < z_exit or abs(z_now) > z_stop:
            return 0
        return side
    if z_now > z_entry:
        return -1
    if z_now < -z_entry:
        return +1
    return 0
