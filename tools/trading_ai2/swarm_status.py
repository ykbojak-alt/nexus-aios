"""Статус торгового роя для экрана NEXUS SWARM — JSON на stdout.

Приложение NEXUS показывает в карточке «NEXUS SWARM» реальное состояние
роя из этого репозитория: пул персон и их приоритетный таймфрейм,
активную модель LLM, базу знаний, дарвиновские веса, ПОРТФЕЛЬ (активы,
которые рой реально торгует, а не один BTC), открытые позиции MarginPad
со счётом и последние циклы из истории торговли.

Рой давно мультивалютный: скринер «Слой 0» выбирает актив каждый цикл,
поэтому актив рынка берётся из последнего цикла истории, а не захардкожен.

Ключи API не читаются и не печатаются: наружу уходят только имена
моделей, агентов, символы и рыночные/счётные числа.

Запуск:
    python -m tools.trading_ai2.swarm_status [--pool 100] [--active 30]
                                             [--symbol SOL/USDT]
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HISTORY_PATH = REPO_ROOT / "logs" / "trading_history.jsonl"

#: Актив по умолчанию — только если в истории ещё нет ни одного цикла.
DEFAULT_SYMBOL = "BTC/USDT"
#: События сделок: открытая/закрытая позиция (для карточки «Портфель»).
POSITION_EVENTS = ("position_opened", "position_closed")
#: Сколько карточек активов показываем в портфеле (каждая = 1 запрос свечей).
PORTFOLIO_LIMIT = 5
#: Сколько последних событий читаем ради сделок — лента цикла длиннее.
TRADE_SCAN = 300
#: Окно истории портфеля: какие активы рой трогал в недавних циклах.
PORTFOLIO_SCAN = 60


def norm_symbol(symbol: object) -> str:
    """«BTC» → «BTC/USDT», «SOL/USDT» как есть — один формат для панели."""
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    return text if "/" in text else f"{text}/USDT"


def base_symbol(symbol: object) -> str:
    """«SOL/USDT» → «SOL» — тикер, который знает MarginPad."""
    return norm_symbol(symbol).split("/")[0]


# ------------------------------------------------------------------ рой
def _swarm_section(pool: int, active: int) -> dict:
    """Пул персон, таймфреймы, модель, база знаний и дарвиновские веса."""
    from tools.trading_ai2.swarm.agent_swarm import AgentPool, llm_status

    swarm = AgentPool(pool, active)
    personas: list[dict] = []
    for agent in swarm.agents:
        row = next((p for p in personas
                    if p["persona"] == agent.persona
                    and p["timeframe"] == agent.timeframe), None)
        if row is None:
            row = {"persona": agent.persona,
                   "timeframe": agent.timeframe, "count": 0}
            personas.append(row)
        row["count"] += 1

    model = ""
    notes: list[str] = []
    try:
        model, notes = llm_status()
    except Exception as exc:  # noqa: BLE001 — статус не должен падать
        notes = [f"llm_status недоступен: {exc}"]

    from tools.trading_ai2.swarm.darwin import load_weights
    from tools.trading_ai2.swarm.knowledge import load_rules

    file_weights = load_weights()
    # Живые веса пула — то, чем реально взвешен каждый голос в цикле.
    # Файл weights.json содержит только тех, кого уже судила сделка.
    live = sorted(((a.name, float(a.weight)) for a in swarm.agents),
                  key=lambda pair: -pair[1])
    learned = sum(1 for _name, weight in live
                  if abs(weight - 1.0) > 1e-9)
    if not learned:
        notes.append("веса стартовые (1.00): закрытых сделок в этом пуле "
                     "ещё не было — арбитр слышит всех одинаково")
    if file_weights and learned:
        notes.append(f"вес скорректирован по исходам сделок: "
                     f"{learned} из {len(live)} агентов")

    return {
        "agents": len(swarm),
        "pool": len(swarm),
        "active": swarm.active_n,
        "personas": personas,
        "model": model,
        "notes": notes,
        "rules": len(load_rules()),
        "weights": [{"name": n, "weight": round(w, 3)} for n, w in live[:6]],
        "weighted": learned,
        "tracked": len(file_weights),
        "examples": [a.name for a in swarm.top(10)],
    }


# ---------------------------------------------------------------- рынок
def _market_section(symbol: str, md=None) -> dict:
    """Мультитаймфреймовый снапшот по реальным свечам Binance.

    `md` — общий шлюз ccxt: load_markets() стоит ~4 с, поэтому отчёт
    держит один MarketData на все секции и меняет у него только символ.
    """
    if md is None:
        from tools.trading_ai2.market_data import MarketData
        md = MarketData(symbol=symbol)
    md.symbol = symbol
    snap = md.snapshot_mtf(("1m", "15m"))
    frames = []
    for tf, frame in (snap.get("mtf") or {}).items():
        frames.append({
            "timeframe": tf,
            "rsi": frame.get("rsi"),
            "macd": frame.get("macd"),
            "ema9Above21": frame.get("ema9_above_ema21"),
            "volume": frame.get("volume"),
            "volumeSma20": frame.get("volume_sma20"),
            "volumeRatio": frame.get("volume_ratio"),
            "regime": frame.get("regime"),
            "atr": frame.get("atr"),
            "atrGrowthPct": frame.get("atr_growth_pct"),
        })
    return {
        "symbol": snap.get("symbol", symbol),
        "price": snap.get("price"),
        "rsi": snap.get("rsi"),
        "macd": snap.get("macd"),
        "ema9Above21": snap.get("ema9_above_ema21"),
        "volume": snap.get("volume"),
        "volumeSma20": snap.get("volume_sma20"),
        "volumeRatio": snap.get("volume_ratio"),
        "regime": snap.get("regime"),
        "frames": frames,
        "source": "binance:public",
    }


def active_symbol(rows: list[dict]) -> str:
    """Актив, на котором рой торгует сейчас: последний символ из истории.

    Скринер «Слой 0» выбирает актив каждый цикл — захардкоженный BTC
    показывал бы панель, которая врёт после первого же ребаланса.
    """
    for row in reversed(rows):
        symbol = norm_symbol(row.get("symbol"))
        if symbol:
            return symbol
    return DEFAULT_SYMBOL


# -------------------------------------------------------------- портфель
def _portfolio_section(rows: list[dict], positions: dict,
                       md=None, market: dict | None = None) -> dict:
    """Активы, которые рой реально держит или торговал, с живыми свечами.

    Порядок: активный символ → символы открытых позиций → последние
    торгованные символы из истории. Для каждого — один реальный
    15m-кадр Binance (цена, RSI, режим, объём против SMA20). Активному
    символу кадр не нужен: он уже посчитан в секции «market».
    """
    if md is None:
        from tools.trading_ai2.market_data import MarketData
        md = MarketData()

    open_syms = [norm_symbol(p.get("symbol"))
                 for p in (positions.get("positions") or [])]
    open_syms = [s for s in open_syms if s]
    current = active_symbol(rows)
    order: list[str] = [current]
    for sym in open_syms + [norm_symbol(r.get("symbol")) for r in
                            reversed(rows)]:
        if sym and sym not in order:
            order.append(sym)

    held, traded = set(open_syms), {norm_symbol(r.get("symbol")) for r in rows}
    frame15 = next((f for f in (market or {}).get("frames") or []
                    if f.get("timeframe") == "15m"), None)

    assets: list[dict] = []
    for sym in order[:PORTFOLIO_LIMIT]:
        base = {"symbol": sym, "held": sym in held,
                "traded": sym in traded, "active": sym == current}
        if sym == current and frame15 and market:
            assets.append({**base, **frame15,
                           "price": market.get("price"),
                           "source": market.get("source", "binance:public")})
            continue
        try:
            md.symbol = sym
            frame = md.frame("15m", limit=60)
        except Exception as exc:  # noqa: BLE001 — актив не роняет портфель
            assets.append({**base, "error": str(exc)[:120]})
            continue
        assets.append({
            **base,
            "price": frame.get("price"),
            "rsi": frame.get("rsi"),
            "macd": frame.get("macd"),
            "ema9Above21": frame.get("ema9_above_ema21"),
            "volume": frame.get("volume"),
            "volumeSma20": frame.get("volume_sma20"),
            "volumeRatio": frame.get("volume_ratio"),
            "regime": frame.get("regime"),
            "atr": frame.get("atr"),
            "atrGrowthPct": frame.get("atr_growth_pct"),
            "source": "binance:public",
        })
    return {"activeSymbol": current, "assets": assets,
            "symbols": [a["symbol"] for a in assets]}


# -------------------------------------------------------------- позиции
def _positions_section() -> dict:
    """Счёт и открытые позиции MarginPad — только числа, без ключей."""
    from tools.trading_ai2.funding_executor import get_balance, list_positions

    try:
        from tools.research_agent_clients import env
    except ImportError:  # pragma: no cover — запуск скриптом
        from research_agent_clients import env

    # ключ живёт в .env и наружу не идёт — только сам факт, что он есть
    key = env("MARGINPAD_API_KEY")
    if not key:
        return {"live": False, "source": "marginpad",
                "note": "MARGINPAD_API_KEY не задан — счёт недоступен "
                        "(DRY-RUN: позиций нет)",
                "balance": {}, "positions": [], "closed": []}

    balance_raw = get_balance(key) or {}
    positions_raw = list_positions(key) or {}
    # 401/5xx от MarginPad приходит как {"error": ...}: без этой строки
    # панель показала бы пустой нулевой счёт вместо «счёт недоступен».
    account_error = balance_raw.get("error") or positions_raw.get("error")
    all_positions = positions_raw.get("positions") or []
    open_positions = [p for p in all_positions if p.get("status") == "open"]
    closed = [p for p in all_positions if p.get("status") != "open"]

    import time as _time

    def row(p: dict) -> dict:
        opened = p.get("opened_ts")
        # opened_ts приходит с сервера MarginPad: его часы могут спешить
        # на долю секунды, и возраст ушел бы в минус («-0.8с» в панели).
        age = (round(max(0.0, _time.time() - float(opened) / 1000.0), 1)
               if opened else None)
        return {
            "id": p.get("id"),
            "symbol": norm_symbol(p.get("symbol")),
            "side": p.get("side"),
            "status": p.get("status"),
            "marginUsd": p.get("margin_usd"),
            "leverage": p.get("leverage"),
            "entryPrice": p.get("entry_price"),
            "markPrice": p.get("mark_price", p.get("exit_price")),
            "unrealizedPnlUsd": p.get("unrealized_pnl_usd",
                                      p.get("pnl_usd")),
            "realizedPnlUsd": p.get("pnl_usd"),
            "qty": p.get("qty"),
            "ageS": age,
        }

    realized = sum(float(p.get("pnl_usd") or 0.0) for p in closed)
    return {
        "live": not account_error,
        "source": "marginpad",
        "error": (f"marginpad: {str(account_error)[:120]}"
                  if account_error else None),
        "balance": {
            "balanceUsd": balance_raw.get("balance_usd"),
            "equityUsd": balance_raw.get("equity_usd"),
            "marginInUseUsd": balance_raw.get("margin_in_use_usd"),
            "freeMarginUsd": balance_raw.get("free_margin_usd"),
            "unrealizedPnlUsd": balance_raw.get("unrealized_pnl_usd"),
            "startingBalanceUsd": balance_raw.get("starting_balance_usd"),
        },
        "positions": [row(p) for p in open_positions],
        "closedCount": len(closed),
        "realizedPnlUsd": round(realized, 2),
        "symbols": sorted({norm_symbol(p.get("symbol"))
                           for p in open_positions} - {""}),
        "note": (f"MarginPad не отдал счёт: {str(account_error)[:120]}"
                 if account_error else
                 "счёт MarginPad: симулятор на реальных ценах, "
                 "не настоящие деньги"),
    }


# --------------------------------------------------------------- история
def _history_section(limit: int) -> list[dict]:
    """Последние события из logs/trading_history.jsonl."""
    rows: list[dict] = []
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    return rows


def _trades_section(rows: list[dict] | None = None, limit: int = 10,
                    scan: int = TRADE_SCAN) -> dict:
    """Сделки из истории: position_opened / position_closed.

    Панель показывает их отдельным списком — «активные треды» видно даже
    когда MarginPad уже отдал пустой список открытых позиций. Окно
    просмотра шире, чем лента последних событий: циклы вытесняют сделки
    из хвоста файла за несколько минут.
    """
    if rows is None:
        rows = _history_section(scan)
    events = [r for r in rows if r.get("event") in POSITION_EVENTS]
    out = []
    for r in events[-limit:]:
        out.append({
            "ts": r.get("ts"),
            "event": r.get("event"),
            "symbol": norm_symbol(r.get("symbol")),
            "side": r.get("side"),
            "id": r.get("id"),
            "marginUsd": r.get("margin_usd"),
            "entryPrice": r.get("entry_price"),
            "price": r.get("price"),
            "pnlUsd": r.get("pnl_usd"),
            "holdS": r.get("hold_s"),
            "reason": r.get("reason"),
            "mode": r.get("mode"),
        })
    realized = [float(r.get("pnl_usd") or 0.0) for r in events
                if r.get("event") == "position_closed"]
    return {
        "rows": out,
        "closedCount": len(realized),
        "realizedPnlUsd": round(sum(realized), 2),
        "note": "" if out else "сделок в истории пока нет",
    }


# ----------------------------------------------------------------- отчёт
def _section(out: dict, name: str, loader) -> object:
    """Одна секция отчёта: ошибка не роняет остальные."""
    try:
        value = loader()
    except Exception as exc:  # noqa: BLE001 — секция не роняет отчёт
        out["errors"].append(f"{name}: {exc}")
        return None
    out["sections"][name] = value
    return value


def build_report(pool: int = 100, active: int = 30,
                 symbol: str = "", history: int = 12) -> dict:
    """Единый отчёт для CLI и HTTP-движка.

    Счёт MarginPad (HTTP) и рынок (ccxt) считаются параллельно, а
    портфель переиспользует уже снятый кадр активного символа: по
    очереди отчёт собирался бы ~30 секунд вместо ~8.
    """
    from concurrent.futures import ThreadPoolExecutor

    from tools.trading_ai2.market_data import MarketData

    out: dict = {
        "ok": True,
        "repo": str(REPO_ROOT),
        "python": platform.python_version(),
        "sections": {},
        "errors": [],
    }
    rows = _section(out, "history",
                    lambda: _history_section(max(history, TRADE_SCAN))) or []
    recent = rows[-history:] if history > 0 else rows
    md = MarketData(symbol=symbol or active_symbol(rows))
    market_symbol = md.symbol
    try:
        with ThreadPoolExecutor(max_workers=2) as jobs:
            pos_job = jobs.submit(_section, out, "positions", _positions_section)
            mkt_job = jobs.submit(_section, out, "market",
                                  lambda: _market_section(market_symbol, md))
            positions = pos_job.result() or {}
            market = mkt_job.result() or {}
        _section(out, "swarm", lambda: _swarm_section(pool, active))
        _section(out, "trades", lambda: _trades_section(rows))
        _section(out, "portfolio",
                 lambda: _portfolio_section(rows[-PORTFOLIO_SCAN:],
                                            positions, md, market))
    finally:
        md.close()
    out["sections"]["history"] = recent
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Статус роя NEXUS SWARM (JSON).")
    parser.add_argument("--pool", type=int, default=100,
                        help="слотов в AgentPool")
    parser.add_argument("--active", type=int, default=30,
                        help="сколько агентов пула голосует за цикл")
    parser.add_argument("--agents", type=int, default=0,
                        help="устаревший синоним --pool")
    parser.add_argument("--symbol", default="",
                        help="актив рынка (по умолчанию — последний из истории)")
    parser.add_argument("--history", type=int, default=12)
    args = parser.parse_args()

    pool = args.pool or args.agents or 100
    print(json.dumps(build_report(pool, args.active, args.symbol,
                                  args.history),
                     ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
