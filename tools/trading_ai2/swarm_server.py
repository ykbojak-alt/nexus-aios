"""Локальный движок NEXUS SWARM — HTTP-мост к торговому рою (stdlib only).

Панель NEXUS (Programs → NEXUS SWARM) ходит из браузера прямо на этот
движок, как VLoader ходит на свой engine. Роут Next здесь не нужен и не
работает: приложение собрано с output: 'export', где route handlers
выключены.

API:
    GET  /api/info       — сводка: репозиторий, интерпретатор, размер пула
    GET  /api/status     — полный статус: рой, портфель, счёт, рынок, история
    GET  /api/positions  — только счёт MarginPad и активные сделки (быстрый)
    GET  /api/decision   — окно «Решение»: вердикты мини-роев, дебаты, вето
    POST /api/cycle      — один цикл роя потоком (--no-execute: ордер не создаётся)
    GET  /api/loop       — состояние автономного прогона + хвост его лога
    POST /api/loop       — запустить loop (--loop --interval N; позиции открываются)
    POST /api/loop/stop  — остановить loop: мягко через флаг-файл (прогон сам
                           закроет позиции) или ?force=1 — жёсткий kill
    GET  /api/risk       — риск-профиль, стрики, chaos-режим, ручной пин
    POST /api/risk       — ?profile=X пин / ?cycle=1 оборот R / ?off=1 снять
    GET  /api/candles    — OHLCV-свечи биржи для графика панели:
                           ?symbol=POL/USDT&tf=1m&limit=60

Рой давно не BTC-монополь: рынок и позиции берутся из портфеля, а цикл
гоняется через AgentPool (100 слотов) и его парламент — три мини-роя по
четыре агента, два раунда дебатов и Мета-Арбитр на трёх вердиктах.

Рынок по умолчанию live: панель просит настоящие свечи и скринер, а
демо-сценарий включается явно (?demo=1) — иначе «прогон роя» в панели
выглядит как игрушка.

CORS открыт только для loopback-origin и origins из NEXUS_SWARM_ORIGINS.
Ключи API не читаются и наружу не отдаются: в статусе только имена моделей,
агентов, символы и рыночные/счётные числа.

Запуск:
    python -m tools.trading_ai2.swarm_server [--port 8890]
                                             [--pool-size 100] [--active 30]
                                             [--interval 60] [--demo]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PORT = int(os.environ.get("NEXUS_SWARM_PORT", "8890"))
#: Пул персон: голосуют топ-ACTIVE_N по дарвиновскому весу.
POOL_SIZE = int(os.environ.get("NEXUS_SWARM_POOL", "100"))
ACTIVE_N = int(os.environ.get("NEXUS_SWARM_ACTIVE", "12"))
#: Цикл по умолчанию — live: настоящие свечи Binance и настоящий скринер.
#: NEXUS_SWARM_LIVE=0, флаг --demo или ?demo=1 возвращают демо-сценарий
#: (реальная цена, синтетические индикаторы).
LIVE_CYCLE = os.environ.get("NEXUS_SWARM_LIVE", "1") == "1"
HISTORY = 12
#: Интервал автономного loop-прогона по умолчанию, сек (панель даёт выбор).
LOOP_INTERVAL = float(os.environ.get("NEXUS_SWARM_LOOP_INTERVAL", "60"))
#: Хвост лога loop: панель показывает последние строки, а не весь прогон.
LOOP_TAIL = 240
#: Флаг мягкого стопа прогона — его же читает demo_trading_grand (путь
#: передаём через NEXUS_LOOP_STOP_FLAG, чтобы стороны не разъехались).
LOOP_FLAG = REPO_ROOT / "logs" / "loop_stop.flag"
#: Dynamic Risk Profiling: те же файлы, что читает цикл и пишет TUI
#: (клавиша R) — API и терминал делят один контракт.
RISK_STATE = REPO_ROOT / "logs" / "risk_state.json"
RISK_OVERRIDE = REPO_ROOT / "logs" / "risk_override.json"
HISTORY_PATH = REPO_ROOT / "logs" / "trading_history.jsonl"
#: Порядок ручного переключения профиля — как в TUI и у Арбитра.
RISK_CYCLE = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")

CYCLE_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()
POSITIONS_LOCK = threading.Lock()
DECISION_LOCK = threading.Lock()
#: Loop живёт вне HTTP-соединения: браузер мог закрыться, а прогон — нет.
LOOP_LOCK = threading.Lock()
LOOP: dict | None = None
#: Статус собирается ~10 секунд (ccxt + MarginPad): перечитываем его не
#: чаще, чем раз в STATUS_TTL_S, иначе каждый клик «ОБНОВИТЬ» = новый опрос
#: биржи, а React StrictMode снимает состояние дважды на монтировании.
STATUS_TTL_S = float(os.environ.get("NEXUS_SWARM_TTL", "15"))
POSITIONS_TTL_S = float(os.environ.get("NEXUS_SWARM_POSITIONS_TTL", "10"))
#: Окно «Решение» — только чтение хвоста истории: дешёвое, но и оно не должно
#: перечитываться на каждом кадре панели.
DECISION_TTL_S = float(os.environ.get("NEXUS_SWARM_DECISION_TTL", "5"))
DECISION_TAIL_BYTES = 262144
#: Сколько последних решений отдаём: одно главное + предыдущие для сравнения.
#: Один из слотов резервируется под самое свежее решение с дебатами.
DECISION_LIMIT = 3
#: Свечи живут сменой целой свечи: чаще TTL перечитывать биржу смысла нет.
CANDLES_TTL_S = float(os.environ.get("NEXUS_SWARM_CANDLES_TTL", "20"))
CANDLE_TIMEFRAMES = {"1m", "3m", "5m", "15m", "1h", "4h", "1d"}
_CANDLES_LIMIT = 200
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,20}/[A-Za-z0-9]{2,20}$")
_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
_POSITIONS_CACHE: dict = {"ts": 0.0, "data": None}
_DECISION_CACHE: dict = {"ts": 0.0, "data": None}
_CANDLES_CACHE: dict = {"ts": 0.0, "key": None, "data": None}
CANDLES_LOCK = threading.Lock()
#: MarketData шарится между свечами и не пересоздаётся: ccxt load_markets
#: дорогой (~4 с), а ThreadingHTTPServer зовёт нас из разных потоков.
_MD: dict = {"obj": None}
_MD_LOCK = threading.Lock()

ALLOWED_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.I)
EXTRA_ORIGINS = {o.strip() for o in os.environ.get("NEXUS_SWARM_ORIGINS", "").split(",") if o.strip()}


def allowed_origin(headers) -> str | None:
    origin = headers.get("Origin")
    if not origin:
        return None
    return origin if (ALLOWED_ORIGIN.match(origin) or origin in EXTRA_ORIGINS) else None


def _cached(lock, cache: dict, ttl: float, force: bool, build) -> dict:
    """Один пересчёт на TTL; пересчёт уже идёт — протухшее сразу, не очередь.

    Сборка статуса — ~25 с ccxt-вызовов против 15-секундного TTL и полла:
    если читатель встаёт в очередь на лок, он ждёт чужую сборку целиком
    (и каждый последующий за ним). Serve-stale: пока первая нить
    пересчитывает, остальные получают последние собранные данные.
    """
    now = time.time()
    if not force and cache["data"] is not None and now - cache["ts"] < ttl:
        return cache["data"]
    if not lock.acquire(blocking=False):
        if cache["data"] is not None:
            return cache["data"]
        lock.acquire()              # холодный кэш — первый прогрев честно ждём
    try:
        now = time.time()
        if not force and cache["data"] is not None and now - cache["ts"] < ttl:
            return cache["data"]
        data = build()
        cache["ts"] = now
        cache["data"] = data
        return data
    finally:
        lock.release()


def invalidate_caches() -> None:
    """Цикл меняет портфель, счёт и историю — кэш обязан протухнуть."""
    _STATUS_CACHE["ts"] = 0.0
    _STATUS_CACHE["data"] = None
    _POSITIONS_CACHE["ts"] = 0.0
    _POSITIONS_CACHE["data"] = None
    _DECISION_CACHE["ts"] = 0.0
    _DECISION_CACHE["data"] = None


def build_candles(symbol: str, timeframe: str, limit: int,
                  force: bool = False) -> dict:
    """OHLCV-свечи для графика панели — те же данные, что кормят скринер.

    Символ/таймфрейм валидируются строго: это строка в ccxt-вызов, и чужой
    формат либо уронит запрос, либо уйдёт в несуществующий рынок. Кэш —
    по (symbol, tf, limit): переключение таймфрейма не сбивает общий TTL.
    """
    symbol = symbol.upper().strip()
    if not _SYMBOL_RE.match(symbol):
        return {"ok": False, "error": "bad_symbol"}
    if timeframe not in CANDLE_TIMEFRAMES:
        return {"ok": False, "error": "bad_timeframe"}
    limit = max(5, min(_CANDLES_LIMIT, int(limit)))
    key = (symbol, timeframe, limit)

    with CANDLES_LOCK:
        now = time.time()
        c = _CANDLES_CACHE
        if (not force and c["data"] is not None and c["key"] == key
                and now - c["ts"] < CANDLES_TTL_S):
            return c["data"]

        from tools.trading_ai2.market_data import MarketData
        with _MD_LOCK:
            if _MD["obj"] is None:
                _MD["obj"] = MarketData()
            md = _MD["obj"]
            md.symbol = symbol
            df = md.fetch_ohlcv(timeframe, limit=limit)
            exchange = md.exchange_id

        candles = [{
            "ts": int(t.timestamp()),
            "open": float(o), "high": float(h),
            "low": float(l), "close": float(cl), "volume": float(v),
        } for t, o, h, l, cl, v in zip(
            df["ts"], df["open"], df["high"], df["low"],
            df["close"], df["volume"])]
        data = {"ok": True, "symbol": symbol, "timeframe": timeframe,
                "source": f"{exchange}:public", "candles": candles}
        c["ts"], c["key"], c["data"] = now, key, data
        return data


def build_status(force: bool = False) -> dict:
    """Тот же отчёт, что у tools.trading_ai2.swarm_status, но с кэшем."""
    from tools.trading_ai2 import swarm_status

    return _cached(STATUS_LOCK, _STATUS_CACHE, STATUS_TTL_S, force,
                   lambda: swarm_status.build_report(
                       POOL_SIZE, ACTIVE_N, "", HISTORY))


def cycle_command(live: bool = False) -> list[str]:
    """Команда одного цикла: пул и топ-N голосующих, ордер не создаётся.

    Аргумент --agents сюда не попадает: он свёл бы рой к legacy-набору
    из 13 фиксированных агентов вместо пула персон.
    """
    cmd = [sys.executable, "-m", "tools.trading_ai2.demo_trading_grand",
           "--pool-size", str(POOL_SIZE), "--active", str(ACTIVE_N),
           "--no-execute"]
    if live:
        cmd.append("--live")
    return cmd


def loop_command(live: bool = False,
                 interval: float = LOOP_INTERVAL) -> list[str]:
    """Команда автономного прогона: цикл каждые --interval сек до стопа.

    --no-execute здесь НЕТ: смысл loop в том, что рой держит позиции, а
    manage_positions закрывает их по profit-take / max-age. Счёт при этом
    бумажный (MarginPad-симулятор на реальных ценах), лимиты держит сам
    скрипт: ≤3 позиций, margin_in_use < $500, min-hold 30с, max-age 300с.
    """
    cmd = [sys.executable, "-m", "tools.trading_ai2.demo_trading_grand",
           "--pool-size", str(POOL_SIZE), "--active", str(ACTIVE_N),
           "--loop", "--interval", f"{interval:g}"]
    if live:
        cmd.append("--live")
    return cmd


def _flag(query: str, name: str) -> bool:
    return re.search(rf"(?:^|&){name}=1(?:&|$)", query) is not None


def _proc_env(extra: dict | None = None) -> dict:
    """Одинаковое окружение для цикла и loop: utf-8, без печатной машинки."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NEXUS_TYPEWRITER": "0"}
    env.update(extra or {})
    return env


