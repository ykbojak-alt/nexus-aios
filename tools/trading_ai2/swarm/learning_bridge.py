"""learning_bridge.py — мост между торговым роем и готовыми механизмами AI-2.

Нового движка памяти и обучения здесь нет: всё нужное уже есть в AI-2
(Learning Engine, MIND, траектории schema 0.1, файл уроков ai2.lessons/1,
память проекта). Мост только подключает торговый цикл к этим механизмам.

Цепочка знаний:

  1. цикл рынка → снимок (цена, RSI, тренд, настроения);
  2. убыточная сделка → событие ``outcome`` в траектории AI-2 с процессным
     фактом ``trade_loss:<условия входа>`` (тот же формат, что у остальных
     прогонов AI-2);
  3. исход сделки (PnL + контекст рынка) → память AI-2: POST /v1/memory,
     проект «trading»;
  4. майнинг AI-2 (``ai2.learning.mine``) по корпусу траекторий → паттерны →
     уроки (``ai2.learning.lessons``);
  5. уроки → файл уроков ``ai2.lessons/1`` (``ai2.learning.store``);
  6. следующий цикл: ``ai2.learning.engine.learn()`` отбирает уроки под
     текущие условия рынка и кладёт их в MIND как PROBABLY_KNOW
     (источник «learning») и пишет событие ``learning``;
  7. отобранные уроки уходят в системный промпт агентов роя.

Если пакета ai2 нет или хост AI-2 не поднят — мост деградирует честно: уроки не
применяются, причина попадает в issues и в лог, торговый цикл продолжает
работать. Торговля не зависит от обучения — обучение зависит от торговли.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import darwin
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm import darwin

#: Каталог с исходниками AI-2: пакет ai2 лежит внутри него
AI2_HOME_ENV = "AI2_HOME"
DEFAULT_AI2_HOME = r"D:\ai2_pilot\ai2"
#: Путь к файлу уроков — тот же, что у AI-2 (Learning Engine читает его отсюда)
LESSONS_ENV = "AI2_LESSONS_PATH"
#: Хост AI-2: там живёт память проекта. Адрес тот же, что у NEXUS TUI.
API_URL_ENV = "NEXUS_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8123"
#: Проект в памяти AI-2, куда пишутся исходы сделок
MEMORY_PROJECT = "trading"
#: Таймаут HTTP-записи в память AI-2, сек
MEMORY_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class _AI2:
    """Готовые механизмы AI-2, к которым подключается торговый цикл."""

    learn: Any
    render_lessons: Any
    mine_paths: Any
    save_lessons: Any
    Knowledge: Any
    TaskState: Any
    Trajectory: Any


def load_ai2() -> tuple[_AI2 | None, str]:
    """Подключить пакет ai2. None — с честной причиной (пакет не найден/не импортируется)."""
    home = (os.environ.get(AI2_HOME_ENV) or DEFAULT_AI2_HOME).strip()
    if home and home not in sys.path:
        candidate = Path(home)
        if (candidate / "ai2" / "__init__.py").is_file():
            sys.path.insert(0, str(candidate))
    try:
        from ai2.learning.engine import learn
        from ai2.learning.lessons import render_lessons
        from ai2.learning.mine import mine_paths
        from ai2.learning.store import save_lessons
        from ai2.mind.state import Knowledge, TaskState
        from ai2.runtime.trajectory import Trajectory
    except Exception as e:  # noqa: BLE001 — мост деградирует, а не падает
        return None, f"AI-2 недоступен: {type(e).__name__}: {e}"
    return _AI2(learn=learn, render_lessons=render_lessons, mine_paths=mine_paths,
                save_lessons=save_lessons, Knowledge=Knowledge, TaskState=TaskState,
                Trajectory=Trajectory), ""


class TradingLearning:
    """Учёба роя на своих убытках через механизмы AI-2.

    corpus_dir — папка траекторий (schema 0.1) одного корпуса прогонов;
    lessons_path — файл уроков ai2.lessons/1, который читает Learning Engine.
    """

    def __init__(self, *, corpus_dir: str | Path, lessons_path: str | Path | None = None,
                 api_url: str | None = None, symbol: str = "BTC/USDT"):
        self.ai2, self.ai2_error = load_ai2()
        self.corpus_dir = Path(corpus_dir)
        self.lessons_path = Path(lessons_path) if lessons_path else \
            self.corpus_dir.parent / "lessons.json"
        self.api_url = (api_url or os.environ.get(API_URL_ENV) or DEFAULT_API_URL).rstrip("/")
        self.symbol = symbol
        self.issues: list[str] = []
        self.state = None            # MIND текущего цикла (ai2.mind.state.TaskState)
        self.traj = None              # траектория текущего цикла (schema 0.1)
        self.task = ""                # текст задачи цикла (его читает Learning Engine)
        if self.ai2_error:
            self.issues.append(self.ai2_error)
        # Learning Engine читает файл уроков по этому пути — штатный способ AI-2
        os.environ[LESSONS_ENV] = str(self.lessons_path)

    # -------------------------------------------------------------- условия

    def conditions(self, snap: dict) -> dict[str, Any]:
        """Канонические условия рынка: токены, по которым отбирается урок."""
        rsi = float(snap.get("rsi") or 0.0)
        band = ("rsi_oversold" if rsi < 30 else
                "rsi_overbought" if rsi > 70 else "rsi_neutral")
        trend_up = bool(snap.get("ema9_above_ema21"))
        direction = "buy" if trend_up else "sell"
        sent = snap.get("sentiment_score")
        if isinstance(sent, (int, float)) and not isinstance(sent, bool):
            mood = ("sentiment_euphoria" if sent > 0.6 else
                    "sentiment_negative" if sent < -0.3 else "sentiment_neutral")
        else:
            mood = "sentiment_unknown"
        return {"rsi": rsi, "band": band, "direction": direction,
                "trend": "trend_up" if trend_up else "trend_down", "mood": mood,
                "price": float(snap.get("price") or 0.0),
                "sentiment": sent if isinstance(sent, (int, float)) else None}

    def task_text(self, snap: dict) -> str:
        """Текст задачи цикла: рынок и направление, по которым MIND отбирает урок."""
        c = self.conditions(snap)
        return (f"торговая задача: {self.symbol}; направление {c['direction']}; "
                f"условия {c['band']} {c['trend']} {c['mood']}; "
                f"цена {c['price']:.2f}; RSI {c['rsi']:.1f}")

    # ----------------------------------------------------------- траектория

    def begin_cycle(self, task: str) -> Any:
        """Открыть прогон цикла: траектория AI-2 (schema 0.1) + MIND."""
        if not self.ai2:
            return None
        self.task = task
        try:
            self.corpus_dir.mkdir(parents=True, exist_ok=True)
            run_id = f"trading-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            self.traj = self.ai2.Trajectory(path=self.corpus_dir / f"{run_id}.jsonl",
                                            run_id=run_id)
            self.traj.emit("run_start", schema="0.1", task_id=task,
                           evaluator_hash="", constitution_hash="")
            self.traj.emit("config", name="trading", models={"swarm": "60 агентов"})
            self.state = self.ai2.TaskState(goal=task)
        except Exception as e:  # noqa: BLE001 — траектория не должна ронять цикл
            self.issues.append(f"траектория не открыта: {type(e).__name__}: {e}")
            self.traj = None
            self.state = None
        return self.traj

    # ------------------------------------------------------------ применение

    def apply_lessons(self, task: str) -> dict[str, Any]:
        """Уроки из файла AI-2 → MIND (PROBABLY_KNOW) → блок в промпт агентов.

        Работает Learning Engine AI-2 целиком: отбор под задачу, запись в MIND
        и событие «learning» в траектории.
        """
        if not self.ai2:
            return {"lessons": [], "block": "", "issues": list(self.issues)}
        self.task = task
        if self.state is None:
            self.state = self.ai2.TaskState(goal=task)
        if self.traj is None:
            # траектория на диске не открылась — событие «learning» всё равно нужно
            self.traj = self.ai2.Trajectory()
        before = len(self.traj.events) if self.traj is not None else 0
        try:
            lessons = self.ai2.learn(self)
        except Exception as e:  # noqa: BLE001 — обучение не должно ронять торговлю
            self.issues.append(f"уроки не применены: {type(e).__name__}: {e}")
            return {"lessons": [], "block": "", "issues": list(self.issues)}
        event = None
        if self.traj is not None and len(self.traj.events) > before:
            event = self.traj.events[-1]
        issues = list(event.get("issues") or []) if isinstance(event, dict) else []
        block = self.ai2.render_lessons(lessons) if lessons else ""
        return {"lessons": lessons, "block": block, "issues": issues, "event": event}

    # -------------------------------------------------------- дарвиновские веса

    def apply_weights(self, swarm, personas: list[str] | None,
                      pnl_usd: float) -> list[str]:
        """Пересчитать веса агентов, голосовавших за исполненную сторону.

        Убыток → вес падает, прибыль → растёт. Возвращает строки лога
        «[learning] Agent TrendFollower-01 weight: 1.00 -> 0.80 (wrong
        prediction)» (в TUI и в журнал swarm/weights.log).

        Механизм свой у роя: работает и без AI-2, потому чтоLearning
        Engine может быть недоступен, а веса должны честно меняться.
        """
        if swarm is None or not personas:
            return []
        pnl = float(pnl_usd or 0.0)
        try:
            changed = darwin.apply_outcome(swarm, personas, pnl)
        except Exception as e:  # noqa: BLE001 — обучение не роняет торговлю
            return [f"[learning] веса агентов не пересчитаны: "
                    f"{type(e).__name__}: {e}"]
        return [darwin.weight_line(name, info["old"], info["new"], pnl)
                for name, info in changed.items()
                if abs(info["new"] - info["old"]) > 1e-9]

    # -------------------------------------------------------------- исход

    def loss_code(self, action: str, snap: dict) -> str:
        """Код условий входа убыточной сделки (детерминированно, без модели)."""
        c = self.conditions(snap)
        rsi, trend_up = c["rsi"], c["trend"] == "trend_up"
        if action == "BUY":
            if rsi > 85:
                return "buy_euphoria"
            if rsi < 30:
                return "buy_oversold"
            if not trend_up:
                return "buy_against_trend"
        elif action == "SELL":
            if rsi > 70:
                return "sell_overbought"
            if trend_up:
                return "sell_against_trend"
        return "loss"

    def record_trade(self, *, action: str, pnl_usd: float, snap: dict,
                     position_id: str = "", personas: list[str] | None = None,
                     mode: str = "", swarm=None) -> dict[str, Any]:
        """Итог закрытой сделки: веса агентов, событие траектории,
        память AI-2 и уроки.

        При убытке сделка и её контекст уходят в память AI-2, корпус
        перемайнивается и уроки переписываются в файл уроков AI-2.
        """
        pnl = float(pnl_usd or 0.0)
        issues: list[str] = []
        lines: list[str] = []
        # Learning Engine пересчитывает веса агентов по исходу сделки:
        # убыток → вес вниз, прибыль → вверх. Это не зависит от AI-2.
        lines.extend(self.apply_weights(swarm, personas, pnl))
        if not self.ai2:
            return {"code": "", "detail": "", "pnl_usd": pnl, "memory_id": "",
                    "lessons": [], "issues": list(self.issues) + issues,
                    "lines": lines}
        code = self.loss_code(action, snap)
        detail = (f"trade_loss:{code}" if pnl < 0 else f"trade_profit:{code}")
        if self.traj is not None:
            try:
                self.traj.emit("outcome", success=pnl >= 0,
                               score=1.0 if pnl >= 0 else 0.0,
                               evaluator="marginpad", evaluator_hash="",
                               label_source="marginpad_api", detail=detail)
            except Exception as e:  # noqa: BLE001
                issues.append(f"исход не записан в траекторию: {type(e).__name__}: {e}")
        lessons: list[Any] = []
        memory_id = ""
        if pnl < 0:
            memory_id, issue = self._save_memory(action=action, pnl=pnl, snap=snap,
                                                 position_id=position_id, code=code,
                                                 personas=personas or [], mode=mode)
            if issue:
                issues.append(issue)
            lessons, issue = self._mine_and_save()
            if issue:
                issues.append(issue)
            new_lesson = next((l for l in lessons if l.id == f"trade_loss:{code}"), None)
            if new_lesson is not None:
                lines.append(f"[learning] новый урок из убытка: "
                             f"{self._render(new_lesson)}")
            elif lessons:
                lines.append(f"[learning] уроков в файле после убытка: {len(lessons)}")
            if memory_id:
                lines.append(f"[learning] исход сделки записан в память AI-2: {memory_id}")
        return {"code": code, "detail": detail, "pnl_usd": pnl, "memory_id": memory_id,
                "lessons": lessons, "issues": issues, "lines": lines}

    # ------------------------------------------------------------- память AI-2

    def _save_memory(self, *, action: str, pnl: float, snap: dict, position_id: str,
                     code: str, personas: list[str], mode: str) -> tuple[str, str]:
        """Исход убыточной сделки → память AI-2 (POST /v1/memory)."""
        c = self.conditions(snap)
        meta = {"source": "trading_swarm", "action": action, "pnl_usd": pnl,
                "entry_conditions": code, "price": c["price"], "rsi": c["rsi"],
                "trend": c["trend"], "sentiment_score": c["sentiment"],
                "position_id": position_id, "mode": mode,
                "agent_personas_used": list(personas)[:12]}
        text = (f"Торговая сделка {self.symbol}: {action} закрыта с убытком "
                f"{pnl:+.2f} USD. Условия входа: {code}. "
                f"Контекст: цена {c['price']:.2f}, RSI {c['rsi']:.1f}, {c['trend']}, "
                f"настроения {c['mood']}. Агенты: {', '.join(personas[:12]) or '—'}")
        payload = json.dumps({"project": MEMORY_PROJECT, "text": text, "meta": meta},
                             ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.api_url}/v1/memory", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=MEMORY_TIMEOUT_S) as r:
                body = json.loads(r.read().decode("utf-8"))
            return str(body.get("id") or ""), ""
        except urllib.error.HTTPError as e:
            with e:
                return "", f"память AI-2 отклонила запись: HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 — сеть/хост: урок всё равно учится
            return "", f"память AI-2 недоступна: {type(e).__name__}: {e}"

    # ------------------------------------------------------------- майнинг

    def _mine_and_save(self) -> tuple[list[Any], str]:
        """Майнинг AI-2 по корпусу → файл уроков AI-2 (тот же путь, что у ai2 learn mine)."""
        try:
            report = self.ai2.mine_paths([self.corpus_dir])
        except Exception as e:  # noqa: BLE001
            return [], f"майнинг не выполнен: {type(e).__name__}: {e}"
        try:
            self.ai2.save_lessons(self.lessons_path, report.lessons, meta={
                "source": [str(self.corpus_dir)],
                "runs": report.runs,
                "failed_runs": report.failed_runs,
                "files": report.files,
                "configs": report.configs,
                "patterns": len(report.patterns),
            })
        except Exception as e:  # noqa: BLE001
            return report.lessons, f"уроки не сохранены: {type(e).__name__}: {e}"
        return report.lessons, ""

    # ----------------------------------------------------------------- лог

    def _render(self, lesson: Any) -> str:
        """Строка урока в формате AI-2 (Learning Engine)."""
        return self.ai2.render_lessons([lesson]) if self.ai2 else str(lesson)

    def log_lines(self, applied: dict[str, Any]) -> list[str]:
        """Строки лога TUI по событию «learning» AI-2."""
        lines: list[str] = []
        lessons = applied.get("lessons") or []
        if lessons:
            lines.append(f"[learning] уроков применено к рою: {len(lessons)}")
            for lesson in lessons:
                lines.append(f"[learning] {self._render(lesson)}")
        for issue in applied.get("issues") or []:
            lines.append(f"[learning] замечание: {issue}")
        return lines
