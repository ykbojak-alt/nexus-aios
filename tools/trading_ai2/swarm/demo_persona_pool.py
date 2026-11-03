"""demo_persona_pool.py — доказательство масштабирования до 100 агентов.

Показывает на реальном коде:
  · 100 уникальных персон (семья × характер) — имена и промпты;
  · пул из 100 агентов: у каждого свой промпт, стартовый вес 1.0;
  · веса реально меняют score Арбитра;
  · вето-сила (вес > 2.5 → ×3), мёртвые агенты (< 0.1 три раза),
    тяжёлое большинство (70% top-30 HOLD → сделка отменяется).

Скрипт работает офлайн: без ключей и без сети.

    python -m tools.trading_ai2.swarm.demo_persona_pool
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.trading_ai2.swarm import darwin                      # noqa: E402
from tools.trading_ai2.swarm.agent_swarm import (               # noqa: E402
    AgentPool, SwarmVote)
from tools.trading_ai2.swarm.arbiter import Arbiter             # noqa: E402
from tools.trading_ai2.swarm.epoch_rewarder import REP_PATH     # noqa: E402
from tools.trading_ai2.swarm.generator import Proposal           # noqa: E402
from tools.trading_ai2.swarm.persona_generator import (         # noqa: E402
    generate_personas)


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def vote(action: str, agent: str, weight: float, conf: float = 0.9):
    return SwarmVote(action=action, confidence=conf, reason="x",
                     stop_loss=0.0, agent=agent, weight=weight)


def main() -> int:
    # ------------------------------------------------ 1. генератор персон
    head("1) ГЕНЕРАТОР ПЕРСОН: 100 УНИКАЛЬНЫХ ЛИЧНОСТЕЙ")
    variants = generate_personas(100)
    names = {v.name for v in variants}
    prompts = {v.prompt for v in variants}
    fams: dict[str, int] = {}
    for v in variants:
        fams[v.family] = fams.get(v.family, 0) + 1
    print(f"всего персон: {len(variants)} · уникальных имён: {len(names)} · "
          f"уникальных промптов: {len(prompts)}")
    print(f"семей: {len(fams)} · агентов на семью: "
          f"{min(fams.values())}–{max(fams.values())}")
    print()
    print("первые 12 имён (пример):")
    for i, v in enumerate(variants[:12], 1):
        print(f"  {i:2d}. {v.name:<34} семейство: {v.family:<16} "
              f"риск: {v.risk:<7} стоп: {v.stop_pct:.1f}%")
    print()
    print("раскладка по семействам:")
    for fam, count in fams.items():
        print(f"  {fam:<18} {count}")
    print()
    print("уникальный системный промпт агента (хвост — характер):")
    agent = variants[0]
    print("  " + agent.prompt[-220:])

    # ---------------------------------------------------- 2. пул агентов
    head("2) ПУЛ ИЗ 100 АГЕНТОВ")
    pool = AgentPool(pool_size=100, active_n=30, seed=42)
    print(f"слотов: {len(pool)} · уникальных имён: "
          f"{len({a.name for a in pool.agents})} · уникальных промптов: "
          f"{len({a.system_prompt for a in pool.agents})}")
    print(f"стартовый вес у всех агентов: "
          f"{sorted({a.weight for a in pool.agents})}")
    print(f"голосуют за цикл: {len(pool.active())} (топ по весу)")
    print(pool.persona_line(6))
    try:
        rep = json.loads(Path(REP_PATH).read_text(encoding="utf-8"))
        print(f"репутация ({REP_PATH.name}): {len(rep)} записей, "
              f"новые агенты стартуют с {sorted(set(rep.values()))}")
    except OSError:
        print("репутация: файл не создан")

    # ------------------------------------------ 3. веса влияют на score
    head("3) ВЕСА РЕАЛЬНО МЕНЯЮТ ГОЛОСОВАНИЕ АРБИТРА")
    print("одни и те же голоса, разный вес у BUY-агента:")
    for weight in (1.0, 1.5, 2.0):
        votes = [vote("BUY", "buyer", weight), vote("HOLD", "risk", 1.0)]
        print(f"  BUY (вес {weight:.1f}) + HOLD (вес 1.0) → "
              f"score {Arbiter.score_votes(votes):+.3f} "
              f"· масса {Arbiter.weight_mass(votes):.2f}")
    buy = vote("BUY", "buyer", 1.0)
    hold = vote("HOLD", "risk", 1.0)
    before = Arbiter.score_votes([buy, hold])
    new_w = darwin.update_weight(buy.weight, -12.0)
    buy.weight = new_w
    after = Arbiter.score_votes([buy, hold])
    print(f"  убыток BUY-агента: вес 1.00 → {new_w:.2f} → "
          f"score {before:+.3f} → {after:+.3f} (голос слабее)")

    # ---------------------------------------------------- 4. вето-сила
    head("4) ВЕТО-СИЛА: ВЕС > 2.5 → ГОЛОС ГОВОРИТ ВТРОЕ ГРОМЧЕ")
    print(f"эффективный вес: 2.5 → {Arbiter.effective_weight(2.5):.1f} · "
          f"2.6 → {Arbiter.effective_weight(2.6):.1f} · "
          f"3.0 → {Arbiter.effective_weight(3.0):.1f}")
    heavy = vote("BUY", "heavy", 2.6)
    light = vote("SELL", "light", 1.0)
    score_heavy = Arbiter.score_votes([heavy, light])
    heavy.weight = 2.5
    score_plain = Arbiter.score_votes([heavy, light])
    print(f"BUY (2.6) против SELL (1.0): score {score_heavy:+.3f} "
          f"с вето-силой · {score_plain:+.3f} без неё")

    # -------------------------------------------- 5. мёртвый агент
    head("5) МЁРТВЫЙ АГЕНТ: ВЕС < 0.1 ТРИ ГОЛОСА ПОДРЯД → ГОЛОС ИГНОРИРОВАН")
    arb = Arbiter()
    ghost = vote("BUY", "ghost", 0.05, conf=1.0)
    live = vote("HOLD", "live", 1.0)
    proposal = Proposal("BUY", 0.0, {"BUY": 1, "HOLD": 1}, 1.5, 1.0, "t")
    for i in (1, 2, 3):
        con = arb.rule(proposal, votes=[ghost, live])
        print(f"  голос {i}: мёртвых={con.dead or '—'} · "
              f"score {con.score:+.3f} · вердикт {con.action}")

    # -------------------------------- 6. тяжёлое большинство HOLD
    head("6) ТЯЖЁЛОЕ БОЛЬШИНСТВО: 70% TOP-30 ПО ВЕСУ HOLD → СДЕЛКА ОТМЕНЕНА")
    votes = [vote("HOLD", f"h{i}", 3.0 - i * 0.01) for i in range(21)]
    votes += [vote("BUY", f"b{i}", 0.5) for i in range(19)]
    con = Arbiter().rule(proposal, votes=votes)
    print(f"  21 HOLD (вес ~3.0) + 19 BUY (вес 0.5), голосов 40")
    print(f"  вердикт: {con.action} · отменено: {con.cancelled}")
    print(f"  причина: {con.reason}")
    print()
    print("  правило закреплено в базе знаний:")
    from tools.trading_ai2.swarm import knowledge as kb
    for rule in kb.load_rules():
        if "top-30" in rule:
            print(f"  - {rule}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
