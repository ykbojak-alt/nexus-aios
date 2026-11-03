"""NEXUS Grand Demo — экономика агентного роя on-chain.

Run: python -m tools.hackathon_grand_demo ["question"]

Один прогон:
  задача → create_task (эскроу 0.05 NEAR) → submit_bid research-agent
  (валидная ed25519-подпись) → submit_bid freelancer (сломанная подпись,
  контракт отвергает) → accept_bid → работа агента → submit_work →
  verify_and_release (контракт платит агенту) → финальный ответ.

Все TxHash в логе — вызовы nexus-escrow.yksunder.testnet. Прямого
transfer'а и fallback'а нет. Финальный ответ печатается полностью, без
обрезки цитат.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_guard import LoopGuard
from near_intent_payer import NearIntentPayer
import orchestrator_demo

try:
    from ansi_demo import stage, green, magenta, link, red, dim, \
        rule, colorize_log, slow_print, logo_lines
except ImportError:
    from tools.ansi_demo import stage, green, magenta, link, red, dim, \
        rule, colorize_log, slow_print, logo_lines

BANNER = "═" * 66
BUDGET_NEAR = orchestrator_demo.DEFAULT_BUDGET_NEAR


def _indent_json(obj: dict, pad: int = 4) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2).replace(
        "\n", "\n" + " " * pad)


def _await_payout(payer: NearIntentPayer, account_id: str,
                  before: float, min_delta: float,
                  timeout_s: float = 30.0) -> float:
    """Ждёт promise-перевод verify_and_release: broadcast_tx_commit
    подтверждает транзакцию, а выплата приходит следующим receipt.
    Ждём именно приход (min_delta), а не любое изменение баланса —
    газ за bid/submit_work меняет баланс ещё до settlement'а."""
    deadline = time.time() + timeout_s
    current = payer.balance_near(account_id)
    while (current - before) < min_delta and time.time() < deadline:
        time.sleep(1.0)
        current = payer.balance_near(account_id)
    return current


