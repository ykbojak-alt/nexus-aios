"""demo_trading_grand.py — единый пайплайн Three Minds → MarginPad.

  python -m tools.trading_ai2.demo_trading_grand            # демо-сценарий
  python -m tools.trading_ai2.demo_trading_grand --live     # реальный рынок
  python -m tools.trading_ai2.demo_trading_grand --loop --interval 60
                                                            # автономный режим

Цикл: [data] снапшот → [swarm] 60 голосов → [news] оценка фона →
Генератор → Критик → Арбитр → [execution] ордер в MarginPad.

Честность сценариев (обе метки печатаются в логе):
  демо (по умолчанию) — индикаторы синтетические, цена РЕАЛЬНАя
      (GET /price MarginPad), оценка новостей помечена demo-override;
      ордер — настоящий, position_id от MarginPad;
  --live — настоящие свечи Binance и настоящая оценка новостей
      через LLM; сегодня рынок в панике, и гейты отменяют сделку.

Для TUI: штатный выход — exit code 0, subprocess-безопасно.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.trading_ai2.market_data import (
    MarketData, combined_regime, detect_regime, frame_from_snapshot)
from tools.trading_ai2.funding_executor import (
    FundingExecutor, get_price, get_balance, list_positions,
    close_position)
from tools.trading_ai2.swarm.agent_swarm import (
    AgentPool, SwarmVote, _model_label, apply_lessons, llm_status,
    make_swarm, swarm_llm_line)
from tools.trading_ai2.swarm.knowledge import summary as knowledge_summary
from tools.trading_ai2.swarm.portfolio import (
    decide_rebalance, filter_uncorrelated, get_top_assets)
from tools.trading_ai2.swarm.llm_factory import LLMFactory

try:
    from tools.research_agent_clients import env
except ImportError:  # pragma: no cover — запуск скриптом
    from research_agent_clients import env
from tools.trading_ai2.swarm.learning_bridge import TradingLearning
from tools.trading_ai2.swarm.generator import Generator
from tools.trading_ai2.swarm.critic import Critic
from tools.trading_ai2.swarm.arbiter import (
    Arbiter, CHAOS_ATR_GROWTH, Consensus)
from tools.trading_ai2.swarm.debate_swarm import (
    run_parliament, meta_votes, risk_veto)
from tools.trading_ai2.swarm.news_sentiment import (
    get_asset_sentiment, get_market_sentiment, invalidate_cache)
from tools.trading_ai2.swarm.epoch_rewarder import load_reputation

try:
    from ansi_demo import stage, gold, cyan, green, magenta, red, dim, \
        rule, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, red, \
        dim, rule, slow_print, logo_lines

DEMO_SENTIMENT = 0.35          # зелёный свет демо-сценария (override)
EQUITY_USD = 10_000.0
#: Артефакты прогонов: траектории AI-2 (schema 0.1) и файл уроков ai2.lessons/1
RUNS_DIR = Path(__file__).resolve().parents[2] / "runs" / "trading"
#: История торговли для overnight-анализа: одна JSONL-строка на событие
HISTORY_PATH = Path(__file__).resolve().parents[2] / "logs" / \
    "trading_history.jsonl"
#: Кто голосовал за сторону открытой позиции — веса этих агентов
#: пересчитаются, когда позиция закроется.
_POSITION_VOTERS: dict[str, list[str]] = {}

#: Chaos Mode: id позиций, открытых в шторм (risk_mode=MICRO). У них
#: свои выходы — TP +0.8%, SL -0.5%, time-exit 120с — вместо
#: стандартных profit-take/max-age.
_CHAOS_POSITIONS: set[str] = set()
CHAOS_TP_PCT, CHAOS_SL_PCT, CHAOS_MAX_AGE_S = 0.8, -0.5, 120.0

#: id позиций, для которых уже ушёл /close: MarginPad отдаёт их «open»
#: ещё пару секунд — без дедупа одна позиция закрывается и пишется
#: в историю дважды, а её минус считается в стрике два раза.
_CLOSE_REQUESTED: set[str] = set()
#: id позиций, чей исход уже учтён в серии побед/поражений.
_COUNTED_CLOSES: set[str] = set()

#: Dynamic Risk Profiling: текущий профиль Арбитра — попадает в каждую
#: запись истории; стрики и ручной оверрайд живут в маленьких json-файлах
#: рядом с историей — переживают рестарт и читаются каждый цикл.
_CURRENT_PROFILE: str = "BALANCED"
_RISK_STATE = HISTORY_PATH.parent / "risk_state.json"
_RISK_OVERRIDE = HISTORY_PATH.parent / "risk_override.json"

#: Мягкий стоп автономного прогона: движок панели (или человек) создаёт
#: файл — loop проверяет флаг между итерациями и сворачивается штатным
#: circuit breaker'ом, закрывая позиции. Сигналы ОС тут не годятся:
#: на Windows CTRL_BREAK не прерывает сон main-thread Python-процесса,
#: а terminate()/SIGTERM убивает процесс мгновенно — позиции остались
#: бы висеть на счёте без закрытия и без записи в историю.
LOOP_STOP_FLAG = Path(os.environ.get("NEXUS_LOOP_STOP_FLAG")
                      or HISTORY_PATH.parent / "loop_stop.flag")


def stop_requested() -> bool:
    """Флаг стопа читаем и убираем: старый флаг не должен загонять в
    circuit breaker следующий прогон."""
    try:
        if LOOP_STOP_FLAG.exists():
            LOOP_STOP_FLAG.unlink()
            return True
    except OSError:                   # нет доступа — считаем, что стопа нет
        pass
    return False


def sleep_or_stop(seconds: float, slice_s: float = 1.0) -> bool:
    """Сон между итерациями с проверкой флага: True — пора сворачиваться."""
    deadline = time.time() + max(0.0, seconds)
    while True:
        left = deadline - time.time()
        if left <= 0:
            return stop_requested()
        time.sleep(min(slice_s, left))
        if stop_requested():
            return True


def _risk_streaks() -> dict:
    """{win, loss} — серии последних исходов из risk_state.json."""
    try:
        st = json.loads(_RISK_STATE.read_text(encoding="utf-8"))
        return {"win": int(st.get("win") or 0),
                "loss": int(st.get("loss") or 0)}
    except Exception:                       # noqa: BLE001 — файла нет
        return {"win": 0, "loss": 0}


def _risk_note_close(pnl: float | None, position_id: str | None = None
                     ) -> None:
    """Исход сделки → серия побед/поражений.

    Считаем только позиции, которые открыл наш Арбитр (id есть в
    _POSITION_VOTERS): на общем счёту MarginPad лежат и чужие сделки
    (другие прогоны/процессы), их пыльный PnL — не наш результат.
    Каждая позиция учитывается ровно один раз — MarginPad пару секунд
    отдаёт закрытую позицию «open», и повторное закрытие раздувало
    loss_streak дублем. None-итог серию не трогает: честный null
    лучше выдуманного результата.
    """
    if pnl is None:
        return
    pid = str(position_id or "")
    if pid in _COUNTED_CLOSES or pid not in _POSITION_VOTERS:
        return
    _COUNTED_CLOSES.add(pid)
    st = _risk_streaks()
    if float(pnl) > 0:
        st["win"], st["loss"] = st["win"] + 1, 0
    elif float(pnl) < 0:
        st["loss"], st["win"] = st["loss"] + 1, 0
    try:
        _RISK_STATE.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


def _risk_override() -> str | None:
    """Ручной профиль из risk_override.json (клавиша R в TUI)."""
    try:
        name = str(json.loads(_RISK_OVERRIDE.read_text(
            encoding="utf-8")).get("profile") or "").upper()
        return name or None
    except Exception:                       # noqa: BLE001 — нет файла
        return None
# Цикл к циклу (live): price/sentiment для триггеров сброса кэша новостей.
_PREV_CYCLE: dict = {}


def _persist(event: str, **fields) -> None:
    """Добавить событие в logs/trading_history.jsonl (атомарная строка).

    Ошибки записи не должны ронять торговый цикл — лог истории
    вторичен по отношению к безопасности исполнения.
    """
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc)
               .isoformat(timespec="seconds"),
               "event": event, **fields}
        rec.setdefault("risk_profile", _CURRENT_PROFILE)
        if event == "position_closed":
            _risk_note_close(fields.get("pnl_usd"), fields.get("id"))
        with HISTORY_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str)
                     + "\n")
    except OSError:
        pass


#: Сколько строк дерева дебатов кладётся в историю: окно «Решение» показывает
#: его целиком, а JSONL не обязан разрастаться до мегабайт за ночь прогонов.
DEBATE_LINES_IN_HISTORY = 80


def _swarm_entry(v, models: dict) -> dict:
    """Вердикт мини-роя → карточка: финальные голоса плюс переходы
    второго раунда.

    «opening_*» берутся из раунда 1 и есть только когда дебаты были:
    при единогласии отсчётной точки нет, и панель честно пишет
    «единогласно», а не рисует несуществующую смену голоса.
    """
    before = {m.agent: m for m in (getattr(v, "opening", None) or [])}
    members = []
    for m in v.members:
        o = before.get(m.agent)
        members.append({"agent": m.agent, "persona": m.persona,
                        "action": m.action,
                        "confidence": round(float(m.confidence), 3),
                        "weight": round(float(m.weight), 3),
                        "model": models.get(m.agent),
                        "mode": m.mode,
                        "opening_action": o.action if o else m.action,
                        "opening_confidence": (round(float(o.confidence), 3)
                                               if o else None),
                        "changed": bool(o and o.action != m.action)})
    return {"name": v.name, "action": v.action,
            "score": round(float(v.score), 3),
            "confidence": round(float(v.confidence), 3),
            "tally": {a: sum(1 for m in v.members if m.action == a)
                      for a in ("BUY", "SELL", "HOLD")},
            "debated": bool(before),
            "moved": sum(1 for m in members if m["changed"]),
            "members": members}


def _veto_state(veto: str, pre: dict | None) -> dict:
    """Что абсолютное вето сделало с вердиктом Арбитра.

    Без этих полей запись цикла не различает «перебил BUY» и «подтвердил
    HOLD»: владелец видит HOLD и не может проверить, сработало ли правило
    Risk Swarm > 0.7 или рой и так молчал.
    """
    if not veto or not pre:
        return {"veto_before": None, "veto_changed": False}
    action = str(pre.get("action") or "")
    return {
        "veto_before": {"action": action,
                        "score": round(float(pre.get("score") or 0.0), 3),
                        "approved": bool(pre.get("approved"))},
        "veto_changed": action != "HOLD",
    }


def _veto_impact(pre: dict | None) -> str:
    """Строка TUI: вето перебило вердикт Арбитра или подтвердил его."""
    action = str((pre or {}).get("action") or "HOLD")
    return (f"перебил {action} → HOLD" if action != "HOLD"
            else f"{action} и без вета — вето подтверждено")


def _debate_record(verdicts: list, lines: list, veto: str,
                   models: dict | None = None,
                   pre_veto: dict | None = None) -> dict | None:
    """Итог парламента для записи цикла: вердикты мини-роев с голосами
    участников, дерево строк TUI и след вета Мета-Арбитра.

    Без парламента — None: плоское голосование не притворяется решением
    трёх роев, и панель не рисует пустое окно «Решение».
    """
    if not verdicts:
        return None
    models = models or {}
    return {
        "veto": veto,
        **_veto_state(veto, pre_veto),
        "swarms": [_swarm_entry(v, models) for v in verdicts],
        "lines": [str(x)[:240] for x in list(lines)[:DEBATE_LINES_IN_HISTORY]],
    }


def _log(say, tag: str, msg: str, color=dim) -> None:
    say(color(f"    {tag:<14} {msg}"))
def demo_snapshot(symbol: str) -> dict:
    """Демо-индикаторы на РЕАЛЬНОЙ цене MarginPad — помечается честно.

    Сценарий «паника → разворот»: RSI перепродан, цена ниже EMA21
    (mr персон берёт), но EMA9 уже выше EMA21 (тренд вверх) — рой
    голосует за покупку, новости дают зелёный свет.
    """
    price = float(get_price(symbol)["price"])
    return {"symbol": symbol, "timeframe": "1m", "ts": "demo-scenario",
            "price": price, "rsi": 28.0, "rsi_prev": 44.0,
            "macd": 180.0, "macd_signal": 60.0,
            "ema9": round(price * 1.005, 2),
            "ema21": round(price * 1.03, 2),
            "ema9_above_ema21": True, "source": "demo+marginpad-price"}


def _data_line(snap: dict) -> str:
    """Строка [data]: цена, индикаторы, объём, режим рынка, таймфреймы."""
    vol = float(snap.get("volume") or 0.0)
    sma = float(snap.get("volume_sma20") or 0.0)
    ratio = float(snap.get("volume_ratio") or 0.0)
    frames = ", ".join(f"{tf}:{float(f.get('rsi') or 0):.0f}RSI"
                       for tf, f in (snap.get("mtf") or {}).items())
    return (f"{snap.get('symbol', '')} {float(snap.get('price') or 0):.2f} "
            f"RSI={snap.get('rsi')} MACD={float(snap.get('macd') or 0):+.2f} "
            f"EMA9/21={'+' if snap.get('ema9_above_ema21') else '-'} "
            f"объём={vol:.2f} (SMA20 {sma:.2f}, ×{ratio:.2f}) "
            f"режим={snap.get('regime', '?')} [{frames}]")


def _vote_line(i: int, total: int, ag, vote) -> str:
    """Строка голосования: [vote 01/13] TrendFollower-01 (15m) → BUY …

    Таймфрейм в скобках — по нему видно, какой срез рынка смотрела
    персона (1m — Scalper и Momentum, 15m — TrendFollower, Breakout
    и MeanReversion).
    """
    mark = (green("BUY ") if vote.action == "BUY" else
            red("SELL") if vote.action == "SELL" else dim("HOLD"))
    tag = _model_label(ag.model_id) if ag.model_id else "stub"
    asset = getattr(ag, "asset", "")
    ctx = ag.timeframe + (f"·{asset}" if asset else "")
    return (f"    [vote {i:02d}/{total}] {ag.name} ({ctx})·{tag} → "
            f"{mark} conf {vote.confidence:.2f} w {vote.weight:.2f} · "
            f"{vote.reason[:200]}")


def build_snapshot(a, base: str, md, say) -> dict:
    """Снапшот рынка: два таймфрейма (1m + 15m), объём и режим рынка.

    LIVE — реальные свечи по обоим таймфреймам.
    DEMO — 1m-индикаторы синтетические (сценарий «паника → разворот»),
    но объём, ATR и среднесрочный 15m — реальные: правила паники и
    объёма должны решаться по факту, а не по сценарию.
    """
    if a.live:
        snap = md.snapshot_mtf(("1m", "15m"))
        _log(say, "[data]", _data_line(snap) + " [binance 1m+15m]", cyan)
        return snap

    snap = demo_snapshot(base)
    frames: dict[str, dict] = {}
    try:
        snap = {**snap, **md.snapshot_context("1m")}
    except Exception as e:                      # noqa: BLE001
        _log(say, "[data]", f"ATR/объём недоступны: {e}", dim)
    frames["1m"] = frame_from_snapshot(snap)
    try:
        frames["15m"] = md.frame("15m")
    except Exception as e:                      # noqa: BLE001
        _log(say, "[data]", f"15m недоступен: {e}", dim)
        frames["15m"] = {**frames["1m"], "timeframe": "15m"}
    for frame in frames.values():
        if not frame.get("regime"):
            frame["regime"] = detect_regime(frame)
    snap["mtf"] = frames
    snap["regime"] = combined_regime(frames)
    _log(say, "[data]", _data_line(snap) +
         " [1m демо-индикаторы, 15m и объём — реальные свечи]", cyan)
    return snap


def _account_line(fex: FundingExecutor, say,
                  tag: str = "[account]") -> None:
    """Состояние счёта: баланс, эквити и сколько маржи задействовано."""
    try:
        b = get_balance(fex.key)
    except Exception:                           # noqa: BLE001
        return
    _log(say, tag, f"Balance: ${float(b.get('balance_usd') or 0):,.2f} | "
         f"Equity: ${float(b.get('equity_usd') or 0):,.2f} | "
         f"margin_in_use: ${float(b.get('margin_in_use_usd') or 0):,.2f}",
         cyan)


def _position_by_id(key: str, position_id: str) -> dict | None:
    """GET /positions → запись одной позиции по id (None — не нашли)."""
    try:
        ps = list_positions(key).get("positions") or []
    except Exception:                           # noqa: BLE001
        return None
    for p in ps:
        if p.get("id") == position_id:
            return p
    return None


def close_and_report(fex: FundingExecutor, say, position_id: str,
                     *, delay_s: float = 0.0, reason: str = "",
                     learner=None, snap: dict | None = None,
                     personas: list[str] | None = None, mode: str = "",
                     swarm=None) -> dict:
    """Закрыть позицию и честно записать реализованный PnL.

    Ждём delay_s — PnL у MarginPad обновляется с задержкой, закрывать
    сразу после открытия бессмысленно. Перед закрытием снимаем
    mark-to-market, после закрытия читаем pnl_usd уже у закрытой
    позиции: это итог с комиссиями, а не оценка.

    Исход сделки меняет дарвиновские веса агентов, голосовавших за эту
    сторону: пересчёт делает Learning Engine (learner.record_trade),
    строки вида «[learning] Agent TrendFollower-01 weight: 1.00 -> 0.80
    (wrong prediction)» печатаются в TUI и пишутся в swarm/weights.log.
    """
    if delay_s > 0:
        time.sleep(delay_s)
    before = _position_by_id(fex.key, position_id) or {}
    mark = before.get("unrealized_pnl_usd")
    hold_s = _age_s(before) if before else delay_s
    if isinstance(mark, (int, float)):
        say(dim(f"    [execution] Position {position_id} mark after "
                f"{hold_s:.0f}s hold: {float(mark):+.2f}$"))
    r = close_position(position_id, fex.key)
    _CLOSE_REQUESTED.add(str(position_id))
    closed = _position_by_id(fex.key, position_id) or {}
    realized = closed.get("pnl_usd")
    if realized is None:
        realized = r.get("realized_pnl_usd")
    realized = float(realized or 0.0)
    sign = "+" if realized >= 0 else "-"
    tail = f" ({reason})" if reason else ""
    say(gold(f"    [execution] Position {position_id} CLOSED. "
             f"Realized PnL: {sign}${abs(realized):.2f}.{tail}"))
    _persist("position_closed", id=position_id,
             symbol=(closed or before).get("symbol"),
             side=(closed or before).get("side"),
             margin_usd=(closed or before).get("margin_usd"),
             entry_price=(closed or before).get("entry_price"),
             exit_price=closed.get("exit_price"),
             reason=reason, hold_s=round(hold_s, 1), pnl_usd=realized,
             mode=mode)
    try:
        eq = float(get_balance(fex.key).get("equity_usd") or 0)
        say(magenta(f"    [realized]     PnL: {sign}${abs(realized):.2f} "
                    f"| Balance updated: equity ${eq:,.2f}"))
    except Exception:                           # noqa: BLE001
        say(magenta(f"    [realized]     PnL: {sign}${abs(realized):.2f} "
                    f"| Balance updated."))
    # голоса за исполненную сторону: из вызова или из записи при открытии
    voters = list(personas or []) or _POSITION_VOTERS.pop(position_id, [])
    if learner is not None and snap is not None:
        # исход сделки → веса агентов → траектория AI-2 → память → урок
        side = {"long": "BUY", "short": "SELL"}.get(
            str((closed or {}).get("side") or "").lower(), "")
        outcome = learner.record_trade(
            action=side or "HOLD", pnl_usd=realized, snap=snap,
            position_id=position_id, personas=voters, mode=mode,
            swarm=swarm)
        for line in outcome["lines"]:
            say(line)
    elif learner is not None and swarm is not None and voters:
        # снимка рынка нет (аварийное закрытие) — веса всё равно честно
        # пересчитываем: Learning Engine от AI-2 не зависит
        for line in learner.apply_weights(swarm, voters, realized):
            say(line)
    return {"id": position_id, "ok": bool(r.get("ok", True)),
            "realized_pnl_usd": realized, "mark_pnl_usd": mark,
            "position": closed}


def close_open_positions(fex: FundingExecutor, say, *,
                         reason: str = "", learner=None,
                         snap: dict | None = None, mode: str = "",
                         swarm=None) -> int:
    """Закрыть ВСЕ открытые позиции — аварийный путь.

    В штатном loop-режиме позициями управляет manage_positions
    (profit-take / max-age, до --max-positions штук одновременно).
    Эта функция — только для circuit breaker при выходе.
    """
    if not fex.live:
        _log(say, "[safety]", "dry-run — позиций нет", dim)
        return 0
    try:
        ps = [p for p in list_positions(fex.key).get("positions", [])
              if p.get("status") == "open"]
    except Exception as e:                      # noqa: BLE001
        _log(say, "[safety]", f"positions error: {e}", red)
        return 0
    if not ps:
        _log(say, "[safety]", "открытых позиций нет", dim)
        return 0
    for p in ps:
        close_and_report(fex, say, p["id"], reason=reason, learner=learner,
                         snap=snap, mode=mode, swarm=swarm)
    _log(say, "[safety]", f"маржа свободна — закрыто позиций: {len(ps)}",
         green)
    return len(ps)


def close_all_positions(fex: FundingExecutor, say, *,
                        reason: str = "circuit breaker", learner=None,
                        snap: dict | None = None, mode: str = "",
                        swarm=None) -> int:
    """Circuit-breaker: закрыть ВСЕ открытые позиции через POST /close."""
    return close_open_positions(fex, say, reason=reason, learner=learner,
                                snap=snap, mode=mode, swarm=swarm)


def _age_s(position: dict) -> float:
    """Возраст позиции в секундах по opened_ts (мс)."""
    ts = position.get("opened_ts")
    if not ts:
        return 0.0
    return max(0.0, time.time() - float(ts) / 1000.0)


def closed_trade_fields(pos: dict, res: dict, rec: dict) -> dict:
    """Итог закрытия для истории: реализованный PnL, выход, удержание.

    MarginPad кладёт pnl_usd/exit_price/closed_ts в запись позиции, а не в
    ответ /close, поэтому запись перечитывают (rec) и берут из неё числа.
    Честный null лучше выдуманного нуля: панель покажет «—», а не «$0.00»
    там, где MarginPad итог ещё не отдал.
    """
    pnl = rec.get("pnl_usd")
    if pnl is None:
        pnl = res.get("realized_pnl_usd")
    opened = pos.get("opened_ts")
    closed_ts = rec.get("closed_ts")
    hold_s = (max(0.0, round((float(closed_ts) - float(opened)) / 1000.0, 1))
              if opened and closed_ts else _age_s(pos))
    return {"exit_price": rec.get("exit_price"),
            "hold_s": hold_s,
            "pnl_usd": float(pnl) if pnl is not None else None}


def manage_positions(fex: FundingExecutor, say, *,
                     max_age_s: float, min_hold_s: float = 30.0,
                     take_profit_usd: float = 1.2, learner=None,
                     snap: dict | None = None, mode: str = "",
                     swarm=None) -> list[dict]:
    """Profit-take / max-age: закрыть дохеджировавшие позиции.

    Каждую итерацию цикла:
      - age < min_hold_s → держим всегда: PnL ещё качается, раннее
        закрытие = потеря на комиссиях;
      - unrealized_pnl > take_profit_usd → profit-take (порог выше
        измеренной стоимости закрытия ~$0.57 — иначе realized
        уходит в минус);
      - age > max_age_s → принудительное закрытие.
    Возвращает список ОСТАВШИХСЯ открытых.
    """
    if not fex.live:
        return []
    try:
        ps = [p for p in list_positions(fex.key).get("positions", [])
              if p.get("status") == "open"]
    except Exception as e:                      # noqa: BLE001
        _log(say, "[risk]", f"positions error: {e}", red)
        return []
    kept = []
    for p in ps:
        if str(p.get("id")) in _CLOSE_REQUESTED:
            continue                  # /close уже ушёл — не дублируем
        pnl = float(p.get("unrealized_pnl_usd") or 0.0)
        age = _age_s(p)
        if str(p.get("id")) in _CHAOS_POSITIONS:
            # Chaos Mode: шторм не ждёт — TP +0.8%, SL -0.5%, выход
            # через 120с. Ход цены ≈ pnl / (маржа × плечо).
            notional = (float(p.get("margin_usd") or 0.0)
                        * float(p.get("leverage") or 1.0))
            move = pnl / notional * 100 if notional > 0 else 0.0
            hit = (f"take-profit +{CHAOS_TP_PCT}%" if move >= CHAOS_TP_PCT
                   else f"stop-loss {CHAOS_SL_PCT}%"
                        if move <= CHAOS_SL_PCT
                   else "time-exit 120s" if age > CHAOS_MAX_AGE_S
                   else "")
            if hit:
                _log(say, "[chaos]", f"close {p['id']} ({hit}, "
                     f"move {move:+.2f}%, age {age:.0f}s)", magenta)
                close_and_report(fex, say, p["id"], reason=hit,
                                 learner=learner, snap=snap, mode=mode,
                                 swarm=swarm)
                _CHAOS_POSITIONS.discard(str(p["id"]))
            else:
                _log(say, "[chaos]", f"hold {p['id']} (move "
                     f"{move:+.2f}%, age {age:.0f}s/120s)", dim)
                kept.append(p)
            continue
        if age < min_hold_s:
            _log(say, "[loop]", f"hold {p['id']} (age {age:.0f}s < "
                 f"min-hold {min_hold_s:.0f}s, pnl {pnl:+.2f}$)", dim)
            kept.append(p)
            continue
        profit = pnl > take_profit_usd
        if profit or age > max_age_s:
            reason = (f"take-profit +${take_profit_usd:.2f}" if profit
                      else f"max-age {max_age_s:.0f}s")
            _log(say, "[loop]", f"close {p['id']} ({reason}, "
                 f"age {age:.0f}s, pnl {pnl:+.2f}$)", dim)
            close_and_report(fex, say, p["id"], reason=reason,
                             learner=learner, snap=snap, mode=mode,
                             swarm=swarm)
        else:
            _log(say, "[loop]", f"hold {p['id']} (age {age:.0f}s, "
                 f"pnl {pnl:+.2f}$ < +${take_profit_usd:.2f})", dim)
            kept.append(p)
    return kept


def run_cycle(*, say, a, base, news_mode, md, swarm, gen, critic,
              arb, fex, allow_order: bool = True, learner=None,
              margin_in_use: float = 0.0) -> dict:
    """Один цикл: snapshot → 60 голосов → news → Три Ума → execution."""
    model, llm_lines = llm_status()
    say(cyan(f"    [llm] Swarm active model: {model}"))
    for line in llm_lines:
        say(magenta(f"    {line}"))
    # «Слой 0» + портфель: скринер топ-5 → корреляция → ребалансировка.
    # Флэт → цикл спит, LLM-лимиты не жгутся.
    pick = None
    asset_symbol = a.symbol
    if a.live:
        try:
            top = get_top_assets(
                5, min_volatility=float(env("SCREEN_MIN_VOL") or 1.5),
                min_volume_ratio=float(env("SCREEN_MIN_VRATIO") or 1.0),
                flat_volatility=float(env("SCREEN_FLAT_VOL") or 1.0))
            top_err = None
        except Exception as e:                      # noqa: BLE001
            top, top_err = [], e
        if top_err is not None:
            _log(say, "[screener]", f"ошибка скринера: {top_err} — "
                 f"работаю на {a.symbol}", red)
        if not top and top_err is None:
            _log(say, "[screener]", "Flat market. Waiting...", gold)
            _persist("screener_skip", mode="live" if a.live else "demo",
                     symbol=a.symbol, reason="flat market")
            return {"symbol": a.symbol, "skipped": "flat_market"}
        if top:
            top = filter_uncorrelated(top)[:3]
            rebal = None
            if fex.live:
                try:
                    pos = [p for p in (list_positions(fex.key)
                                       .get("positions") or [])
                           if p.get("status") == "open"]
                    rebal = decide_rebalance(pos, top,
                                             equity_usd=EQUITY_USD)
                except Exception as e:                  # noqa: BLE001
                    _log(say, "[portfolio]",
                         f"positions feed error: {e}", red)
            if rebal is not None:
                if rebal["close"] or rebal["open"]:
                    parts = [f"CLOSE {p.get('symbol')}/USDT "
                             "(fell out of top)" for p in rebal["close"]]
                    parts += [f"OPEN {o['symbol']} (new opportunity)"
                              for o in rebal["open"]]
                    _log(say, "[portfolio]", "Rebalance: " +
                         ", ".join(parts), gold)
                    _persist("portfolio_rebalance",
                             mode="live" if a.live else "demo",
                             close=[p.get("id") for p in rebal["close"]],
                             open=[o["symbol"] for o in rebal["open"]])
                pct = (rebal["margin_used"] / rebal["budget"] * 100.0
                       if rebal["budget"] else 0.0)
                _log(say, "[portfolio]", "Total margin in use: "
                     f"${rebal['margin_used']:.0f}"
                     f"/${rebal['budget']:.0f} ({pct:.0f}%)", gold)
                for p in rebal["close"]:
                    try:
                        if str(p.get("id")) in _CLOSE_REQUESTED:
                            continue    # /close уже ушёл — MarginPad
                                        # ещё отдаёт позицию «open»
                        r = close_position(p["id"], fex.key)
                        _CLOSE_REQUESTED.add(str(p.get("id")))
                        _CHAOS_POSITIONS.discard(str(p.get("id")))
                        _log(say, "[execution]", "портфель: закрыта "
                             f"{p.get('id')} ({p.get('symbol')})", gold)
                        # MarginPad пишет итог в запись позиции, а не в
                        # ответ /close — перечитываем её, иначе в истории
                        # лежало бы закрытие с пустым PnL.
                        rec = _position_by_id(fex.key, str(p.get("id"))) or {}
                        _persist("position_closed", id=p.get("id"),
                                 symbol=p.get("symbol"),
                                 side=p.get("side"),
                                 margin_usd=p.get("margin_usd"),
                                 entry_price=p.get("entry_price"),
                                 reason="portfolio rebalance",
                                 mode="live" if a.live else "demo",
                                 **closed_trade_fields(p, r, rec))
                    except Exception as e:              # noqa: BLE001
                        _log(say, "[execution]", f"close {p.get('id')} "
                             f"failed: {e}", red)
            if rebal is not None and rebal["open"]:
                o = rebal["open"][0]
                pick = {"symbol": o["symbol"], "hot": True,
                        "volatility": o.get("volatility"),
                        "volume_ratio": 0.0}
            elif top:
                pick = top[0]
        if pick is not None:
            sym = str(pick["symbol"])
            md.symbol = sym
            base = sym.split("/")[0]
            asset_symbol = sym
            if hasattr(fex, "symbol"):
                fex.symbol = base
            vol = pick.get("volatility")
            vol_word = ("High" if (pick.get("volume_ratio") or 0) > 1
                        else "Low")
            vol_txt = (f"(Volatility: {vol:.1f}%, Volume: {vol_word})"
                       if vol is not None else "")
            if pick.get("hot"):
                _log(say, "[screener]", f"Best asset for trade: {sym} "
                     f"{vol_txt}", gold)
            else:
                _log(say, "[screener]", f"no hot asset — least boring: "
                     f"{sym} {vol_txt}", dim)
    try:
        snap = build_snapshot(a, base, md, say)
    except Exception as e:                      # noqa: BLE001
        # Транзиентный сбой фида (SSL/timeout к бирже) не должен
        # убивать движок — цикл пропускается, как flat market.
        _log(say, "[data]", f"snapshot недоступен: {e} — "
             "цикл пропущен, повтор после sleep", red)
        _persist("data_skip", mode="live" if a.live else "demo",
                 symbol=asset_symbol, reason=str(e))
        return {"symbol": asset_symbol, "skipped": "data_feed"}

    # News Engine v2.0 — аварийный сброс кэша по триггерам рынка:
    # ATR вырос > 50%, цена гэпнула > 2% → закэшированные новости протухли.
    prev_price = _PREV_CYCLE.get("price")
    cur_price = snap.get("price")
    atr_g = float(snap.get("atr_growth_pct") or 0.0)
    reset_why = None
    if atr_g > 50.0:
        reset_why = f"ATR spike +{atr_g:.0f}% за час"
    elif (prev_price and cur_price and
          abs(float(cur_price) - float(prev_price)) /
          float(prev_price) > 0.02):
        reset_why = (f"price gap {prev_price} → {cur_price} (>2%)")
    if reset_why:
        invalidate_cache(reset_why)
        _log(say, "[news]", f"⚡ Cache reset: {reset_why}", gold)
    _PREV_CYCLE.update({"price": cur_price})

    # Chaos Mode: ATR выросла > 100% за час — Арбитр слушает только
    # отряд шторма, исполнение переходит на микро-лоты.
    chaos = atr_g > CHAOS_ATR_GROWTH
    _log(say, "[mode]", "⚡ CHAOS" if chaos else "NORMAL",
         magenta if chaos else dim)
    if chaos:
        _log(say, "[chaos]", "Volatility detected. Switching to "
             "micro-execution.", magenta)

    # база знаний (knowledge_base.json) + уроки из прошлых убытков:
    # Learning Engine AI-2 → MIND → промпт агентов
    n_lessons = 0
    if learner is not None:
        task_text = learner.task_text(snap)
        learner.begin_cycle(task_text)
        applied = learner.apply_lessons(task_text)
        for line in learner.log_lines(applied):
            say(line)
        apply_lessons(swarm, applied.get("block") or "",
                      str(snap.get("regime") or ""), asset_symbol)
        n_lessons = len(applied.get("lessons") or [])
    else:
        apply_lessons(swarm, "", str(snap.get("regime") or ""), asset_symbol)
    if isinstance(swarm, AgentPool):
        # респавны цикла строят промпты под выбранный скринером актив
        swarm.set_context(
            symbol=asset_symbol,
            regime=str(snap.get("regime") or ""),
            lessons=((applied.get("block") or "")
                     if learner is not None else ""))
    _log(say, "[memory]", knowledge_summary(n_lessons), cyan)
    regime = str(snap.get("regime") or "")
    if regime == "VOLATILE":
        _log(say, "[regime]", "VOLATILE — паника: в промпт агентов добавлен "
             "запрет лонгов (только HOLD или шорт с малым плечом)", red)

    # сентимент считаем ДО голосования: правило базы знаний «не покупай,
    # если RSI > 75, а sentiment < -0.3» агент проверяет сам
    sent_score, sent_mode = None, "skip"
    asset_score = None
    if a.sentiment is not None:
        sent_score, sent_mode = a.sentiment, "override"
        _log(say, "[news]", f"sentiment {sent_score:+.2f} "
             f"(override — реальный фид подменён для демо)", magenta)
    elif news_mode == "demo":
        sent_score, sent_mode = DEMO_SENTIMENT, "demo-override"
        _log(say, "[news]", f"sentiment {sent_score:+.2f} "
             f"(demo-override — новости не опрашивались)", magenta)
    elif news_mode == "live":
        try:
            sent = get_market_sentiment("")     # глобальный фон (уровень 1)
            prev_s = _PREV_CYCLE.get("sentiment")
            if (prev_s is not None
                    and abs(sent.score - prev_s) > 0.5):
                why = (f"global sentiment {prev_s:+.2f} → "
                       f"{sent.score:+.2f}")
                invalidate_cache(why)
                _log(say, "[news]", f"⚡ Cache reset: {why}", gold)
                sent = get_market_sentiment("", use_cache=False)
            _PREV_CYCLE["sentiment"] = sent.score
            state = (f"cached {sent.age_mins}m ago" if sent.from_cache
                     else sent.mode)
            _log(say, "[news]", f"Global: {sent.score:+.2f} "
                 f"({sent.label}, {state}) — {sent.summary}", magenta)
            sent_score, sent_mode = sent.score, sent.mode
        except Exception as e:                  # noqa: BLE001
            _log(say, "[news]", f"feed error: {e} — neutral", dim)
            sent_score, sent_mode = 0.0, "error"
        try:                                    # уровень 2: актив портфеля
            asent = get_asset_sentiment(base)
            astate = (f"cached {asent.age_mins}m ago" if asent.from_cache
                      else asent.mode)
            _log(say, "[news]", f"{base}: {asent.score:+.2f} "
                 f"({asent.label}, {astate}) — {asent.summary}", magenta)
            asset_score = asent.score
        except Exception as e:                  # noqa: BLE001
            _log(say, "[news]", f"{base} asset-feed error: {e}", dim)
    if sent_score is not None:
        snap = {**snap, "sentiment_score": sent_score,
                "sentiment_mode": sent_mode,
                "market_sentiment": sent_score,
                "asset_sentiment": asset_score}

    votes = []
    meta: list | None = None          # мета-голоса парламента для Арбитра
    debate_veto = ""
    # Структура дебатов для истории (и для окна «Решение» в NUE): вердикты
    # мини-роев и дерево строк TUI. Вне парламента остаются пустыми.
    swarm_verdicts: list = []
    debate_tree: list[str] = []
    debate_models: dict = {}
    if isinstance(swarm, AgentPool):
        # Swarm Pool v3.0: голосуют только топ-active_n по весу;
        # 404/429 или вес < 0.1 — смерть слота и респавн
        swarm.cycle_reset()
        say(cyan(f"    {swarm.header_line()}"))
        dead: list[tuple] = []
        if getattr(a, "debate", True):
            # Иерархический парламент: 3 мини-роя × 4 агента, два
            # раунда дебатов; в шторм — дебаты отряда Chaos.
            # Блок каждого роя печатается по готовности (on_block), а
            # не пачкой в конце — ответы агентов видны в логе сразу.
            def _say_block(_name, _verdict, block):
                for ln in block:
                    say(cyan(f"    {ln}"))

            verdicts, dlines, r2votes, _models = run_parliament(
                swarm, snap, chaos=chaos, on_block=_say_block)
            swarm_verdicts = verdicts
            debate_tree = list(dlines)
            debate_models = dict(_models or {})
            by_name = {ag.name: ag for ag in swarm.agents}
            for v in r2votes:
                ag = by_name.get(v.agent)
                votes.append((v, getattr(ag, "reputation_score", 1.0)))
                if v.error_status in (404, 429) and ag is not None:
                    dead.append((ag, v.error_status))
            # в шторм мета-слой пропускается: голоса отряда идут в
            # Арбитр напрямую — у них настоящие CHAOS_PERSONAS
            meta = r2votes if chaos else meta_votes(verdicts)
            debate_veto = risk_veto(verdicts)
        else:
            roster = swarm.active()

            def _decide(ag):
                try:
                    return ag.decide(snap)
                except Exception as e:  # noqa: BLE001 — цикл не роняем
                    return SwarmVote(action="HOLD", confidence=0.0,
                                     reason=f"[error] decide: "
                                            f"{str(e)[:80]}",
                                     stop_loss=0.0, agent=ag.name,
                                     persona=ag.persona, mode="error",
                                     weight=ag.weight)

            # max_workers=5: агенты не долбят провайдеров разом —
            # защита от 429; as_completed — голос каждого агента
            # печатается сразу по приходу, а не блоком после последнего
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = {ex.submit(_decide, ag): ag for ag in roster}
                for i, fut in enumerate(as_completed(futs), 1):
                    ag = futs[fut]
                    try:
                        vote = fut.result()
                    except Exception as e:          # noqa: BLE001
                        vote = SwarmVote(
                            action="HOLD", confidence=0.0,
                            reason=f"[error] decide: {str(e)[:80]}",
                            stop_loss=0.0, agent=ag.name,
                            persona=ag.persona, mode="error",
                            weight=ag.weight)
                    votes.append((vote, ag.reputation_score))
                    say(_vote_line(i, len(roster), ag, vote))
                    if vote.error_status in (404, 429):
                        dead.append((ag, vote.error_status))
        for ag, status in dead:
            for line in swarm.kill_agent(
                    ag, f"http {status} from {ag.model_id or 'model'}"):
                say(gold(f"    {line}"))
        for ag in list(swarm.agents):
            if ag.weight < 0.1:
                for line in swarm.kill_agent(ag, "weight < 0.1"):
                    say(gold(f"    {line}"))
        say(cyan(f"    {swarm.status_line()}"))
    else:
        for i, ag in enumerate(swarm, 1):
            vote = ag.decide(snap)
            votes.append((vote, ag.reputation_score))
            say(_vote_line(i, len(swarm), ag, vote))
    t = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v, _ in votes:
        t[v.action] += 1
    _log(say, "[swarm]", f"{len(votes)} agents voted: "
         f"{t['BUY']} BUY, {t['SELL']} SELL, {t['HOLD']} HOLD.", cyan)

    # Dynamic Risk Profiling: профиль Арбитра по стрикам/режиму/фону;
    # ручной оверрайд из risk_override.json приоритетнее автоматики.
    global _CURRENT_PROFILE
    st = _risk_streaks()
    ov = _risk_override()
    if ov:
        arb.set_profile(ov, manual=True)
    else:
        arb.clear_manual()
        new = arb.update_profile(win_streak=st["win"],
                                 loss_streak=st["loss"],
                                 regime=regime,
                                 market_sentiment=sent_score)
        if new:
            icon = {"AGGRESSIVE": "🚀", "CONSERVATIVE": "🛡️"}.get(new, "⚖️")
            _log(say, "[risk]", f"{icon} Switched to {new} "
                 f"(Win streak: {st['win']}, Loss streak: {st['loss']}, "
                 f"Trend: {regime}). Threshold: {arb.threshold:.2f}, "
                 f"Size: {arb.max_size_pct:.1f}%",
                 green if new == "AGGRESSIVE"
                 else red if new == "CONSERVATIVE" else gold)
    _CURRENT_PROFILE = arb.risk_profile
    _log(say, "[risk]", f"Profile: {arb.risk_profile} | "
         f"Win Streak: {st['win']} | Loss Streak: {st['loss']}",
         green if arb.risk_profile == "AGGRESSIVE"
         else red if arb.risk_profile == "CONSERVATIVE" else gold)

    say(rule())
    proposal = gen.propose(votes)
    say(cyan(f"    ГЕНЕРАТОР │ {gen.speech(proposal)}"))
    objections = critic.review(
        proposal, rsi=snap["rsi"], sentiment_score=sent_score,
        sentiment_mode=sent_mode,
        tech_signal=("BUY" if snap["ema9_above_ema21"] else "SELL"))
    if not objections:
        say(gold("    КРИТИК    │ возражений нет — все довольны"))
    for ob in objections:
        say((red if ob.blocking else gold)(
            f"    КРИТИК    │ {ob.text}"))
    # Арбитр считает score сам: Σ (confidence × weight) — вес агента
    # падает на убытке и растёт на прибыли. В парламенте — те же веса,
    # но голосуют вердикты мини-роев, а не 12 агентов поодиночке.
    raw_votes = meta if meta is not None else [v for v, _ in votes]
    con = arb.rule(proposal, objections, votes=raw_votes,
                   atr_growth=atr_g)
    pre_veto: dict | None = None
    if debate_veto:
        # Risk Swarm HOLD > 0.7 — абсолютное вето Мета-Арбитра,
        # выше любых профилей и консенсуса
        pre_veto = {"action": con.action, "score": con.score,
                    "approved": con.approved}
        con = Consensus("HOLD", con.score, con.tally, 0.0, 0.0,
                        debate_veto, cancelled=True)
    say(green(f"    АРБИТР    │ {con.speech()}"))
    if debate_veto:
        _log(say, "[arbiter]",
             f"⚖️ {debate_veto} · {_veto_impact(pre_veto)}", red)    # Критик — жёсткие абсолютные лимиты поверх профиля: портфельная
    # маржа ≤20% equity, стоп не шире 1.5% — агрессия их не обходит
    try:
        eq_now = float(get_balance(fex.key).get("equity_usd")
                       or EQUITY_USD)
    except Exception:                       # noqa: BLE001
        eq_now = EQUITY_USD
    for note in critic.harden(con, equity_usd=eq_now,
                              margin_in_use_usd=margin_in_use):
        _log(say, "[critic]", note, red)
    if con.dead:
        _log(say, "[arbiter]", "мёртвые агенты (голос проигнорирован): "
             + ", ".join(con.dead), gold)
    _log(say, "[darwin]", f"score = Σ(conf × weight): {con.score:+.3f} · "
         f"масса голосов {arb.weight_mass(raw_votes):.2f} · "
         f"средний вес {sum(v.weight for v in raw_votes) / max(1, len(raw_votes)):.2f}",
         cyan)
    say(rule())

    order_status = "rejected"
    exec_res: dict | None = None
    if con.approved and getattr(a, "no_execute", False):
        order_status = "skipped:no_execute"
        _log(say, "[execution]", "--no-execute: голосование завершено — "
             "ордер пропущен (тест роя, позиции не открываются)", dim)
    elif con.approved and not allow_order:
        order_status = "blocked:risk_limit"
        _log(say, "[risk]", f"вердикт {con.action} одобрен, но лимит "
             "позиций исчерпан — ордер заблокирован Risk Manager", red)
    elif con.approved:
        _account_line(fex, say)
        _log(say, "[execution]", f"вердикт APPROVED {con.action} → "
             f"MarginPad order…", gold)
        res = arb.execute(con, fex, emit=say, equity_usd=EQUITY_USD,
                          rsi=snap["rsi"], sentiment_score=sent_score,
                          sentiment_mode=sent_mode,
                          tech_signal=("BUY" if snap["ema9_above_ema21"]
                                       else "SELL"))
        exec_res = res
        order_status = str(res.get("status") or "unknown")
        if res.get("status") not in ("filled", "dry_run"):
            _log(say, "[execution]",
                 f"{res.get('status')}: "
                 f"{res.get('reason', res.get('reasons'))}", red)
        elif res.get("status") == "filled":
            say(rule("═"))
            say(green(f"  MARGINPAD POSITION OPENED — id {res.get('id')}"))
            # сделка в истории: панель «Портфель» показывает активные
            # треды даже после того, как MarginPad отдал пустой список
            pos = ((res.get("order") or {}).get("position") or {})
            if con.risk_mode == "MICRO":
                # Chaos Mode: позиция живёт по своим выходам —
                # TP +0.8% / SL -0.5% / time-exit 120с (manage_positions)
                _CHAOS_POSITIONS.add(str(res.get("id")))
            _persist("position_opened", id=res.get("id"),
                     symbol=snap.get("symbol"), side=res.get("side"),
                     margin_usd=pos.get("margin_usd"),
                     leverage=fex.leverage,
                     entry_price=pos.get("entry_price"),
                     risk_mode=con.risk_mode,
                     action=con.action, mode="live" if a.live else "demo")
            # кто голосовал за исполненную сторону — их веса пересчитаются
            # при закрытии позиции (в том числе в loop-режиме)
            voters = [v.agent for v, _ in votes if v.action == con.action]
            _POSITION_VOTERS[str(res.get("id"))] = voters
            if a.hold or a.loop:
                _log(say, "[execution]", "позиция держится — loop "
                     "закроет её по PnL/возрасту", gold)
            else:
                _log(say, "[execution]", f"авто-закрытие через "
                     f"{a.autoclose_delay:.0f}s — ждём обновления PnL…",
                     dim)
                close_and_report(fex, say, res["id"],
                                 delay_s=a.autoclose_delay,
                                 reason="auto-close", learner=learner,
                                 snap=snap,
                                 personas=voters,
                                 mode="live" if a.live else "demo",
                                 swarm=swarm)
                _account_line(fex, say)
    else:
        _log(say, "[execution]", "ордер не создан — вердикт "
             f"{con.action} ({con.reason})", dim)
    _persist("cycle", mode="live" if a.live else "demo",
             symbol=snap["symbol"], price=snap["price"],
             rsi=snap["rsi"], macd_hist=snap.get("macd_hist"),
             sentiment=sent_score, sentiment_mode=sent_mode,
             votes={"buy": sum(1 for v, _ in votes if v.action == "BUY"),
                    "sell": sum(1 for v, _ in votes if v.action == "SELL"),
                    "hold": sum(1 for v, _ in votes if v.action == "HOLD"),
                    "count": len(votes)},
             votes_detail=[{"agent": v.agent, "persona": v.persona,
                            "action": v.action,
                            "confidence": v.confidence,
                            "weight": v.weight, "mode": v.mode}
                           for v, _ in votes],
             arbiter={"action": con.action, "approved": con.approved,
                      "score": con.score, "reason": con.reason},
             debate=_debate_record(swarm_verdicts, debate_tree, debate_veto,
                                   debate_models, pre_veto),
             risk_mode=con.risk_mode,
             order=order_status, execution=exec_res)
    return snap


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--margin", type=float, default=100.0)
    p.add_argument("--leverage", type=int, default=3)
    p.add_argument("--live", action="store_true",
                   help="реальные свечи Binance + реальные новости "
                        "(сейчас рынок в панике — гейты отменят сделку)")
    p.add_argument("--news", choices=["live", "demo", "skip"], default=None,
                   help="источник оценки новостей (по умолчанию: demo "
                        "для демо-сценария, live для --live)")
    p.add_argument("--sentiment", type=float, default=None,
                   help="переопределить оценку новостей (помечается)")
    p.add_argument("--loop", action="store_true",
                   help="автономный режим: цикл каждые --interval сек")
    p.add_argument("--interval", type=float, default=60.0,
                   help="пауза между циклами в loop-режиме, сек")
    p.add_argument("--autoclose-delay", type=float, default=8.0,
                   help="ждать N сек перед авто-закрытием позиции "
                        "(PnL у MarginPad обновляется с задержкой)")
    p.add_argument("--hold", action="store_true",
                   help="не закрывать позицию автоматически — маржа "
                        "остаётся занятой до следующего цикла")
    p.add_argument("--max-cycles", type=int, default=0,
                   help="0 = бесконечно; >0 — прогнать N циклов и "
                        "закрыть всё (для теста)")
    p.add_argument("--max-positions", type=int, default=3,
                   help="жёсткий лимит одновременно открытых позиций")
    p.add_argument("--max-margin", type=float, default=500.0,
                   help="лимит margin_in_use в $ — сверх него новые "
                        "ордера блокируются")
    p.add_argument("--max-age", type=float, default=300.0,
                   help="возраст позиции в секундах, после которого "
                        "она закрывается принудительно")
    p.add_argument("--min-hold", type=float, default=30.0,
                   help="минимальное время удержания позиции, сек — "
                        "раньше не закрываем, даём PnL раскачаться")
    p.add_argument("--take-profit", type=float, default=1.2,
                   help="порог unrealized PnL для profit-take, $ — "
                        "выше комиссии закрытия (~$0.57), иначе "
                        "realized уходит в минус")
    p.add_argument("--agents", type=int, default=12,
                   help="размер роя голосующих агентов (legacy-режим — "
                        "фиксированный рой без пула)")
    p.add_argument("--pool-size", type=int, default=100,
                   help="слотов в AgentPool (по умолчанию пул включён: "
                        "100 слотов, голосуют топ-30 по весу)")
    p.add_argument("--active", type=int, default=12,
                   help="сколько агентов пула голосует за цикл "
                        "(ThreadPoolExecutor ×5 — защита от 429)")
    p.add_argument("--no-debate", dest="debate", action="store_false",
                   default=True,
                   help="плоское голосование топ-12 вместо иерархического "
                        "парламента (3 мини-роя × 4 агента, два раунда "
                        "дебатов, мета-голоса Арбитру)")
    p.add_argument("--no-execute", action="store_true",
                   help="прогнать рой и Три Ума без реального ордера — "
                        "позиции не открываются (тест голосования)")
    a = p.parse_args()
    base = a.symbol.split("/")[0]
    news_mode = a.news or ("live" if a.live else "demo")

    say = slow_print
    for ln in logo_lines():
        say(ln)
    say(rule("═"))
    say("  NEXUS GRAND TRADING — swarm → minds → MarginPad")
    say(rule("═"))
    scenario = "LIVE (реальные данные)" if a.live else \
        "DEMO (синтетические индикаторы, реальная цена)"
    _log(say, "[scenario]", scenario + (" + LOOP" if a.loop else ""),
         magenta)

    md = MarketData(symbol=a.symbol)
    gen_models = LLMFactory.role_models("generator")
    if "--agents" in sys.argv:
        # legacy-режим: фиксированный рой без пула и респавна
        swarm = make_swarm(a.agents, reputation=load_reputation(),
                           gen_models=gen_models or None, symbol=a.symbol)
    else:
        swarm = AgentPool(a.pool_size, a.active,
                          gen_models=gen_models or None,
                          reputation=load_reputation(), symbol=a.symbol)
    learner = TradingLearning(corpus_dir=RUNS_DIR / "traj",
                              lessons_path=RUNS_DIR / "lessons.json",
                              symbol=a.symbol)
    for issue in learner.issues:
        _log(say, "[learning]", issue, red)
    gen, critic, arb = Generator(), Critic(), Arbiter()
    fex = FundingExecutor(margin_usd=a.margin, leverage=a.leverage)
    _log(say, "[swarm]", f"Initializing {len(swarm)} autonomous "
         f"trading agents... connected to MarginPad. Актив берёт скринер, "
         f"а не фиксированный {a.symbol}.", cyan)
    if isinstance(swarm, AgentPool):
        _log(say, "[personas]", swarm.persona_line(), dim)
    _log(say, "[swarm]", f"feed={'binance-public' if a.live else 'demo'} "
         f"exec={'marginpad' if fex.live else 'dry-run'} "
         f"guard=2%/5%/shadow×6", dim)
    line = swarm_llm_line(swarm)
    if line:
        say(cyan(f"    {line}"))
    if a.no_execute:
        say(dim("    [execution] --no-execute: тест голосования — "
                "ордера отключены"))

    last_snap: dict | None = None
    try:
        if not a.loop:
            last_snap = run_cycle(say=say, a=a, base=base, news_mode=news_mode,
                                  md=md, swarm=swarm, gen=gen, critic=critic,
                                  arb=arb, fex=fex, learner=learner)
        else:
            _log(say, "[swarm]", f"Автономный режим запущен. "
                 f"Интервал: {a.interval:.0f}с. "
                 f"Стоп: Ctrl+C или флаг {LOOP_STOP_FLAG.name}.", cyan)
            _log(say, "[risk]", f"лимиты: ≤{a.max_positions} позиций, "
                 f"margin_in_use < ${a.max_margin:,.0f}, "
                 f"min-hold {a.min_hold:.0f}с, "
                 f"take-profit +${a.take_profit:.2f}, "
                 f"max-age {a.max_age:.0f}с — Risk Manager", magenta)
            try:  # SIGTERM → тот же emergency close, что и Ctrl+C
                import signal
                signal.signal(
                    signal.SIGTERM,
                    lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
            except (ValueError, OSError, AttributeError):
                pass
            _log(say, "[loop]", f"Интервал: {a.interval:.0f}с. "
                 f"Квота: ~{a.active} запросов/мин.", cyan)
            it = 0
            while a.max_cycles <= 0 or it < a.max_cycles:
                if stop_requested():
                    _log(say, "[loop]", "стоп по флагу движка — выхожу "
                         "на штатный circuit breaker", cyan)
                    break
                it += 1
                say(rule())
                _log(say, "[loop]", f"итерация {it} — проверяю "
                     "открытые позиции (profit-take / max-age)", dim)
                open_ps = manage_positions(fex, say,
                                           max_age_s=a.max_age,
                                           min_hold_s=a.min_hold,
                                           take_profit_usd=a.take_profit,
                                           learner=learner, snap=last_snap,
                                           mode="live" if a.live else "demo",
                                           swarm=swarm)
                in_use = sum(float(p.get("margin_usd") or 0)
                             for p in open_ps)
                allow = (len(open_ps) < a.max_positions
                         and in_use < a.max_margin)
                if allow:
                    _log(say, "[risk]", f"slots "
                         f"{len(open_ps)}/{a.max_positions} · in use "
                         f"${in_use:,.2f}/${a.max_margin:,.0f} — "
                         "ордер разрешён", dim)
                else:
                    _log(say, "[risk]", f"LIMIT: "
                         f"{len(open_ps)}/{a.max_positions} позиций, "
                         f"in use ${in_use:,.2f} — новые ордера "
                         "заблокированы", red)
                last_snap = run_cycle(say=say, a=a, base=base,
                                      news_mode=news_mode, md=md, swarm=swarm,
                                      gen=gen, critic=critic, arb=arb, fex=fex,
                                      allow_order=allow, learner=learner,
                                      margin_in_use=in_use)
                if a.max_cycles > 0 and it >= a.max_cycles:
                    break
                _log(say, "[loop]", f"sleep {a.interval:.0f}s "
                     "(Ctrl+C или флаг стопа — circuit breaker)", dim)
                if sleep_or_stop(a.interval):
                    _log(say, "[loop]", "стоп по флагу движка — выхожу "
                         "на штатный circuit breaker", cyan)
                    break
            say(red("\n    [safety] loop завершён — circuit breaker: "
                    "закрываю открытые позиции"))
            close_all_positions(fex, say, learner=learner, snap=last_snap,
                                mode="live" if a.live else "demo",
                                swarm=swarm)
    except KeyboardInterrupt:
        say(red("\n    [safety] Ctrl+C — circuit breaker: "
                "закрываю открытые позиции"))
        close_all_positions(fex, say)
        return 0
    except Exception as e:                # noqa: BLE001
        say(red(f"\n    [fatal] {e}"))
        return 1

    say(rule("═"))
    say(green("  GRAND CYCLE COMPLETE — exit 0"))
    say(rule("═"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
