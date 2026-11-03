"""NEXUS Paper Trading Demo — аукцион торговых стратегий + paper-портфель.

Run:  python -m tools.trading_agent_demo [--mock] [--wait N]
      python tools/trading_agent_demo.py

Цепочка (та же архитектура, что у grand demo — аукцион → работа → Intent):
  1. Менеджер выставляет аукцион «управление капиталом 1000 USDT»:
     стратегии momentum / mean_reversion / sentiment / conservative
     котируют комиссию и risk-adjusted score; quant_fund ломится
     с подделанной чексуммой → отсекается верификацией.
  2. Победитель (max score при fee ≤ бюджета) торгует на РЕАЛЬНЫХ ценах
     Binance public API (ключей не нужно, ордера никуда не уходят —
     виртуальный портфель). Без сети — MockFeed, режим честно помечен.
  3. Risk controls через LoopGuard: позиция ≤ 2% equity, stop-loss −1.5%,
     ≤ 5 сделок за цикл, halt при drawdown > 5%; rogue-стратегия,
     пробующая transfer_near напрямую, получает DENIED от политики.
  4. Комиссия победителю — NEAR Intent 0.001 NEAR (testnet при ключах,
     stub без) — paper-торговля тоже проходит через NEAR Intents.

Все суммы виртуальные, реальные только цены и testnet-чек комиссии.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer

try:
    from ansi_demo import stage, gold, cyan, green, magenta, link, red, \
        dim, rule, colorize_log
except ImportError:
    from tools.ansi_demo import stage, gold, cyan, green, magenta, link, \
        red, dim, rule, colorize_log

ARCHITECT = "architect"
ARCHITECT_ACCOUNT = "architect.near"
TRADING_ROLE = "trading-desk"
RISK_ROLE = "risk-manager"
TASK_ID = "trading-auction-1"
SKILL = "trading"

BUDGET_NEAR = 0.005          # макс. комиссия за цикл
START_USDT = 1000.0          # стартовый виртуальный баланс
MAX_POSITION_PCT = 0.02      # ≤ 2% equity на сделку
STOP_LOSS_PCT = 0.015        # −1.5% → принудительное закрытие
MAX_TRADES = 5               # ≤ 5 сделок за цикл
MAX_DRAWDOWN_PCT = 0.05      # drawdown > 5% → halt
DEFAULT_WAIT_S = 6           # пауза между снимками цен (live)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "NEARUSDT", "SOLUSDT", "DOGEUSDT")

# «Исторические» метрики стратегий для скоринга аукциона: score = edge/risk.
# quant_fund — forged bid: заоблачный score, но чексумма чужая.
_STRATEGY_STATS = {
    "momentum":       {"edge": 1.50, "risk": 0.80, "fee": 0.001},
    "mean_reversion": {"edge": 1.10, "risk": 0.60, "fee": 0.0008},
    "sentiment":      {"edge": 1.60, "risk": 0.95, "fee": 0.0015},
    "conservative":   {"edge": 0.75, "risk": 0.50, "fee": 0.0005},
}


def sub_account(worker: str) -> str:
    return f"{worker}.{ARCHITECT_ACCOUNT}"


def _bid_checksum(worker: str, task_id: str, fee: float, score: float) -> str:
    return hashlib.sha256(
        f"{worker}:{task_id}:{fee}:{score}".encode()).hexdigest()[:16]


def make_bid(worker: str, task_id: str) -> dict:
    st = _STRATEGY_STATS[worker]
    score = round(st["edge"] / st["risk"], 3)
    return {"worker": worker, "sub_account": sub_account(worker),
            "skill": SKILL, "task_id": task_id, "fee": st["fee"],
            "score": score,
            "checksum": _bid_checksum(worker, task_id, st["fee"], score)}


def forged_bid(task_id: str) -> dict:
    """quant_fund: score 999 (слишком хорошо, чтобы быть правдой) — но
    чексумма подделана, верификация отсекает независимо от score."""
    bid = {"worker": "quant_fund", "sub_account": sub_account("quant_fund"),
           "skill": SKILL, "task_id": task_id, "fee": 0.0001,
           "score": 999.0, "checksum": "f" * 16}
    return bid


def verify_bid(bid: dict, task: dict) -> str | None:
    """None если bid валиден, иначе причина отказа."""
    if bid.get("skill") != task["skill"]:
        return "skill mismatch"
    if bid.get("task_id") != task["task_id"]:
        return "task_id mismatch"
    if bid.get("sub_account") != sub_account(bid.get("worker", "")):
        return "sub-account shape"
    expected = _bid_checksum(bid.get("worker", ""), task["task_id"],
                             bid.get("fee", -1), bid.get("score", -1))
    if bid.get("checksum") != expected:
        return "checksum mismatch"
    if bid.get("fee", 0) <= 0 or bid["fee"] > task["budget"]:
        return "over budget"
    if not (0 < bid.get("score", 0) < 10):
        return "implausible score"
    return None


# ------------------------------------------------------------- feeds

class BinanceFeed:
    """Реальные котировки Binance public API — без ключей, без ордеров."""
    name = "binance"
    URL = "https://api.binance.com/api/v3/ticker/24hr"

    def snapshot(self, symbols) -> dict:
        """24hr-тикер по каждому символу (batch ?symbols=[...] капризен
        к кодировке массива — отдаёт 400; per-symbol надёжен)."""
        out = {}
        for s in symbols:
            with urllib.request.urlopen(
                    f"{self.URL}?symbol={s}", timeout=15) as resp:
                d = json.loads(resp.read().decode())
            out[d["symbol"]] = {"price": float(d["lastPrice"]),
                                "change_pct": float(d["priceChangePercent"])}
        return out


class MockFeed:
    """Детерминированные цены для офлайн-режима и тестов. Режим честно
    помечается stub_prices — live-котировки не имитируются."""

    name = "mock"
    DEFAULT_TICKS = (
        {"BTCUSDT": (67543.20,  2.40), "ETHUSDT": (3120.55, -1.80),
         "NEARUSDT": (4.82,      5.10), "SOLUSDT": (142.10, -3.20),
         "DOGEUSDT": (0.1210,    0.60)},
        {"BTCUSDT": (67810.00,  2.80), "ETHUSDT": (3095.10, -2.60),
         "NEARUSDT": (4.91,      7.00), "SOLUSDT": (141.30, -3.80),
         "DOGEUSDT": (0.1215,    1.00)},
    )

    def __init__(self, ticks=None):
        self._ticks = tuple(ticks) if ticks else self.DEFAULT_TICKS
        self._i = 0

    def snapshot(self, symbols) -> dict:
        t = self._ticks[min(self._i, len(self._ticks) - 1)]
        self._i += 1
        return {s: {"price": p, "change_pct": c}
                for s, (p, c) in t.items() if s in symbols}


def make_feed(say, force_mock: bool = False):
    """Live-фид при доступности Binance, иначе честный mock-фолбэк."""
    if force_mock:
        say(colorize_log("[feed] --mock: mock-цены (mode=stub_prices)"))
        return MockFeed()
    feed = BinanceFeed()
    try:
        feed.snapshot(("BTCUSDT",))
        say(colorize_log(
            "[feed] Binance public API — реальные котировки (mode=live_prices)"))
        return feed
    except Exception as e:  # noqa: BLE001 — сырой сбой виден судьям
        say(colorize_log(
            f"[feed] Binance недоступен ({e}) → mock-цены (mode=stub_prices)"))
        return MockFeed()


# ---------------------------------------------------------- strategies

def decide(strategy: str, snapshot: dict, equity: float) -> list[dict]:
    """Сигналы стратегии → намерения BUY (quote в USDT). Продажи в демо
    только защитные (stop-loss) — горизонт одного цикла."""
    cap = equity * MAX_POSITION_PCT
    ranked = sorted(snapshot.items(),
                    key=lambda kv: kv[1]["change_pct"], reverse=True)
    if strategy == "momentum":
        picks = [s for s, d in ranked if d["change_pct"] >= 0.8]
        quote = cap
    elif strategy == "mean_reversion":
        picks = [s for s, d in reversed(ranked) if d["change_pct"] <= -0.8]
        quote = cap
    elif strategy == "sentiment":
        picks = [s for s, d in sorted(
            snapshot.items(), key=lambda kv: abs(kv[1]["change_pct"]),
            reverse=True) if abs(d["change_pct"]) >= 1.5]
        quote = cap
    else:  # conservative — один самый спокойный актив, половина лимита
        s = min(snapshot.items(), key=lambda kv: abs(kv[1]["change_pct"]))
        picks, quote = [s[0]], cap / 2
    return [{"symbol": s, "side": "BUY", "quote": quote}
            for s in picks[:MAX_TRADES]]


# ------------------------------------------------------------ trading

def _pnl(v: float, fmt: str = "+.2f") -> str:
    s = format(v, fmt)
    return green(s) if v >= 0 else red(s)


def _cell(v: float, width: int, fmt: str = "+.2f", suffix: str = "") -> str:
    """Ячейка таблицы: паддинг до ширины ДО раскраски — ANSI-escapes не
    ломают выравнивание колонок."""
    s = f"{v:{fmt}}{suffix}".rjust(width)
    return green(s) if v >= 0 else red(s)


def run_cycle(strategy: str, feed, say, guard: LoopGuard,
              wait_s: float = 0.0) -> dict:
    """Один торговый цикл: снимок → сигналы → лимиты → вход → mark-to-market
    → stop-loss / drawdown-halt. Возвращает позиции, сделки и equity."""
    mode = "live_prices" if feed.name == "binance" else "stub_prices"
    equity = START_USDT
    t0 = feed.snapshot(SYMBOLS)
    say(colorize_log(
        f"[feed] снимок t0 ({mode}): " + ", ".join(
            f"{s}={t0[s]['price']}({t0[s]['change_pct']:+.1f}%)"
            for s in SYMBOLS if s in t0)))

    signals = decide(strategy, t0, equity)
    guard.require("risk_check", RISK_ROLE)  # гард пишется в аудит каждый цикл
    if not signals:
        say(dim("    сигналов нет — стратегия в кэше"))
        return {"positions": [], "trades": 0, "equity": equity,
                "cash": equity, "mode": mode, "halted": False}
    cash, positions, trades = equity, [], 0
    for sig in signals:
        if trades >= MAX_TRADES:
            guard.check("enforce_limits", RISK_ROLE)
            say(colorize_log(
                f"[guard] max trades ({MAX_TRADES}/цикл) — сигналы отклонены"))
            break
        quote = min(sig["quote"], equity * MAX_POSITION_PCT)
        price = t0[sig["symbol"]]["price"]
        qty = quote / price
        cash -= quote
        positions.append({"symbol": sig["symbol"], "qty": qty,
                          "entry": price, "closed": False})
        trades += 1
        say(colorize_log(
            f"[trade] BUY {qty:.6f} {sig['symbol']} @ {price:,.4f} "
            f"— {quote:.2f} USDT"))

    if wait_s > 0:
        say(dim(f"    ...держим {wait_s:.0f}s, ждём t1"))
        time.sleep(wait_s)
    t1 = feed.snapshot(SYMBOLS)

    halted = False
    for pos in positions:
        cur = t1.get(pos["symbol"], {}).get("price", pos["entry"])
        pos["current"], pos["pnl"] = cur, (cur - pos["entry"]) * pos["qty"]
        pos["pnl_pct"] = (cur - pos["entry"]) / pos["entry"] * 100
        if not pos["closed"] and pos["pnl_pct"] <= -STOP_LOSS_PCT * 100:
            guard.check("enforce_limits", RISK_ROLE)
            say(colorize_log(
                f"[guard] stop-loss {pos['symbol']}: "
                f"{pos['pnl_pct']:+.2f}% ≤ −{STOP_LOSS_PCT * 100}% — закрыта"))
            cash += pos["qty"] * cur
            pos["closed"] = True
            trades += 1
            say(colorize_log(
                f"[trade] SELL {pos['qty']:.6f} {pos['symbol']} "
                f"@ {cur:,.4f} — stop-loss"))

    equity_now = cash + sum(p["qty"] * p["current"]
                            for p in positions if not p["closed"])
    drawdown = (START_USDT - equity_now) / START_USDT
    if drawdown > MAX_DRAWDOWN_PCT:
        guard.require("halt", RISK_ROLE)
        halted = True
        say(colorize_log(
            f"[guard] halt: drawdown limit "
            f"({drawdown * 100:.2f}% > {MAX_DRAWDOWN_PCT * 100}%)"))

    return {"positions": positions, "trades": trades,
            "equity": equity_now, "cash": cash, "mode": mode,
            "halted": halted}


def _portfolio_table(cycle: dict, strategy: str) -> list[str]:
    W = 66
    lines = [rule("═"),
             f"  ПОРТФЕЛЬ — стратегия {strategy} ({cycle['mode']})",
             rule(),
             f"  {'Актив':<10}{'Qty':>12}{'Entry':>12}{'Текущая':>12}"
             f"{'PnL $':>9}{'PnL%':>9}{'Вес%':>7}",
             rule()]
    for p in cycle["positions"]:
        weight = (0.0 if p["closed"]
                  else p["qty"] * p["current"] / cycle["equity"] * 100)
        lines.append(
            f"  {p['symbol']:<10}{p['qty']:>12.6f}{p['entry']:>12,.4f}"
            f"{p['current']:>12,.4f}"
            f"{_cell(p['pnl'], 9)}{_cell(p['pnl_pct'], 9, suffix='%')}"
            f"{weight:>6.1f}%" + (dim(" [closed]") if p["closed"] else ""))
    lines.append(rule())
    pnl = cycle["equity"] - START_USDT
    lines.append(
        f"  Equity: {cycle['equity']:,.2f} USDT | PnL цикла: "
        f"{_pnl(pnl)} USDT ({_pnl(pnl / START_USDT * 100, '+.2f')}%)"
        + (red("  [HALTED]") if cycle["halted"] else ""))
    lines.append(rule("═"))
    return lines


# ------------------------------------------------------------- demo

def run_demo(feed=None, wait_s: float = 0.0, emit=print,
             force_mock: bool = False) -> dict:
    """Полный прогон: аукцион стратегий → торговый цикл → комиссия-интент
    → отчёт. emit — строковый лог (None для тихого режима)."""
    def say(line: str) -> None:
        if emit is not None:
            emit(line)

    guard = LoopGuard()
    # Intent+чек — stub-режим payer'а: комиссия стратегии идёт НЕ через
    # эскроу-контракт, а прямым transfer'ом на NEAR_RECEIVER_ID (ниже).
    payer = NearIntentPayer(loop_guard=guard, payer_role=TRADING_ROLE,
                            verbose=False, mode="stub")

    say(rule("═"))
    say("  NEXUS PAPER TRADING — аукцион стратегий + виртуальный портфель")
    say(rule("═"))
    say("")
    say(stage(f"[1/5] Менеджер объявил аукцион капитала {START_USDT:.0f} USDT "
              f"— бюджет комиссии {BUDGET_NEAR} NEAR"))

    guard.require("decompose", ARCHITECT)
    task = {"task_id": TASK_ID, "skill": SKILL, "budget": BUDGET_NEAR}
    say(gold(f"    [auction] задача {TASK_ID} — skill={SKILL}, "
             f"комиссия ≤ {BUDGET_NEAR} NEAR"))

    say("")
    say(stage("[2/5] Торг стратегий (fee + risk-adjusted score):"))
    say(rule())
    guard.require("hire", ARCHITECT)
    bids = [make_bid(w, TASK_ID) for w in
            ("momentum", "mean_reversion", "sentiment", "conservative")]
    bids.append(forged_bid(TASK_ID))
    rejected, verified = [], []
    for bid in bids:
        reason = verify_bid(bid, task)
        if reason is None:
            verified.append(bid)
            say(cyan(f"    [bid] {bid['worker']:<15} fee={bid['fee']:.4f} "
                     f"score={bid['score']:.2f} — verified"))
        else:
            rejected.append({"worker": bid["worker"], "reason": reason})
            say(red(f"    🛡️  [bid] {bid['worker']:<15} fee={bid['fee']:.4f} "
                    f"score={bid['score']:.2f} — REJECTED ({reason})"))

    guard.require("verify", ARCHITECT)
    winner = max(verified, key=lambda b: b["score"]) if verified else None
    if winner is None:
        say(red("    [auction] ни одного верифицированного bid — цикл отменён"))
        return {"bids": bids, "rejected_bids": rejected, "winner": None,
                "payment": None, "report": None,
                "audit_log": guard.audit_log}
    say(green(f"    🏆 [winner] {winner['worker']} — score={winner['score']:.2f} "
              f"fee={winner['fee']:.4f} NEAR"))
    say(rule())

    # rogue-beat: проигравший пытается вывести деньги напрямую, в обход
    # Intents — политика отвечает DENIED, запись остаётся в audit_log.
    deny = guard.check("transfer_near", TRADING_ROLE)
    if deny["verdict"] == "denied":
        say(red("    🛡️  [guard] DENIED: 'transfer_near' напрямую — "
                "платежи только через NEAR Intents"))

    say("")
    say(stage(f"[3/5] Торговый цикл ({winner['worker']}):"))
    say(rule())
    feed = feed or make_feed(say, force_mock)
    cycle = run_cycle(winner["worker"], feed, say, guard, wait_s)
    say(rule())

    say("")
    say(stage("[4/5] Портфель:"))
    for ln in _portfolio_table(cycle, winner["worker"]):
        say(ln)

    say("")
    say(magenta("[5/5] NEAR Intent — комиссия победителю:"))
    say(magenta("    Submitting intent to NEAR Solvers network..."))
    receipt = payer.pay(winner["sub_account"], TASK_ID, winner["fee"],
                        service="paper_trading")

    # Реальная комиссия 0.001 NEAR: прямой testnet-transfer на
    # NEAR_RECEIVER_ID (эскроу-задачи у трейдера нет — это tip, не escrow).
    try:
        from near_contract_client import resolve_private_key
        from near_rpc import NearTestnetClient, EXPLORER_TX
        from research_agent_clients import env as _cenv
    except ImportError:
        from tools.near_contract_client import resolve_private_key
        from tools.near_rpc import NearTestnetClient, EXPLORER_TX
        from tools.research_agent_clients import env as _cenv
    sender, receiver = _cenv("NEAR_ACCOUNT_ID"), _cenv("NEAR_RECEIVER_ID")
    if sender and receiver:
        try:
            key = resolve_private_key(sender, "manager")
            txh = NearTestnetClient().send_transfer(
                sender, receiver, int(0.001 * 10**24), key)
            receipt.update(tx_hash=txh, explorer=EXPLORER_TX + txh,
                           mode="testnet", commission_tx=True,
                           receiver=receiver)
        except Exception as e:  # noqa: BLE001 — сбой сети виден в чеке
            receipt["note"] = f"commission tx failed: {e}"
    say(magenta("    " + json.dumps(receipt["intent"], ensure_ascii=False,
                                    indent=2).replace("\n", "\n    ")))
    if receipt.get("tx_hash"):
        say("    🪙 " + link(f"TxHash: {receipt['tx_hash']}"))
        say("      " + link(f"Explorer: {receipt.get('explorer', '')}"))
    else:
        reason = (receipt.get("error") or receipt.get("note")
                  or receipt["status"])
        say(dim(f"    status={receipt['status']} mode={receipt['mode']} — "
                f"{reason}"))

    report = {
        "strategy": winner["worker"], "mode": cycle["mode"],
        "equity": round(cycle["equity"], 4),
        "pnl_usdt": round(cycle["equity"] - START_USDT, 4),
        "pnl_pct": round((cycle["equity"] - START_USDT) / START_USDT * 100, 3),
        "trades": cycle["trades"], "halted": cycle["halted"],
        "positions": [
            {"symbol": p["symbol"], "qty": round(p["qty"], 8),
             "entry": p["entry"], "current": p["current"],
             "pnl": round(p["pnl"], 4), "closed": p["closed"]}
            for p in cycle["positions"]],
    }
    say("")
    say(stage("Отчёт (JSON):"))
    say(dim(json.dumps(report, ensure_ascii=False, indent=2)))
    say("")
    audit = " → ".join(e["command"] for e in guard.audit_log)
    say(f"audit_log: {dim(audit)}")
    say(rule("═"))

    return {"bids": bids, "rejected_bids": rejected, "winner": winner,
            "cycle": cycle, "payment": receipt, "report": report,
            "audit_log": guard.audit_log}


def main() -> dict:
    args = sys.argv[1:]
    wait = DEFAULT_WAIT_S
    force_mock = "--mock" in args
    for i, a in enumerate(args):
        if a == "--wait" and i + 1 < len(args):
            wait = float(args[i + 1])
    return run_demo(wait_s=wait, force_mock=force_mock)


if __name__ == "__main__":
    main()