def loop_alive() -> bool:
    with LOOP_LOCK:
        rec = LOOP
        return bool(rec) and rec["proc"].poll() is None


def request_loop_stop() -> None:
    """Флаг мягкого стопа: loop заметит его между итерациями и закроет
    позиции своим circuit breaker'ом. Сигнал ОС не используем — на Windows
    CTRL_BREAK не прерывает основной поток Python-процесса, а terminate()
    бросил бы открытые позиции висеть на счёте."""
    LOOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    LOOP_FLAG.write_text(f"stop requested {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                         encoding="utf-8")


def _pump_loop(rec: dict) -> None:
    """Читатель stdout живёт своим потоком: GET /api/loop не должен
    зависеть от того, смотрит ли в панель кто-то прямо сейчас."""
    proc = rec["proc"]
    try:
        for line in proc.stdout or []:
            with LOOP_LOCK:
                rec["tail"].append(line.rstrip("\n"))
        rec["code"] = proc.wait()
    except (ValueError, OSError):    # пайп закрыт при остановке движка
        pass
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        invalidate_caches()


def start_loop(live: bool, interval: float = LOOP_INTERVAL) -> tuple[int, dict]:
    """200 — прогон пошёл, 409 — уже есть активный цикл или loop."""
    global LOOP
    if CYCLE_LOCK.locked():
        return 409, {"ok": False, "error": "cycle_busy"}
    with LOOP_LOCK:
        if LOOP and LOOP["proc"].poll() is None:
            return 409, {"ok": False, "error": "loop_busy",
                         "pid": LOOP["pid"],
                         "uptimeS": round(time.time() - LOOP["started"], 1)}
        cmd = loop_command(live, interval)
        if LOOP_FLAG.exists():
            LOOP_FLAG.unlink()      # старый флаг убил бы новый прогон
        kwargs: dict = {}
        proc_env = _proc_env({"NEXUS_LOOP_STOP_FLAG": str(LOOP_FLAG)})
        if os.name == "nt":
            # Своя группа процессов: Ctrl+C от ночного прогона или из
            # другого окна не прилетит в этот loop. CREATE_NO_WINDOW —
            # иначе консольный python.exe всплывает окном поверх панели.
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=proc_env,
            **kwargs)
        LOOP = {"proc": proc, "pid": proc.pid, "cmd": cmd, "live": live,
                "interval": interval, "started": time.time(), "code": None,
                "stopping": False, "tail": deque(maxlen=LOOP_TAIL)}
        rec = LOOP
    threading.Thread(target=_pump_loop, args=(rec,), daemon=True).start()
    invalidate_caches()
    return 200, loop_status()


