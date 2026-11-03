"""market_screener.py — «Слой 0»: мультивалютный скринер перед циклом.

Для каждого тикера из SCREEN_TICKERS (.env, csv) тянет 15m-свечи
(ccxt, binance public — ключей не надо) и ранжирует по жару рынка:

    volatility    = (high - low) / close * 100   последней закрытой свечи
    volume_ratio  = vol_last / SMA20(volume)

«Горячий» актив: volatility > 1.5% и volume_ratio > 1.0. `get_top_assets(n)`
отдаёт N лучших (горячие первыми; горячих нет, но рынок не мёртв — наименее
скучные с hot=False). Если все активы во флэте (vol < 1.0%) — пустой список:
цикл ставится на паузу и LLM-лимиты не жгутся.
"""

from __future__ import annotations

import atexit
import time
from statistics import mean

import ccxt

from ...market_data import (
    _is_binance_geo_or_unavailable, fetch_binance_vision_exchange_info,
    fetch_binance_vision_ohlcv)
from ..llm_factory import env

DEFAULT_TICKERS = ("BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,"
                   "XRP/USDT,AVAX/USDT,LINK/USDT")

MIN_VOLATILITY = 1.5          # % — порог «горячего» актива
MIN_VOLUME_RATIO = 1.0        # объём выше среднего — движение настоящее
FLAT_VOLATILITY = 1.0         # % — все ниже → рынок плоский, цикл спит
SCAN_TOP = 40                 # кандидатов фазы B при скане всего рынка
MIN_QUOTE_VOL = 5_000_000     # USDT/24ч — ниже это мусорные пары
DISCOVERY_TTL_S = 300         # рынок пересматриваем раз в 5 минут
#: Стейблкоины и фиат — вечный флэт, слоты скана жгут зря.
_STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USD1",
                 "EUR", "GBP", "BRL", "TRY", "UAH", "RUB", "ARS",
                 "BUSD", "USDD", "PYUSD", "XUSD", "BFUSD", "AEUR"}

_EX = None                    # ленивый синглтон ccxt-шлюза
_DISCOVERY_CACHE: tuple[float, list[str]] = (0.0, [])


class MarketDataUnavailable(RuntimeError):
    """Every requested market-data feed failed; this is not a flat market."""


def _exchange():
    """Binance public для скрининга (отдельный от MarketData-фида,
    чтобы не трогать его symbol вне цикла)."""
    global _EX
    if _EX is None:
        _EX = ccxt.binance({"enableRateLimit": True})
    return _EX


def close_exchange() -> None:
    """Close and forget the process-wide ccxt market-scanner session."""
    global _EX
    exchange, _EX = _EX, None
    if exchange is not None:
        exchange.close()


atexit.register(close_exchange)


def screen_tickers() -> list[str]:
    """Тикеры из .env (SCREEN_TICKERS, csv) или дефолт.

    «all»/«auto»/«*» — весь рынок: discover_tickers() отдаёт
    топ-кандидатов по 24ч-статистике, свечи тянутся только для них.
    """
    raw = env("SCREEN_TICKERS").strip() or DEFAULT_TICKERS
    if raw.lower() in ("all", "auto", "*"):
        return discover_tickers()
    return [t.strip() for t in raw.split(",") if t.strip()]


