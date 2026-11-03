#!/usr/bin/env python3
"""Analytics Engine — разбор logs/trading_history.jsonl и эволюция роя.

Метрики по ночному прогону: циклы, вердикты Арбитра, отмены Критика
(по причинам), точность голосов каждой персоны (совпал ли её сигнал
с фактическим движением рынка через горизонт --horizon-min) и итоговый
реализованный PnL. На основе точности обновляет swarm/reputation.json:
серия 5 верных → +0.2 (макс 3.0), серия 5 ошибок → -0.2 (мин 0.1);
HOLD, упустивший рост (missed_profit), считается ошибкой эпизода.

Запуск:
  python -m tools.trading_ai2.swarm.analyze_history [--history PATH]
      [--horizon-min 5] [--no-write]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "tools"))

try:
    from ansi_demo import gold, cyan, green, magenta, red, dim, rule
except ImportError:  # pragma: no cover — запуск как пакет
    from tools.ansi_demo import gold, cyan, green, magenta, red, dim, rule

HISTORY = _REPO / "logs" / "trading_history.jsonl"
REP_PATH = Path(__file__).resolve().parent / "reputation.json"

#: «Плоский» рынок: движение меньше этого — ни сигнал, ни упущение.
FLAT_EPS_PCT = 0.05
#: Серия эпизодов для эволюции веса.
STREAK_N = 5
W_STEP, W_MIN, W_MAX = 0.2, 0.1, 3.0


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [r for r in out if isinstance(r, dict)]


def _ts(rec: dict) -> float:
    try:
        return datetime.fromisoformat(rec["ts"]).timestamp()
    except (KeyError, ValueError):
        return 0.0


def _critic_kind(reason: str) -> str:
    r = reason.lower()
    if "новост" in r or "sentiment" in r:
        return "новости"
    if "atr" in r or "волатильн" in r or "паник" in r:
        return "волатильность/ATR"
    if "лимит" in r or "позиц" in r or "марж" in r:
        return "лимиты риска"
    return "прочее"


def _proposal_dir(reason: str) -> str:
    """Направление отменённой Критиком сделки из текста причины."""
    if "против продажи" in reason:
        return "SELL"
    if "против покупки" in reason:
        return "BUY"
    return ""


def _future_price(cycles: list[dict], i: int, horizon_s: float) -> float | None:
    """Цена первого цикла не раньше горизонта; иначе — последняя известная."""
    t0 = _ts(cycles[i])
    fut = None
    for rec in cycles[i + 1:]:
        fut = rec
        if _ts(rec) - t0 >= horizon_s:
            return float(rec.get("price") or 0)
    if fut is not None:
        return float(fut.get("price") or 0)
    return None


def _episode(action: str, move_pct: float) -> str:
    """Вердикт эпизода для одного голоса против фактического хода рынка."""
    up, down = move_pct > FLAT_EPS_PCT, move_pct < -FLAT_EPS_PCT
    if action == "BUY":
        return "correct" if up else "wrong" if down else "neutral"
    if action == "SELL":
        return "correct" if down else "wrong" if up else "neutral"
    # HOLD: флэт — нейтрально; падение — увернулись; рост — упустили прибыль
    if down:
        return "correct"
    if up:
        return "missed_profit"
    return "neutral"


def analyze(cycles: list[dict], horizon_s: float) -> dict:
    arbiter = Counter()
    critic = Counter()
    saves = 0
    per: dict[str, Counter] = {}
    signals: dict[str, list[str]] = {}

    for i, rec in enumerate(cycles):
        arb = rec.get("arbiter") or {}
        action = str(arb.get("action") or "HOLD")
        arbiter[action] += 1
        reason = str(arb.get("reason") or "")
        if "отменено Критиком" in reason or "ОТМЕНА" == action:
            critic[_critic_kind(reason)] += 1
            prop = _proposal_dir(reason)
            fut = _future_price(cycles, i, horizon_s)
            if prop and fut and float(rec.get("price") or 0):
                move = (fut - float(rec["price"])) / float(rec["price"]) * 100
                if (prop == "BUY" and move < -FLAT_EPS_PCT) or \
                        (prop == "SELL" and move > FLAT_EPS_PCT):
                    saves += 1                     # отмена спасла от убытка

        detail = rec.get("votes_detail") or []
        if detail and float(rec.get("price") or 0):
            fut = _future_price(cycles, i, horizon_s)
            if fut:
                move = (fut - float(rec["price"])) / float(rec["price"]) * 100
                for v in detail:
                    name = str(v.get("agent") or v.get("persona") or "?")
                    ep = _episode(str(v.get("action") or "HOLD"), move)
                    per.setdefault(name, Counter())[ep] += 1
                    if ep != "neutral":
                        signals.setdefault(name, []).append(
                            "correct" if ep == "correct" else "wrong")

    return {"arbiter": arbiter, "critic": critic, "saves": saves,
            "per": per, "signals": signals}


def evolve(signals: dict[str, list[str]], rep: dict) -> dict[str, dict]:
    """Серии верных/ошибочных → сдвиг веса. Возвращает diff по агентам."""
    changes: dict[str, dict] = {}
    for name, seq in signals.items():
        w = float(rep.get(name, 1.0))
        streak = 0
        for s in seq:
            streak = streak + 1 if s == "correct" else streak - 1
            if streak >= STREAK_N:
                w = min(W_MAX, round(w + W_STEP, 3))
                streak = 0
            elif streak <= -STREAK_N:
                w = max(W_MIN, round(w - W_STEP, 3))
                streak = 0
        rep[name] = w
        changes[name] = {"weight": w, "streak": streak}
    return changes


def _acc(c: Counter) -> float:
    good = c.get("correct", 0)
    bad = c.get("wrong", 0) + c.get("missed_profit", 0)
    return good / max(1, good + bad)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(HISTORY))
    ap.add_argument("--horizon-min", type=float, default=5.0,
                    help="горизонт оценки сигнала, минут (дефолт 5)")
    ap.add_argument("--no-write", action="store_true",
                    help="только отчёт — reputation.json не трогать")
    a = ap.parse_args()

    recs = _load(Path(a.history))
    cycles = [r for r in recs if r.get("event") == "cycle"]
    closes = [r for r in recs if r.get("event") == "position_closed"]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in closes)

    print(gold("═══ NEXUS ANALYTICS — разбор ночного прогона ═══"))
    print(dim(f"history: {a.history} · записей {len(recs)} · "
              f"горизонт {a.horizon_min:.0f} мин"))
    print(rule())
    print(cyan(f"  Total Cycles      : {len(cycles)}"))
    print(cyan(f"  Position closes   : {len(closes)} · realized PnL "
               f"{'+' if pnl >= 0 else ''}{pnl:.2f}$"))

    st = analyze(cycles, a.horizon_min * 60)
    arb = st["arbiter"]
    print(rule())
    print(gold("  ARBITER ACTIONS"))
    for act in ("BUY", "SELL", "HOLD"):
        n = arb.get(act, 0)
        col = green if act == "BUY" else red if act == "SELL" else dim
        print(col(f"    {act:4s}: {n:4d}  "
                  f"({n / max(1, len(cycles)) * 100:.0f}%)"))
    crit = st["critic"]
    print(rule())
    print(gold("  CRITIC OVERRULES"))
    if crit:
        for kind, n in crit.most_common():
            print(red(f"    {kind:20s}: {n}"))
        print(green(f"    → сделок спасено от убытка: {st['saves']}"))
    else:
        print(dim("    отмен не было"))

    print(rule())
    print(gold("  PERSONA PERFORMANCE"))
    per = st["per"]
    if not per:
        print(dim("    нет votes_detail — прогоны до добавления "
                  "детализации голосов; дождись новых циклов"))
        return 0
    rows = sorted(per.items(), key=lambda kv: _acc(kv[1]), reverse=True)
    print(dim(f"    {'агент':22s} {'верно':>5s} {'ошибка':>6s} "
              f"{'упущено':>7s} {'acc':>6s}"))
    for name, c in rows:
        acc = _acc(c)
        col = green if acc >= 0.6 else red if acc <= 0.4 else cyan
        print(col(f"    {name:22s} {c.get('correct', 0):5d} "
                  f"{c.get('wrong', 0):6d} {c.get('missed_profit', 0):7d} "
                  f"{acc * 100:5.0f}%"))

    print(rule())
    print(gold("  TOP-3 ЛУЧШИХ"))
    for name, c in rows[:3]:
        print(green(f"    {name:22s} acc {_acc(c) * 100:.0f}%"))
    print(gold("  TOP-3 ХУДШИХ"))
    for name, c in rows[-3:]:
        print(red(f"    {name:22s} acc {_acc(c) * 100:.0f}%"))

    # --- эволюция: репутация по сериям эпизодов ----------------------
    rep: dict = {}
    if REP_PATH.exists():
        try:
            rep = json.loads(REP_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rep = {}
    changes = evolve(st["signals"], rep)
    moved = {n: ch for n, ch in changes.items()
             if abs(ch["weight"] - 1.0) > 1e-9}
    print(rule())
    print(gold("  DARWIN UPDATE — reputation.json"))
    if not moved:
        print(dim("    сдвигов нет — серий по 5 пока не набежало"))
    else:
        for name, ch in sorted(moved.items(),
                               key=lambda kv: -kv[1]["weight"]):
            print(magenta(f"    {name:22s} → weight {ch['weight']:.1f}"))
    if not a.no_write:
        REP_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2)
                            + "\n", encoding="utf-8")
        print(dim(f"    записано: {REP_PATH}"))
    else:
        print(dim("    --no-write: файл не тронут"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
