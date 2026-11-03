"""market_data.py — рыночный фид: OHLCV через ccxt + индикаторы.

Публичные данные (свечи, тикер) не требуют ключей и идут с основного
api.binance.com — ключи нужны только исполнению (executor), и то на
testnet. Индикаторы считаются на чистом pandas (ewm): pandas-ta
опционален — его numba не собирается на Python 3.15. Если pandas-ta
установлен — используется он; иначе встроенный расчёт, математика та же.
"""

from __future__ import annotations

import json
import urllib.error
import weakref
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ccxt
import pandas as pd

try:
    import pandas_ta as ta  # noqa: F401 — опционально, есть фолбэк ниже
    _HAS_PANDAS_TA = True
except ImportError:
    _HAS_PANDAS_TA = False


def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI Уайлдера: среднее роста/падения по ewm(alpha=1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss != 0.0, 100.0)   # только рост → RSI 100
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    return rsi.where(~flat, 50.0)             # флэт → нейтральные 50


def _macd(close: pd.Series, fast: int = 12, slow: int = 26,
          signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd = _ema(close, fast) - _ema(close, slow)
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR Уайлдера: средний true range. Нужен для правила «ATR выросла
    больше чем на 50% за час» из базы знаний.

    Без колонок high/low (например, только close) берём close вместо них —
    индикатор не должен ронять расчёт из-за формы фрейма.
    """
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


#: Минут в таймфрейме — для «ATR за последний час».
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1h": 60, "2h": 120, "4h": 240, "1d": 1440}

#: Режимы рынка, которые определяет detect_regime.
REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE")
#: ATR выросла больше чем на столько процентов за час — паника.
PANIC_ATR_GROWTH_PCT = 50.0
#: ATR больше такого процента от цены — волатильность аномальна.
VOLATILE_ATR_PCT = 1.0


def _is_binance_geo_or_unavailable(exc: Exception) -> bool:
    return (isinstance(exc, ccxt.ExchangeNotAvailable)
            or "451" in str(exc))


def _vision_get(path: str, params: dict) -> object:
    url = "https://data-api.binance.vision/api/v3/" + path
    request = Request(url + "?" + urlencode(params),
                      headers={"User-Agent": "near-hackathon-nexus/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise
    if isinstance(payload, dict) and payload.get("code"):
        raise RuntimeError(f"Binance Vision API error: {payload.get('code')}")
    return payload


def fetch_binance_vision_ohlcv(symbol: str, timeframe: str,
                               limit: int) -> list:
    """Spot klines from Binance's public data mirror (no futures endpoints)."""
    return _vision_get("klines", {
        "symbol": symbol.replace("/", "").replace(":", ""),
        "interval": timeframe,
        "limit": limit,
    })


def fetch_binance_vision_exchange_info() -> dict:
    """Spot symbol metadata from Binance's public data mirror."""
    return _vision_get("exchangeInfo", {})


def detect_regime(snap: dict) -> str:
    """Режим рынка без LLM: TRENDING_UP / TRENDING_DOWN / RANGING / VOLATILE.

    VOLATILE — ATR выросла больше чем на 50% за час (признак паники) либо
    ATR аномально велика относительно цены; иначе тренд по EMA9/21 и
    MACD, всё остальное — флэт.
    """
    atr_growth = float(snap.get("atr_growth_pct") or 0.0)
    if atr_growth > PANIC_ATR_GROWTH_PCT:
        return "VOLATILE"
    price = float(snap.get("price") or 0.0)
    atr = float(snap.get("atr") or 0.0)
    if price > 0 and atr / price * 100.0 > VOLATILE_ATR_PCT:
        return "VOLATILE"
    trend = bool(snap.get("ema9_above_ema21"))
    macd = float(snap.get("macd") or 0.0)
    if trend and macd > 0:
        return "TRENDING_UP"
    if not trend and macd < 0:
        return "TRENDING_DOWN"
    return "RANGING"


def frame_from_snapshot(snap: dict) -> dict:
    """Плоский снапшот → блок одного таймфрейма (для сборки mtf)."""
    keys = ("timeframe", "ts", "price", "rsi", "rsi_prev", "macd",
            "macd_signal", "ema9", "ema21", "ema9_above_ema21", "atr",
            "atr_growth_pct", "volume", "volume_sma20", "volume_ratio")
    return {k: snap[k] for k in keys if k in snap}


def combined_regime(frames: dict) -> str:
    """Режим рынка по всем таймфреймам: паника на любом — паника везде."""
    regimes = [f.get("regime") for f in frames.values()]
    if "VOLATILE" in regimes:
        return "VOLATILE"
    primary = next(iter(frames.values()), {})
    return str(primary.get("regime") or "RANGING")


class MarketData:
    """Фид Binance через ccxt. sandbox/testnet включается отдельно и
    касается только приватных методов — публичный фид всегда mainnet-read.
    """

    def __init__(self, exchange_id: str = "binance",
                 symbol: str = "BTC/USDT"):
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        self._exchange_finalizer = weakref.finalize(self, self.ex.close)

    def close(self) -> None:
        """Release the ccxt HTTP session owned by this market-data instance."""
        self.ex = None
        if self._exchange_finalizer.alive:
            self._exchange_finalizer()

    def fetch_ohlcv(self, timeframe: str = "1h", limit: int = 120
                    ) -> pd.DataFrame:
        """OHLCV → DataFrame[ts, open, high, low, close, volume]."""
        try:
            rows = self.ex.fetch_ohlcv(self.symbol, timeframe, limit=limit)
        except Exception as exc:
            if (self.exchange_id != "binance"
                    or not _is_binance_geo_or_unavailable(exc)):
                raise
            rows = fetch_binance_vision_ohlcv(self.symbol, timeframe, limit)
        df = pd.DataFrame(
            rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """RSI(14), MACD(12/26/9), EMA(9/21), ATR(14) — колонки рядом с OHLCV."""
        df = df.copy()
        if _HAS_PANDAS_TA:
            import pandas_ta as ta
            df["rsi"] = ta.rsi(df["close"], length=14)
            macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
            df["macd"] = macd["MACD_12_26_9"]
            df["macd_signal"] = macd["MACDs_12_26_9"]
            df["ema9"] = ta.ema(df["close"], length=9)
            df["ema21"] = ta.ema(df["close"], length=21)
        else:
            df["rsi"] = _rsi(df["close"])
            df["macd"], df["macd_signal"], _ = _macd(df["close"])
            df["ema9"] = _ema(df["close"], 9)
            df["ema21"] = _ema(df["close"], 21)
        df["atr"] = _atr(df)
        return df

    def atr_context(self, df: pd.DataFrame, timeframe: str = "1h") -> dict:
        """ATR и его рост за час — под правило базы знаний «не входи, если
        ATR выросла больше чем на 50% за последний час»."""
        atr = float(df["atr"].iloc[-1])
        bars = max(1, round(60 / _TF_MINUTES.get(timeframe, 60)))
        prev = (float(df["atr"].iloc[-1 - bars]) if len(df) > bars else 0.0)
        return {"atr": round(atr, 4),
                "atr_growth_pct": (round((atr - prev) / prev * 100.0, 2)
                                   if prev else 0.0)}

    def volume_context(self, df: pd.DataFrame, window: int = 20) -> dict:
        """Объём последней ЗАКРОТОЙ свечи, его SMA(20) и отношение к средней.

        Последняя свеча биржи ещё формируется: её объём несопоставим со
        средней по закрытым барам (у только что открытой свечи объём
        почти нулевой) — иначе «объём падает» получается всегда. Поэтому
        в сравнение идёт закрытая свеча.

        volume_ratio < 1 — объём ниже нормы (пробой без объёма — ловушка),
        > 1.5 — активная фаза (резкое движение с деньгами).
        """
        if "volume" not in df.columns or df.empty:
            return {"volume": 0.0, "volume_sma20": 0.0, "volume_ratio": 1.0}
        vol = df["volume"]
        closed = vol.iloc[:-1] if len(vol) > 1 else vol
        last = float(closed.iloc[-1])
        window_vals = closed.iloc[-window:]
        sma = float(window_vals.mean()) if len(window_vals) else last
        return {"volume": round(last, 4),
                "volume_sma20": round(sma, 4),
                "volume_ratio": round(last / sma, 3) if sma else 1.0}

    def _frame_block(self, df: pd.DataFrame, timeframe: str) -> dict:
        """Блок одного таймфрейма: индикаторы + объём + режим рынка."""
        last = df.iloc[-1]
        prev = df.iloc[-2]
        block = {
            "timeframe": timeframe,
            "ts": str(last["ts"]),
            "price": float(last["close"]),
            "rsi": round(float(last["rsi"]), 2),
            "macd": round(float(last["macd"]), 4),
            "macd_signal": round(float(last["macd_signal"]), 4),
            "ema9": round(float(last["ema9"]), 2),
            "ema21": round(float(last["ema21"]), 2),
            "ema9_above_ema21": bool(last["ema9"] > last["ema21"]),
            "rsi_prev": round(float(prev["rsi"]), 2),
            **self.atr_context(df, timeframe),
            **self.volume_context(df),
        }
        block["regime"] = detect_regime(block)
        return block

    def snapshot_atr(self, timeframe: str = "1m", limit: int = 120) -> dict:
        """Только ATR-контекст по свечам (для демо-снимка с прочими
        синтетическими индикаторами)."""
        df = self.add_indicators(self.fetch_ohlcv(timeframe, limit))
        return self.atr_context(df, timeframe)

    def snapshot_context(self, timeframe: str = "1m",
                         limit: int = 120) -> dict:
        """ATR + объёмный контекст по реальным свечам.

        Для демо-снимка, где индикаторы синтетические: и паника (ATR), и
        объём должны решаться по факту, а не по сценарию.
        """
        df = self.add_indicators(self.fetch_ohlcv(timeframe, limit))
        return {**self.atr_context(df, timeframe),
                **self.volume_context(df)}

    def frame(self, timeframe: str = "15m", limit: int = 120) -> dict:
        """Блок одного таймфрейма по реальным свечам."""
        df = self.add_indicators(self.fetch_ohlcv(timeframe, limit))
        return self._frame_block(df, timeframe)

    def snapshot(self, timeframe: str = "1h", limit: int = 120) -> dict:
        """Последняя закрытая свеча + индикаторы → JSON для LLM."""
        df = self.add_indicators(self.fetch_ohlcv(timeframe, limit))
        block = self._frame_block(df, timeframe)
        return {"symbol": self.symbol, **block,
                "source": f"{self.exchange_id}:{timeframe}"}

    def snapshot_mtf(self, timeframes: tuple[str, ...] = ("1m", "15m"),
                     limit: int = 120) -> dict:
        """Мультитаймфреймовый снапшот: плоский блок 1m + блоки по ТФ.

        Плоские ключи (price, rsi, macd, volume…) — по главному
        таймфрейму, чтобы тень, risk-менеджер и старый код продолжали
        работать. В "mtf" лежат блоки каждого таймфрейма: агент смотрит
        свой (1m или 15m), а режим рынка считается по худшему из них.
        """
        frames: dict[str, dict] = {}
        for tf in timeframes:
            df = self.add_indicators(self.fetch_ohlcv(tf, limit))
            frames[tf] = self._frame_block(df, tf)
        if not frames:
            return {}
        primary_tf = "1m" if "1m" in frames else next(iter(frames))
        out = dict(frames[primary_tf])
        out["symbol"] = self.symbol
        out["mtf"] = frames
        out["regime"] = combined_regime(frames)
        return out