def discover_tickers(*, scan_top: int | None = None,
                     min_quote_vol: float | None = None,
                     ex=None) -> list[str]:
    """Весь спот Binance ×USDT: топ по обороту + топ-движущиеся за 24ч.

    Двухфазный скан: fetch_tickers() — ОДИН запрос на весь рынок
    (24h-статистика по каждой паре), а 15m-свечи score_assets() тянет
    только для отобранных кандидатов — иначе 400+ запросов на цикл.
    Кэш на DISCOVERY_TTL_S — список рынка пересматривается раз в 5 мин.
    """
    global _DISCOVERY_CACHE
    ts, cached = _DISCOVERY_CACHE
    if ex is None and cached and time.time() - ts < DISCOVERY_TTL_S:
        return list(cached)
    top = scan_top or int(env("SCREEN_SCAN_TOP") or SCAN_TOP)
    min_qv = (min_quote_vol if min_quote_vol is not None
              else float(env("SCREEN_MIN_QV") or MIN_QUOTE_VOL))
    ex = ex or _exchange()
    fallback = [t.strip() for t in DEFAULT_TICKERS.split(",")]
    try:
        markets = {s for s, m in ex.load_markets().items()
                   if m.get("spot") and m.get("quote") == "USDT"
                   and m.get("active", True)}
    except Exception:                               # noqa: BLE001
        markets = set()
    try:
        tickers = ex.fetch_tickers()
    except Exception:                               # noqa: BLE001
        try:
            info = fetch_binance_vision_exchange_info()
            available = {
                item.get("symbol") for item in info.get("symbols", [])
                if item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed", True)
            }
            # exchangeInfo имеет рынок, но не 24h-оборот. Используем
            # известные ликвидные пары как ограниченный candidate pool.
            return [sym for sym in fallback
                    if sym.replace("/", "") in available] or fallback
        except Exception:                           # noqa: BLE001
            return fallback                         # последний статический пул
    rows: list[tuple[str, float, float]] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT") or (markets and sym not in markets):
            continue
        if sym.split("/")[0].upper() in _STABLE_BASES:
            continue                              # стейбл-пары — вечный флэт
        qv = float(t.get("quoteVolume") or 0.0)
        chg = abs(float(t.get("percentage") or 0.0))
        if qv >= min_qv:
            rows.append((sym, qv, chg))
    if not rows:
        return fallback
    # половина пула — ликвидные (оборот), половина — движущиеся (|Δ24h|)
    liquid = sorted(rows, key=lambda r: r[1], reverse=True)
    movers = sorted(rows, key=lambda r: r[2], reverse=True)
    out, seen = [], set()
    for sym, _, _ in liquid[: top // 2] + movers[: top]:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
            if len(out) >= top:
                break
    _DISCOVERY_CACHE = (time.time(), out)
    return out


def score_assets(*, tickers: list[str] | None = None,
                 timeframe: str = "15m", limit: int = 30,
                 ohlcv=None) -> list[dict]:
    """Метрики всех активов, отсортированные по score (vol × vratio) убыв."""
    fetch = ohlcv or _exchange().fetch_ohlcv
    results: list[dict] = []
    symbols = tickers or screen_tickers()
    failed = 0
    for sym in symbols:
        try:
            rows = fetch(sym, timeframe, limit=limit)
        except Exception as exc:                # noqa: BLE001
            if not _is_binance_geo_or_unavailable(exc):
                failed += 1
                continue
            try:
                rows = fetch_binance_vision_ohlcv(sym, timeframe, limit)
            except Exception:                   # noqa: BLE001
                failed += 1
                continue
        if not rows or len(rows) < 3:
            continue
        last = rows[-2] if len(rows) >= 2 else rows[-1]   # закрытая свеча
        close = float(last[4])
        if close <= 0:
            continue
        volatility = (float(last[2]) - float(last[3])) / close * 100.0
        vols = [float(r[5]) for r in rows[:-1] if len(r) > 5]
        sma_vol = mean(vols[-20:]) if vols else 0.0
        volume_ratio = (float(last[5]) / sma_vol) if sma_vol > 0 else 0.0
        results.append({
            "symbol": sym, "volatility": volatility, "price": close,
            "volume_ratio": volume_ratio,
            "score": volatility * max(volume_ratio, 0.0),
        })
    if symbols and failed == len(symbols):
        raise MarketDataUnavailable(
            f"all {len(symbols)} market-data requests failed"
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def get_top_assets(n: int = 3, *, tickers: list[str] | None = None,
                   timeframe: str = "15m", limit: int = 30,
                   min_volatility: float = MIN_VOLATILITY,
                   min_volume_ratio: float = MIN_VOLUME_RATIO,
                   flat_volatility: float = FLAT_VOLATILITY,
                   ohlcv=None) -> list[dict]:
    """N лучших активов; [] — если все во флэте или данных нет."""
    if n <= 0:
        return []
    results = score_assets(tickers=tickers, timeframe=timeframe,
                           limit=limit, ohlcv=ohlcv)
    if not results:
        return []
    if all(r["volatility"] < flat_volatility for r in results):
        return []                               # рынок спит — цикл спит
    hot = [r for r in results if r["volatility"] > min_volatility
           and r["volume_ratio"] > min_volume_ratio]
    pool = hot if hot else results
    out = [{**r, "hot": r in hot} for r in pool[:n]]
    return out


def get_best_asset(*, tickers: list[str] | None = None,
                   timeframe: str = "15m", limit: int = 30,
                   min_volatility: float = MIN_VOLATILITY,
                   min_volume_ratio: float = MIN_VOLUME_RATIO,
                   flat_volatility: float = FLAT_VOLATILITY,
                   ohlcv=None) -> dict | None:
    """Лучший актив или None — обёртка над get_top_assets(1)."""
    top = get_top_assets(1, tickers=tickers, timeframe=timeframe,
                         limit=limit, min_volatility=min_volatility,
                         min_volume_ratio=min_volume_ratio,
                         flat_volatility=flat_volatility, ohlcv=ohlcv)
    return top[0] if top else None