def stop_loop(force: bool = False) -> tuple[int, dict]:
    """Стоп прогона: по умолчанию мягкий (флаг → circuit breaker сам
    закроет позиции). force=True — жёсткий kill: от него открытые позиции
    останутся висеть на счёте, и панель обязана это сказать честно."""
    with LOOP_LOCK:
        rec = LOOP
        if not rec or rec["proc"].poll() is not None:
            return 409, {"ok": False, "error": "loop_not_running"}
        proc = rec["proc"]
        rec["stopping"] = True
        rec["forced"] = force
    if force:
        try:
            proc.kill()
        except OSError as exc:        # noqa: BLE001 — стоп не должен вешать панель
            with LOOP_LOCK:
                rec["stopping"] = rec["forced"] = False
            return 500, {"ok": False, "error": f"kill_failed: {exc}"}
    else:
        try:
            request_loop_stop()
        except OSError as exc:
            with LOOP_LOCK:
                rec["stopping"] = False
            return 500, {"ok": False, "error": f"stop_failed: {exc}"}
    invalidate_caches()
    return 200, loop_status()


def _read_json(path: Path) -> dict | None:
    """Маленький json-файл состояния; его отсутствие — не ошибка."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _last_risk_fields() -> tuple[str, str]:
    """risk_profile/risk_mode последней записи истории (хвост файла).

    Читаем только последние ~64КБ: история растёт построчно, и тащить
    весь файл ради одного поля — пустая работа при каждом статусе.
    """
    try:
        size = HISTORY_PATH.stat().st_size
        with HISTORY_PATH.open("rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return "BALANCED", "NORMAL"
    profile, mode = "", ""
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        profile = profile or str(rec.get("risk_profile") or "")
        mode = mode or str(rec.get("risk_mode") or "")
        if profile and mode:
            break
    return profile or "BALANCED", mode or "NORMAL"


def risk_state() -> dict:
    """Dynamic Risk Profiling для панели: профиль, стрики, chaos-режим,
    ручной пин — одной структурой, как их видит цикл."""
    st = _read_json(RISK_STATE) or {}
    ov = _read_json(RISK_OVERRIDE) or {}
    last_profile, mode = _last_risk_fields()
    manual = str(ov.get("profile") or "").upper() or None
    return {
        "ok": True,
        "profile": manual or last_profile,      # пин важнее истории
        "manualProfile": manual,                # None — автоматика
        "winStreak": int(st.get("win") or 0),
        "lossStreak": int(st.get("loss") or 0),
        "riskMode": mode,                       # NORMAL | MICRO (chaos)
        "overrideFile": str(RISK_OVERRIDE),
    }


def set_risk_override(name: str | None) -> dict:
    """POST /api/risk: ручной пин профиля — тот же файл, что у клавиши R.

    name=None или 'off' → пин снят, автоматика стриков снова управляет.
    """
    if name is None or name == "off":
        try:
            RISK_OVERRIDE.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"override_clear: {exc}"}
        return {"ok": True, "manualProfile": None}
    name = str(name).upper()
    if name not in RISK_CYCLE:
        return {"ok": False, "error": "bad_profile",
                "allowed": list(RISK_CYCLE)}
    try:
        RISK_OVERRIDE.write_text(json.dumps({"profile": name}),
                                 encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"override_write: {exc}"}
    return {"ok": True, "manualProfile": name}


def cycle_risk_override() -> dict:
    """POST /api/risk?cycle=1 — тот же оборот, что у клавиши R в TUI:
    CONSERVATIVE → BALANCED → AGGRESSIVE → CONSERVATIVE."""
    cur = str((_read_json(RISK_OVERRIDE) or {}).get("profile")
              or "").upper()
    nxt = (RISK_CYCLE[(RISK_CYCLE.index(cur) + 1) % len(RISK_CYCLE)]
           if cur in RISK_CYCLE else RISK_CYCLE[1])
    return set_risk_override(nxt)


def loop_status() -> dict:
    """Состояние прогона для панели: жив ли, сколько работает, хвост лога."""
    with LOOP_LOCK:
        rec = LOOP
        if rec is None:
            return {"ok": True, "running": False, "started": False,
                    "phase": "idle", "tail": []}
        alive = rec["proc"].poll() is None
        code = None if alive else rec["code"]
        phase = "stopping" if (alive and rec["stopping"]) else (
            "running" if alive else "exited")
        return {
            "ok": True, "running": alive, "started": True, "phase": phase,
            "pid": rec["pid"], "live": rec["live"], "interval": rec["interval"],
            "uptimeS": round(time.time() - rec["started"], 1),
            "exitCode": code, "forced": bool(rec.get("forced")),
            "cmd": " ".join(rec["cmd"]),
            "tail": list(rec["tail"]),
        }


def build_positions(force: bool = False) -> dict:
    """Счёт MarginPad + сделки из истории: быстрый ответ для карточки."""
    from tools.trading_ai2 import swarm_status

    def build() -> dict:
        out: dict = {"ok": True, "errors": []}
        try:
            out.update(swarm_status._positions_section())
        except Exception as exc:  # noqa: BLE001 — счёт не роняет ответ
            out["errors"].append(f"positions: {exc}")
        try:
            out["trades"] = swarm_status._trades_section()
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"trades: {exc}")
        return out

    return _cached(POSITIONS_LOCK, _POSITIONS_CACHE, POSITIONS_TTL_S, force,
                   build)


def _history_tail_records(limit: int) -> list[dict]:
    """Последние записи истории (свежие сверху), читая только хвост файла."""
    try:
        size = HISTORY_PATH.stat().st_size
        with HISTORY_PATH.open("rb") as fh:
            fh.seek(max(0, size - DECISION_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(tail.splitlines()):
        if len(out) >= limit:
            break
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _decision_item(rec: dict) -> dict:
    """Запись цикла → карточка решения. `debate` отсутствует у плоского
    голосования и у записей, написанных до парламента."""
    arb = rec.get("arbiter") if isinstance(rec.get("arbiter"), dict) else {}
    return {"ts": rec.get("ts"), "mode": rec.get("mode"),
            "symbol": rec.get("symbol"), "price": rec.get("price"),
            "rsi": rec.get("rsi"), "riskMode": rec.get("risk_mode"),
            "riskProfile": rec.get("risk_profile"),
            "order": rec.get("order"), "votes": rec.get("votes"),
            "arbiter": {"action": (arb or {}).get("action"),
                        "approved": (arb or {}).get("approved"),
                        "score": (arb or {}).get("score"),
                        "reason": (arb or {}).get("reason")},
            "debate": rec.get("debate")
            if isinstance(rec.get("debate"), dict) else None}


def build_decision(force: bool = False) -> dict:
    """Окно «Решение»: последние вердикты роя — консенсусы мини-роев, голоса
    участников, META-VERDICT и вето Risk-роя.

    Источник — запись цикла в trading_history.jsonl: ручной цикл и автономный
    loop пишут её одинаково, поэтому панель видит решение независимо от того,
    кто его принял и жив ли ещё процесс прогона.
    """
    def build() -> dict:
        # Берём с запасом: хвост истории — это ещё и close/открытия позиций,
        # они не решение, и их фильтруем по event == "cycle".
        records = _history_tail_records(64)
        items = [_decision_item(r) for r in records if r.get("event") == "cycle"]
        head = items[:DECISION_LIMIT]
        # Loop, поднятый кодом до парламента, пишет плоские циклы каждые
        # 60 секунд — за три минуты он вытеснил бы последнее решение роев
        # из окна совсем. Один слот держим под самую свежую запись с
        # дебатами; панель сама подписывает, что показан не последний цикл.
        latest_debate = next((i for i in items if i.get("debate")), None)
        if latest_debate is not None and all(i is not latest_debate for i in head):
            head = sorted([*head[:DECISION_LIMIT - 1], latest_debate],
                          key=lambda i: str(i.get("ts") or ""), reverse=True)
        return {"ok": True, "items": head,
                "total": len(items)}

    return _cached(DECISION_LOCK, _DECISION_CACHE, DECISION_TTL_S, force, build)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # тихо
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            origin = allowed_origin(self.headers)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Браузер закрыл соединение (перезагрузка/StrictMode) — это не ошибка сервера.
            self.close_connection = True

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:
        origin = allowed_origin(self.headers)
        if origin is None:
            self._send(403, b"forbidden origin", "text/plain")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _want_fresh(query: str) -> bool:
        return _flag(query, "fresh")

    @staticmethod
    def _want_live(query: str) -> bool:
        return _flag(query, "live")

    @staticmethod
    def _want_demo(query: str) -> bool:
        return _flag(query, "demo")

    @classmethod
    def _resolve_live(cls, query: str) -> bool:
        """Live по умолчанию; демо включается явно — флагом движка или ?demo=1."""
        if cls._want_demo(query):
            return False
        return cls._want_live(query) or LIVE_CYCLE

    @staticmethod
    def _qval(query: str, name: str, default: str = "") -> str:
        """Один query-параметр; значение разэкранировано, но не валидировано —
        проверку делают потребители (регекс символа, whitelist таймфрейма)."""
        from urllib.parse import unquote_plus
        m = re.search(rf"(?:^|&){name}=([^&]*)", query)
        return unquote_plus(m.group(1)) if m else default

    @staticmethod
    def _want_interval(query: str) -> float:
        """Интервал loop из query; только целые секунды и в разумных границах."""
        m = re.search(r"(?:^|&)interval=(\d{1,5})(?:&|$)", query)
        if not m:
            return float(LOOP_INTERVAL)
        return min(3600.0, max(10.0, float(m.group(1))))

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/api/info":
            self._json(200, {
                "ok": True,
                "engine": "nexus-swarm",
                "repo": str(REPO_ROOT),
                "python": sys.version.split()[0],
                "poolSize": POOL_SIZE,
                "active": ACTIVE_N,
                "liveCycle": LIVE_CYCLE,
                "loopInterval": LOOP_INTERVAL,
                "loopRunning": loop_alive(),
                "parliament": True,          # 3 мини-роя × 4 агента, дебаты
                "chaosAtrGrowth": 100.0,     # порог CHAOS_MODE, % ATR за час
            })
            return
        if path == "/api/status":
            try:
                # копия: кэшированный отчёт мутировать нельзя — риск-слой
                # живёт быстрее STATUS_TTL и должен быть свежим всегда
                body = {**build_status(self._want_fresh(query)),
                        "risk": risk_state()}
                self._json(200, body)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"status_failed: {exc}"})
            return
        if path == "/api/positions":
            try:
                self._json(200, build_positions(self._want_fresh(query)))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"positions_failed: {exc}"})
            return
        if path == "/api/loop":
            self._json(200, loop_status())
            return
        if path == "/api/decision":
            try:
                self._json(200, build_decision(self._want_fresh(query)))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"decision_failed: {exc}"})
            return
        if path == "/api/risk":
            self._json(200, risk_state())
            return
        if path == "/api/candles":
            try:
                self._json(200, build_candles(
                    self._qval(query, "symbol", "BTC/USDT"),
                    self._qval(query, "tf", "1m"),
                    int(self._qval(query, "limit", "60") or 60),
                    self._want_fresh(query)))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"candles_failed: {exc}"})
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/api/cycle":
            if loop_alive():
                # Loop уже гоняет рой на том же счёте — второй прогон
                # начал бы плодить дубли в истории и спорить за маржу.
                self._json(409, {"ok": False, "error": "loop_busy"})
                return
            if not CYCLE_LOCK.acquire(blocking=False):
                self._json(409, {"ok": False, "error": "cycle_busy"})
                return
            try:
                self._stream_cycle(live=self._resolve_live(query))
            finally:
                CYCLE_LOCK.release()
            return
        if path == "/api/loop":
            code, body = start_loop(self._resolve_live(query),
                                    self._want_interval(query))
            self._json(code, body)
            return
        if path == "/api/loop/stop":
            code, body = stop_loop(force=_flag(query, "force"))
            self._json(code, body)
            return
        if path == "/api/risk":
            # ?profile=AGGRESSIVE|BALANCED|CONSERVATIVE — пин,
            # ?off=1 — снять пин, ?cycle=1 — оборот как у клавиши R.
            if _flag(query, "cycle"):
                body = cycle_risk_override()
            else:
                m = re.search(r"(?:^|&)profile=([A-Za-z]+)(?:&|$)", query)
                body = set_risk_override(
                    "off" if _flag(query, "off")
                    else (m.group(1).upper() if m else ""))
            self._json(200 if body.get("ok") else 400, body)
            return
        self._send(404, b"not found", "text/plain")

    def _stream_cycle(self, live: bool = False) -> None:
        """Один цикл роя потоком в ответ. --no-execute: ордер не создаётся."""
        origin = allowed_origin(self.headers)
        cmd = cycle_command(live)
        kwargs: dict = {}
        if os.name == "nt":
            # Без окна консоли — цикл идёт в фоне, вывод читаем из PIPE.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_proc_env(),
            **kwargs)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Swarm-Mode", "live" if live else "demo")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

        def chunk(text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        try:
            chunk(f"[движок] цикл: пул {POOL_SIZE}, голосуют топ-{ACTIVE_N}, "
                  f"рынок {'live (реальные свечи)' if live else 'демо-сценарий'}, "
                  f"ордера отключены (--no-execute)\n")
            for line in proc.stdout or []:
                chunk(line)
            code = proc.wait()
            chunk(f"\n[цикл завершён, код {code}]\n")
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()
            return
        finally:
            if proc.stdout:
                proc.stdout.close()
            invalidate_caches()
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    global POOL_SIZE, ACTIVE_N, LIVE_CYCLE, LOOP_INTERVAL

    parser = argparse.ArgumentParser(description="Движок NEXUS SWARM для панели NEXUS.")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--pool-size", type=int, default=POOL_SIZE,
                        help="слотов в AgentPool")
    parser.add_argument("--active", type=int, default=ACTIVE_N,
                        help="сколько агентов пула голосует за цикл")
    parser.add_argument("--agents", type=int, default=0,
                        help="устаревший синоним --pool-size")
    parser.add_argument("--interval", type=float, default=LOOP_INTERVAL,
                        help="пауза loop-прогона по умолчанию, сек")
    parser.add_argument("--live", action="store_true",
                        help="явно включить live (он и так по умолчанию)")
    parser.add_argument("--demo", action="store_true",
                        help="циклы на демо-сценарии: синтетические индикаторы")
    args = parser.parse_args()

    POOL_SIZE = args.pool_size or args.agents or POOL_SIZE
    ACTIVE_N = args.active
    LOOP_INTERVAL = args.interval or LOOP_INTERVAL
    if args.demo:
        LIVE_CYCLE = False
    elif args.live:
        LIVE_CYCLE = True

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"NEXUS SWARM engine: http://127.0.0.1:{args.port}/api/status  "
          f"(repo: {REPO_ROOT}, pool {POOL_SIZE}/top-{ACTIVE_N}, "
          f"cycle {'live' if LIVE_CYCLE else 'demo'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        with _MD_LOCK:
            market_data = _MD["obj"]
            _MD["obj"] = None
        if market_data is not None:
            market_data.close()
        try:
            from tools.trading_ai2.swarm.portfolio.market_screener import close_exchange
            close_exchange()
        except ImportError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