async def main(query: str, emit=slow_print, agent=None,
               payer: NearIntentPayer | None = None) -> dict:
    def say(line: str) -> None:
        if emit is not None:
            emit(line)

    for ln in logo_lines():
        say(ln)
    guard = LoopGuard()
    payer = payer or NearIntentPayer(loop_guard=guard, verbose=False)
    contract_mode = payer.mode == "contract"
    agent_account = (payer.contract.agent_account if contract_mode
                     else "research-bot.testnet")
    manager_account = (payer.contract.manager_account if contract_mode
                       else "architect.near")
    before_agent = payer.balance_near(agent_account) if contract_mode else None
    before_manager = payer.balance_near(manager_account) if contract_mode else None

    say(rule("═"))
    say("  NEXUS GRAND DEMO — on-chain эскроу: аукцион + NEAR Intents")
    say(rule("═"))
    say("")
    say(stage(f"[1/6] Менеджер получил задачу: {query!r}"))
    say(dim(f"    manager={manager_account}  agent={agent_account}  "
            f"budget={BUDGET_NEAR:.3f} NEAR"))
    if contract_mode:
        say(dim(f"    contract={payer.contract.contract_id} "
                f"(strict, fallback отключён)"))

    auction = await orchestrator_demo.run_auction(
        query, budget=BUDGET_NEAR, payer=payer, guard=guard,
        agent=agent, emit=None)

    say("")
    say(stage("[2/6] Контракт и аукцион (live log):"))
    say(rule())
    for line in auction["auction_log"]:
        say(colorize_log(line))
    say(rule())

    winner = auction["winner"]
    if winner is None:
        say(red("[3/6] Победителя нет — верифицированных bids не нашлось."))
        return {"auction": auction, "payment": None, "answer": None,
                "verified": False, "contract_receipts":
                    auction.get("contract_receipts", [])}
    out = auction["output"] or {}
    say("")
    say(stage(f"[3/6] Победитель {winner['worker']} "
              f"({winner.get('account') or 'local'}) выполняет работу"))
    say(dim(f"    mode={out.get('mode', '—')} "
            f"sources={len(out.get('sources', []))} "
            f"verified={bool(out.get('verified'))}"))
    url_status = out.get("url_status") or {}
    for url, code in url_status.items():
        color = green if code == 200 else red
        say(dim("    URL ") + color(f"{code}") + dim(f" {url}"))

    receipts = auction.get("payment_receipts") or []
    receipt = receipts[0] if receipts else None
    say("")
    say(stage("[4/6] Сдача работы и расчёт через контракт:"))
    if receipt is None:
        say(red("    release не выполнен: ответ не верифицирован."))
    else:
        intent = receipt.get("intent", {})
        say(magenta("    NEAR Intent:"))
        say(magenta("    " + _indent_json(intent)))
        for call in receipt.get("contract_calls", []):
            say(dim(f"    {call.get('step')}: ")
                + link(f"TxHash: {call.get('tx_hash', '—')}"))
            if call.get("explorer"):
                say(dim("      ") + link(f"Explorer: {call['explorer']}"))

    say("")
    say(stage("[5/6] TxHash вызовов контракта:"))
    if auction.get("contract_receipts"):
        for call in auction["contract_receipts"]:
            status = call.get("status")
            mark = "✓" if status == "success" else (
                "↺" if status == "already_registered" else "✗")
            if status == "already_registered":
                stake = call.get("stake_near")
                stake_txt = (f"stake={stake:.6f} NEAR"
                             if isinstance(stake, (int, float)) else "")
                line = (f"    {mark} {call.get('step', '?'):<20} "
                        f"already registered  {stake_txt}")
            else:
                line = (f"    {mark} {call.get('step', '?'):<20} "
                        f"{call.get('tx_hash', '—')}")
            if status == "failed":
                line += f"  ({call.get('error', '')})"
            say(green(line) if status != "failed" else red(line))
    else:
        say(dim("    offline stub — контракт не вызывался "
                "(нет NEAR_ACCOUNT_ID/NEAR_ESCROW_CONTRACT)"))

    if contract_mode:
        settled = bool(receipt and receipt.get("status") == "settled")
        min_delta = winner["price"] * 0.5 if settled else 0.0
        after_agent = _await_payout(payer, agent_account, before_agent,
                                    min_delta)
        after_manager = payer.balance_near(manager_account)
        say("")
        say(stage("[6/6] Балансы (liquid, NEAR):"))
        say(f"    {agent_account:<22} {before_agent:.6f} → "
            f"{after_agent:.6f}  ({after_agent - before_agent:+.6f})")
        say(f"    {manager_account:<22} {before_manager:.6f} → "
            f"{after_manager:.6f}  ({after_manager - before_manager:+.6f})")
        # v2: репутация агента и прозрачные счётчики эскроу
        info = payer.get_agent(agent_account) or {}
        if isinstance(info, dict):
            say(dim(f"    репутация {agent_account}: "
                    f"completed={info.get('tasks_completed')} "
                    f"failed={info.get('tasks_failed')} "
                    f"active={info.get('active')}"))
        escrow_now = payer.contract.get_escrow_balance()
        burned = payer.contract.get_burned_yocto()
        say(dim(f"    escrow={escrow_now / 10**24:.6f} NEAR  "
                f"burned={burned / 10**24:.6f} NEAR  (v2 counters)"))
    else:
        after_agent = after_manager = None
        say("")
        say(stage("[6/6] Балансы: offline stub — сеть не трогалась"))

    answer = out.get("summary") or "(ответа нет)"
    verified = bool(out.get("verified"))
    say("")
    say(rule("═"))
    say(green("  ФИНАЛЬНЫЙ ОТВЕТ") if verified
        else red("  ФИНАЛЬНЫЙ ОТВЕТ (не верифицирован)"))
    say(rule())
    # Перенос, а не обрезка: textwrap.fill. Длинные URL не ломаем
    # (break_long_words=False) — [Source: https://...] остаётся целым.
    for paragraph in (answer.splitlines() or [answer]):
        if not paragraph.strip():
            say("")
            continue
        say(dim(textwrap.fill(
            paragraph, width=76,
            initial_indent="  │ ", subsequent_indent="  │ ",
            break_long_words=False, break_on_hyphens=False)))
    if out.get("sources"):
        say(rule())
        say(dim("  Источники:"))
        for s in out["sources"]:
            say(dim(f"    - {s}"))
    say(rule("═"))
    status = green(f"verified={verified}") if verified else red(
        f"verified={verified}")
    say(f"{status} | audit_log: "
        + dim(" → ".join(e["command"] for e in auction["audit_log"])))
    say(rule("═"))

    return {
        "auction": {
            "task": auction["task"], "bids": auction["bids"],
            "rejected_bids": auction["rejected_bids"],
            "winner": winner, "audit_log": auction["audit_log"],
            "contract": auction.get("contract"),
        },
        "output": out, "payment": receipt, "answer": answer,
        "verified": verified,
        "contract_receipts": auction.get("contract_receipts", []),
        "balances": ({"agent_before": before_agent,
                      "agent_after": after_agent,
                      "manager_before": before_manager,
                      "manager_after": after_manager}
                     if contract_mode else None),
    }


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What are the key features of NEAR Protocol?"
    asyncio.run(main(q))
