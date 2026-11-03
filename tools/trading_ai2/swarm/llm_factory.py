"""llm_factory.py — переключатель LLM-моделей для торгового роя.

Модель выбирается из ACTIVE_LLM_MODEL и перечитывается на каждом
resolve() — смена переменной между циклами переключает ро без
перезапуска процесса.

Маршрутизация по имени модели:
  deepseek*          → https://api.deepseek.com/v1    + DEEPSEEK_API_KEY
  gpt* / o1* / o3*   → https://api.openai.com/v1      + OPENAI_API_KEY
  claude*            → https://api.anthropic.com/v1/  + ANTHROPIC_API_KEY
  mercury*           → https://api.inceptionlabs.ai/v1 + INCEPTION_API_KEY
  id со «/» (OpenRouter-вид: anthropic/claude-…, cohere/…) →
                      LLM_BASE_URL + LLM_API_KEY (шлюз репозитория)

Модели без узнаваемого префикса и без «/» считаем ненайденными.

Ключа нет или модель не найдена → откат на дефолт: сначала DeepSeek
(дефолт по ТЗ), затем шлюз репозитория, затем stub. Откат всегда виден
в логе: [llm] Fallback to DeepSeek (key for GPT missing).
"""

from __future__ import annotations

import math
import re as _re
import sys
import threading
import time
from dataclasses import dataclass, replace as _replace
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from research_agent_clients import env
except ImportError:  # pragma: no cover — пакетный импорт
    from tools.research_agent_clients import env

ACTIVE_MODEL_ENV = "ACTIVE_LLM_MODEL"
DEFAULT_MODEL = "deepseek-chat"

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/"

#: Бесплатная модель по умолчанию — на неё падаем при 402/429.
DEFAULT_FREE_MODEL = "cohere/north-mini-code:free"
#: Лимит запросов в минуту на одну :free-модель.
FREE_RPM_LIMIT = 20
DEFAULT_LLM_REQUEST_TIMEOUT_S = 120.0
MAX_LLM_REQUEST_TIMEOUT_S = 300.0

#: Минимум токенов, ради которых имеет смысл squeeze-повтор:
#: «can only afford N» с N-16 < 400 — JSON обрежется → сразу следующий
#: ключ/модель, запрос не шлём (правило A.L.I.C.E.).
MIN_SQUEEZE_TOKENS = 400
#: Пауза при полном исчерпании лимитов (все ключи/модели дали 429/402):
#: спим и пробуем заново после окна RPM. 0 в env — для тестов.
QUOTA_SLEEP_S = float(env("LLM_QUOTA_SLEEP_S") or 900)
#: Файл конфигурации ролей роя.
MODELS_CONFIG_NAME = "models.yaml"
#: Чёрный список моделей: 402 (нет баланса) — сразу; 404/429 — после
#: трёх отказов подряд. Запись живёт BLACKLIST_TTL_S (15 минут), запросы
#: к ней больше не тратятся.
BLACKLIST_TTL_S = 900.0
#: Сколько отказов подряд (404/429) считается «мёртвой» моделью.
BLACKLIST_STRIKES = 3


def _request_timeout_s() -> float:
    """Finite OpenAI client deadline, configurable but capped at five minutes."""
    raw = str(env("LLM_REQUEST_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_LLM_REQUEST_TIMEOUT_S
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LLM_REQUEST_TIMEOUT_S
    if not math.isfinite(timeout):
        return DEFAULT_LLM_REQUEST_TIMEOUT_S
    return min(MAX_LLM_REQUEST_TIMEOUT_S, max(1.0, timeout))


@dataclass(frozen=True)
class LLMRoute:
    """Куда и какой моделью говорим прямо сейчас."""
    model: str
    provider: str            # deepseek | openai | anthropic | openai-compatible
    api_key: str
    base_url: str
    requested: str = ""      # модель, которую просили (до отката)
    fallback: bool = False
    reason: str = ""         # почему откатились

    @property
    def live(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class _Spec:
    provider: str
    prefixes: tuple[str, ...]
    key_env: str
    base_url: str
    label: str               # для «key for GPT missing»


_SPECS = (
    _Spec("deepseek", ("deepseek",), "DEEPSEEK_API_KEY", DEEPSEEK_BASE_URL,
          "DEEPSEEK"),
    _Spec("openai", ("gpt", "o1", "o3", "o4", "chatgpt"),
          "OPENAI_API_KEY", OPENAI_BASE_URL, "GPT"),
    _Spec("anthropic", ("claude", "anthropic"), "ANTHROPIC_API_KEY",
          ANTHROPIC_BASE_URL, "CLAUDE"),
    _Spec("inception", ("mercury",), "INCEPTION_API_KEY",
          "https://api.inceptionlabs.ai/v1", "MERCURY"),
    # Неймспейс-префиксы «prov/»: «groq/…» в конфиге → spec groq, в API
    # уходит имя без префикса. Имена моделей этих провайдеров пересекаются
    # с чужими (gpt-oss, llama, deepseek), поэтому без неймспейса не роутить.
    _Spec("groq", ("groq/",), "GROQ_API_KEY",
          "https://api.groq.com/openai/v1", "GROQ"),
    _Spec("sambanova", ("snova/",), "SAMBANOVA_API_KEY",
          "https://api.sambanova.ai/v1", "SAMBANOVA"),
    _Spec("agnes", ("agnes/", "agnes-"), "AGNES_API_KEY",
          "https://apihub.agnes-ai.com/v1", "AGNES"),
    _Spec("mistral", ("mstr/", "mistral-"), "MISTRAL_API_KEY",
          "https://api.mistral.ai/v1", "MISTRAL"),
)


class _EmptyResponse(RuntimeError):
    """Модель ответила без содержимого — повод переключиться на резерв."""


def _yaml_scalar(text: str):
    """Скаляр из строки YAML: кавычки, число, строка."""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def parse_models_yaml(text: str) -> dict:
    """Разбор models.yaml. PyYAML в репозитории нет — свой минимальный
    разбор схемы roles → роль → {models[], model, fallback}."""
    roles: dict[str, dict] = {}
    role: str | None = None
    section: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 0:
            role = None
            section = None
            continue
        if indent == 2 and body.endswith(":"):
            role = body[:-1].strip()
            roles.setdefault(role, {})
            section = None
            continue
        if role is None:
            continue
        if indent == 4 and body.endswith(":"):
            section = body[:-1].strip()
            roles[role].setdefault(section, [])
            continue
        if indent == 6 and body.startswith("- "):
            item = body[2:].strip()
            if ":" in item:
                key, _, val = item.partition(":")
                roles[role].setdefault(section, []).append(
                    {key.strip(): _yaml_scalar(val.strip())})
            else:
                roles[role].setdefault(section, []).append(_yaml_scalar(item))
            continue
        if indent == 8 and ":" in body:
            key, _, val = body.partition(":")
            items = roles[role].get(section) or []
            if items and isinstance(items[-1], dict):
                items[-1][key.strip()] = _yaml_scalar(val.strip())
            continue
        if indent == 4 and ":" in body:
            key, _, val = body.partition(":")
            roles[role][key.strip()] = _yaml_scalar(val.strip())
    return {"roles": roles}


class LLMFactory:
    """Переключатель LLM-моделей по ACTIVE_LLM_MODEL.

      LLMFactory.resolve(model)         → LLMRoute по имени модели
      LLMFactory.get_client(route)      → OpenAI-клиент по маршруту
      LLMFactory.get_client("critic")   → клиент SWARM_CRITIC_MODEL
      LLMFactory.client_for(key, url)   → клиент произвольного шлюза
      LLMFactory.log_lines(route)       → строки лога о выборе модели

    Окружение читается на каждом вызове, поэтому смена ACTIVE_LLM_MODEL
    видна на следующем цикле роя — без перезапуска процесса.
    """

    ACTIVE_MODEL_ENV = ACTIVE_MODEL_ENV
    DEFAULT_MODEL = DEFAULT_MODEL

    _ROLE_ENVS = {"generator": "SWARM_GEN_MODELS",
                  "critic": "SWARM_CRITIC_MODEL",
                  "arbiter": "SWARM_ARBITER_MODEL"}

    _logged: set[str] = set()
    _free_calls: dict[str, list[float]] = {}
    _clients: dict[tuple[str, str, float], object] = {}
    _client_models: dict[tuple[str, str, float], set[str]] = {}
    _client_active: dict[int, int] = {}
    _clients_pending_close: dict[int, object] = {}
    _clients_lock = threading.RLock()
    _config: dict | None = None
    #: Чёрный список: запись → (время истечения, причина).
    _blacklist: dict[str, tuple[float, str]] = {}
    #: Отказы подряд по модели: 404/429 → три подряд = чёрный список.
    _strikes: dict[str, int] = {}
    # момент последнего quota-сна: повторный сон не раньше QUOTA_SLEEP_S,
    # иначе 13 агентов проспали бы 15 минут каждый подряд
    _quota_slept_at: float = 0.0

    # --- маршрутизация -------------------------------------------------

    @staticmethod
    def _spec_for(model: str) -> _Spec | None:
        """Провайдер по префиксу имени модели (None — модель не найдена)."""
        low = model.lower()
        for spec in _SPECS:
            if any(low.startswith(p) for p in spec.prefixes):
                return spec
        return None

    @staticmethod
    def _base_url(spec: _Spec) -> str:
        if spec.provider == "deepseek":
            return env("DEEPSEEK_BASE_URL").strip() or DEEPSEEK_BASE_URL
        if spec.provider == "openai":
            return env("OPENAI_BASE_URL").strip() or OPENAI_BASE_URL
        if spec.provider == "anthropic":
            return env("ANTHROPIC_BASE_URL").strip() or ANTHROPIC_BASE_URL
        if spec.provider == "inception":
            return (env("INCEPTION_BASE_URL").strip()
                    or "https://api.inceptionlabs.ai/v1")
        custom = env(f"{spec.label}_BASE_URL").strip()
        return custom or spec.base_url

    @staticmethod
    def _gateway() -> tuple[str, str]:
        """Шлюз репозитория (OpenRouter-совместимый): (ключ, base_url)."""
        return env("LLM_API_KEY").strip(), env("LLM_BASE_URL").strip()

    @classmethod
    def _fallback_route(cls, requested: str, reason: str) -> LLMRoute:
        """Откат: DeepSeek → шлюз репозитория → ничего (stub)."""
        ds_key = env("DEEPSEEK_API_KEY").strip()
        if ds_key:
            return LLMRoute(model=DEFAULT_MODEL, provider="deepseek",
                            api_key=ds_key,
                            base_url=(env("DEEPSEEK_BASE_URL").strip()
                                      or DEEPSEEK_BASE_URL),
                            requested=requested, fallback=True, reason=reason)
        g_key, g_url = cls._gateway()
        if g_key and g_url:
            return LLMRoute(model=env("LLM_MODEL").strip() or DEFAULT_MODEL,
                            provider="openai-compatible", api_key=g_key,
                            base_url=g_url, requested=requested,
                            fallback=True, reason=reason)
        return LLMRoute(model=DEFAULT_MODEL, provider="deepseek", api_key="",
                        base_url=DEEPSEEK_BASE_URL, requested=requested,
                        fallback=True, reason=reason)

    @staticmethod
    def resolve(model: str | None = None) -> LLMRoute:
        """Маршрут для активной модели (ACTIVE_LLM_MODEL или аргумент).

        Ключ читается на каждом вызове: смена ACTIVE_LLM_MODEL между
        циклами переключает модель на лету.
        """
        requested = (model or env(ACTIVE_MODEL_ENV) or "").strip() or DEFAULT_MODEL
        spec = LLMFactory._spec_for(requested)
        if spec is not None:
            key = env(spec.key_env).strip()
            if not key:
                return LLMFactory._fallback_route(requested,
                                                  f"key for {spec.label} missing")
            actual = requested
            for p in spec.prefixes:
                if p.endswith("/") and requested.lower().startswith(p):
                    actual = requested[len(p):]
                    break
            return LLMRoute(model=actual, provider=spec.provider, api_key=key,
                            base_url=LLMFactory._base_url(spec),
                            requested=requested)
        if "/" in requested:
            # id шлюза репозитория (anthropic/claude-…, cohere/…): тот же шлюз
            g_key, g_url = LLMFactory._gateway()
            if g_key and g_url:
                return LLMRoute(model=requested, provider="openai-compatible",
                                api_key=g_key, base_url=g_url,
                                requested=requested)
            return LLMFactory._fallback_route(requested,
                                              "key for GATEWAY missing")
        return LLMFactory._fallback_route(
            requested, f"model {requested} not found")

    # --- роли роя ------------------------------------------------------

    @classmethod
    def role_model(cls, role: str) -> str | None:
        """Модель роли из .env (generator — первая из csv-списка)."""
        models = cls.role_models(role)
        return models[0] if models else None

    @classmethod
    def role_route(cls, role: str) -> LLMRoute:
        """Маршрут роли: SWARM_*_MODEL → ACTIVE_LLM_MODEL → дефолт."""
        return cls.resolve(cls.role_model(role))

    # --- конфигурация ролей (models.yaml) -------------------------------

    @staticmethod
    def config_path() -> Path:
        """Путь к models.yaml рядом с пакетом trading_ai2."""
        return Path(__file__).resolve().parents[1] / MODELS_CONFIG_NAME

    @classmethod
    def load_config(cls, force: bool = False) -> dict:
        """Разобранный models.yaml (кеш в процессе; force — перечитать)."""
        if cls._config is not None and not force:
            return cls._config
        try:
            text = cls.config_path().read_text(encoding="utf-8")
        except OSError:
            cls._config = {"roles": {}}
            return cls._config
        cls._config = parse_models_yaml(text)
        return cls._config

    @classmethod
    def role_config(cls, role: str) -> dict:
        """Раздел роли из models.yaml (пусто — если роли нет)."""
        roles = cls.load_config().get("roles") or {}
        cfg = roles.get(role) or {}
        return cfg if isinstance(cfg, dict) else {}

    @classmethod
    def role_models(cls, role: str) -> list[str]:
        """Модели роли: SWARM_* из .env (csv) → models.yaml.

        SWARM_GEN_MODELS — список через запятую, SWARM_CRITIC_MODEL и
        SWARM_ARBITER_MODEL — по одной модели; .env переопределяет yaml.
        """
        env_name = cls._ROLE_ENVS.get(role)
        raw = env(env_name).strip().strip('"').strip("'") if env_name else ""
        if raw:
            return [m.strip() for m in raw.split(",") if m.strip()]
        cfg = cls.role_config(role)
        out: list[str] = []
        for item in (cfg.get("models") or []):
            mid = item.get("model") if isinstance(item, dict) else item
            if mid:
                out.append(str(mid))
        single = cfg.get("model")
        if not out and single:
            out.append(str(single))
        return out

    @classmethod
    def role_plan(cls, role: str, n: int) -> list[str]:
        """Раскладка моделей роли на n агентов.

        SWARM_GEN_MODELS (csv) — поровну между моделями; models.yaml —
        по весам (20/20/20 на 60 агентов).
        """
        if n <= 0:
            return []
        env_name = cls._ROLE_ENVS.get(role)
        raw = env(env_name).strip().strip('"').strip("'") if env_name else ""
        if raw:
            models = [m.strip() for m in raw.split(",") if m.strip()]
            if models:
                per = max(1, n // len(models))
                return [models[min(i // per, len(models) - 1)] for i in range(n)]
        cfg = cls.role_config(role)
        weighted = [(str(it.get("model")), int(it.get("weight") or 0))
                    for it in (cfg.get("models") or [])
                    if isinstance(it, dict) and it.get("model")]
        if weighted:
            # веса не обязаны суммироваться в n: раздаём n слотов
            # пропорционально (max-remainder), иначе хвост списка —
            # модели с поздним весом — вообще не доходил до агентов
            total = sum(max(0, w) for _, w in weighted)
            counts = [max(1, round(max(0, w) * n / total))
                      if total else 0 for _, w in weighted]
            while sum(counts) > n:
                i = counts.index(max(counts))
                counts[i] -= 1
            while sum(counts) < n:
                i = counts.index(max(counts))
                counts[i] += 1
            out: list[str] = []
            for (mid, _), c in zip(weighted, counts):
                out.extend([mid] * c)
            return out
        single = cfg.get("model")
        return [str(single)] * n if single else []

    @classmethod
    def role_fallback(cls, role: str) -> str:
        """Резерв роли: models.yaml → LLM_MODEL → бесплатный дефолт."""
        fb = str(cls.role_config(role).get("fallback") or "").strip()
        if fb:
            return fb
        return env("LLM_MODEL").strip() or DEFAULT_FREE_MODEL

    @staticmethod
    def is_free_model(model: str) -> bool:
        """Бесплатная модель OpenRouter — с суффиксом :free."""
        return ":free" in (model or "").lower()

    # --- квота :free-моделей -------------------------------------------

    @classmethod
    def free_rpm_used(cls, account: str) -> int:
        """Сколько запросов к :free-моделям ушло за последнюю минуту.

        Счётчик общий на аккаунт (шлюз), а не на модель: бесплатный
        тариф OpenRouter ограничен 20 запросами в минуту на ключ, и
        лимит делится между всеми :free-моделями этого ключа.
        """
        now = time.time()
        calls = [t for t in cls._free_calls.get(account, []) if now - t < 60.0]
        cls._free_calls[account] = calls
        return len(calls)

    @classmethod
    def note_free_call(cls, account: str) -> None:
        """Засчитать запрос к :free-модели на аккаунт."""
        cls._free_calls.setdefault(account, []).append(time.time())

    @classmethod
    def free_quota_left(cls, account: str) -> int:
        """Остаток квоты :free-моделей аккаунта в текущей минуте."""
        return max(0, FREE_RPM_LIMIT - cls.free_rpm_used(account))

    @classmethod
    def reset_free_calls(cls) -> None:
        """Сбросить счётчики :free-моделей (тесты)."""
        cls._free_calls.clear()

    # --- чёрный список моделей -----------------------------------------

    @staticmethod
    def _key_tag(key: str) -> str:
        """Отпечаток ключа для записи «модель|ключ» (без секрета)."""
        return (key or "")[-6:]

    @staticmethod
    def _key_mask(key: str) -> str:
        """Маска ключа для лога: только хвост из 4 символов."""
        return "…" + (key or "")[-4:]

    @classmethod
    def blacklist_model(cls, model: str, reason: str = "",
                        *, key: str = "", base_url: str = "") -> float:
        """В чёрный список на BLACKLIST_TTL_S (15 минут).

        key — конкретный ключ пула: баланс у каждого ключа свой, поэтому
        402 гасит ключ, а не модель целиком. Без ключа — модель целиком
        (404: эндпоинт мёртв, ни один ключ не поможет). При base_url
        ключ гасится ещё и на уровне шлюза: баланс/квота привязаны к
        ключу, а не к паре «модель|ключ», — иначе каждая резервная
        модель того же шлюза тратит по запросу на уже мёртвый ключ.
        """
        entry = model if not key else f"{model}|{cls._key_tag(key)}"
        expiry = time.time() + BLACKLIST_TTL_S
        cls._blacklist[entry] = (expiry, reason)
        if key and base_url:
            cls._blacklist[f"key|{base_url}|{cls._key_tag(key)}"] = \
                (expiry, reason)
        cls._close_cached_clients(model=model, key=key, base_url=base_url)
        return expiry

    @classmethod
    def _blacklist_hit(cls, entry: str) -> bool:
        """Запись в чёрном списке жива? (просроченная — удаляется)"""
        item = cls._blacklist.get(entry)
        if not item:
            return False
        expiry, _ = item
        if expiry <= time.time():
            cls._blacklist.pop(entry, None)
            return False
        return True

    @classmethod
    def is_blacklisted(cls, model: str, key: str = "",
                       base_url: str = "") -> bool:
        """Модель в чёрном списке (модель целиком, ключ пула или ключ
        на уровне шлюза — 402/квота действует на все его модели)?"""
        if cls._blacklist_hit(model):
            return True
        if key and cls._blacklist_hit(f"{model}|{cls._key_tag(key)}"):
            return True
        if key and base_url and cls._blacklist_hit(
                f"key|{base_url}|{cls._key_tag(key)}"):
            return True
        return False

    @classmethod
    def blacklist_reason(cls, model: str, key: str = "",
                         base_url: str = "") -> str:
        """Причина блокировки (rate_limit | not_found | …)."""
        for entry in (model,
                      f"{model}|{cls._key_tag(key)}" if key else None,
                      f"key|{base_url}|{cls._key_tag(key)}"
                      if key and base_url else None):
            item = cls._blacklist.get(entry)
            if item:
                return item[1]
        return ""

    @classmethod
    def blacklist_entries(cls) -> dict[str, float]:
        """Чёрный список → {запись: остаток секунд}."""
        now = time.time()
        out: dict[str, float] = {}
        for entry, (expiry, _reason) in list(cls._blacklist.items()):
            if expiry <= now:
                cls._blacklist.pop(entry, None)
                continue
            out[entry] = round(expiry - now, 1)
        return out

    @classmethod
    def reset_blacklist(cls) -> None:
        """Сбросить чёрный список и счётчики отказов (тесты)."""
        cls._blacklist.clear()
        cls._strikes.clear()

    @classmethod
    def _count_strike(cls, model: str) -> int:
        """Счётчик подряд ошибок модели (404/429/empty).

        Три отказа подряд → модель в чёрном списке на 15 минут: единичный
        429 (лимит запросов), 404 (микро-сетевой сбой) или пустой ответ ещё
        не приговор, а три подряд — уже неисправный эндпоинт/ответ модели.
        Успех сбрасывает общий счётчик; 402 обрабатывается отдельно.
        """
        n = cls._strikes.get(model, 0) + 1
        cls._strikes[model] = n
        return n

    @classmethod
    def _clear_strikes(cls, model: str) -> None:
        """Модель ответила — счётчик сброшен: отказы не подряд."""
        cls._strikes.pop(model, None)

    @classmethod
    def strikes(cls, model: str) -> int:
        """Сколько отказов подряд у модели (лог и тесты)."""
        return cls._strikes.get(model, 0)

    @classmethod
    def _block_model(cls, model: str, status: int, api_key: str,
                     pool: list[str], tried: set,
                     base_url: str = "") -> None:
        """402 → чёрный список на 15 минут.

        404 — эндпоинт мёртв: гасим модель целиком (здесь только после
        трёх отказов подряд, см. _count_strike). 402 — не хватило
        баланса: баланс у каждого ключа свой, поэтому гасим ключ (на
        уровне шлюза, см. blacklist_model); если других ключей в пуле
        нет — модель целиком.
        """
        if status == 404:
            cls.blacklist_model(model, "not_found")
            cls._log(f"[llm] Blacklisted model {model} for 15 min "
                     f"(404 dead endpoint)")
            return
        others = [k for k in pool if k not in tried]
        if others:
            cls.blacklist_model(model, "rate_limit", key=api_key,
                                base_url=base_url)
            cls._log(f"[llm] Blacklisted key {cls._key_mask(api_key)} of "
                     f"{model} for 15 min (402 insufficient balance)")
        else:
            cls.blacklist_model(model, "rate_limit")
            cls._log(f"[llm] Blacklisted model {model} for 15 min "
                     f"(402 insufficient balance)")

    # --- клиенты -------------------------------------------------------

    @staticmethod
    def client_for(api_key: str, base_url: str):
        """Клиент для произвольного маршрута (шлюз репозитория, пул)."""
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=base_url,
                      timeout=_request_timeout_s())

    @classmethod
    def get_client(cls, route: "LLMRoute | str | None" = None):
        """OpenAI-клиент: по LLMRoute, по имени роли, по активной модели."""
        if isinstance(route, str):
            r = cls.role_route(route) if route in cls._ROLE_ENVS \
                else cls.resolve(route)
        elif isinstance(route, LLMRoute):
            r = route
        else:
            r = cls.resolve()
        if not r.live:
            raise RuntimeError(f"no LLM key for {r.provider}/{r.model}")
        from openai import OpenAI
        return OpenAI(api_key=r.api_key, base_url=r.base_url,
                      timeout=_request_timeout_s())

    @classmethod
    def _cached_client(cls, route: LLMRoute):
        """Клиент маршрута с переиспользованием соединения."""
        timeout = _request_timeout_s()
        cache_key = (route.base_url, route.api_key, timeout)
        with cls._clients_lock:
            client = cls._clients.get(cache_key)
            if client is None:
                client = cls.get_client(route)
                cls._clients[cache_key] = client
            cls._client_models.setdefault(cache_key, set()).update(
                model for model in (route.model, route.requested) if model)
            return client

    @classmethod
    def _acquire_cached_client(cls, route: LLMRoute):
        """Lease a cached client while one concurrent request uses it."""
        with cls._clients_lock:
            client = cls._cached_client(route)
            ident = id(client)
            cls._client_active[ident] = cls._client_active.get(ident, 0) + 1
            return client

    @classmethod
    def _release_cached_client(cls, client) -> None:
        """Finish a request lease and perform a deferred close if evicted."""
        to_close = None
        with cls._clients_lock:
            ident = id(client)
            active = cls._client_active.get(ident, 0)
            if active <= 1:
                cls._client_active.pop(ident, None)
                to_close = cls._clients_pending_close.pop(ident, None)
            else:
                cls._client_active[ident] = active - 1
        if to_close is not None:
            cls._close_client(to_close)

    @staticmethod
    def _close_client(client) -> None:
        """Close an OpenAI client without letting cleanup mask the API error."""
        close = getattr(client, "close", None)
        try:
            if callable(close):
                close()
                return
        except Exception:  # noqa: BLE001 — try the underlying transport too
            pass
        http_client = getattr(client, "http_client", None)
        transport_close = getattr(http_client, "close", None)
        if callable(transport_close):
            try:
                transport_close()
            except Exception:  # noqa: BLE001 — shutdown must continue
                pass

    @classmethod
    def _close_cached_clients(cls, *, model: str = "", key: str = "",
                              base_url: str = "") -> None:
        to_close: list[object] = []
        with cls._clients_lock:
            for cache_key, client in list(cls._clients.items()):
                url, api_key, _timeout = cache_key
                models = cls._client_models.get(cache_key, set())
                matches = ((key and base_url and url == base_url
                            and api_key == key)
                           or (not key and model and model in models)
                           or (not key and not model))
                if not matches:
                    continue
                cls._clients.pop(cache_key, None)
                cls._client_models.pop(cache_key, None)
                if cls._client_active.get(id(client), 0):
                    cls._clients_pending_close[id(client)] = client
                else:
                    to_close.append(client)
        for client in to_close:
            cls._close_client(client)

    @classmethod
    def reset_clients(cls) -> None:
        """Закрыть и сбросить кеш клиентов (тесты и shutdown)."""
        cls._close_cached_clients()

    # --- запрос с откатом ----------------------------------------------

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        """HTTP-статус из исключения провайдера (None — неизвестен)."""
        for attr in ("status_code", "http_status"):
            value = getattr(exc, attr, None)
            if isinstance(value, int):
                return value
        resp = getattr(exc, "response", None)
        value = getattr(resp, "status_code", None)
        return value if isinstance(value, int) else None

    @classmethod
    def _reason_of(cls, exc: Exception) -> str:
        """Причина отказа: rate_limit | not_found | empty | error."""
        status = cls._status_of(exc)
        if status in (402, 429):
            return "rate_limit"
        if status == 404:
            return "not_found"
        if isinstance(exc, _EmptyResponse):
            return "empty"
        return "error"

    @classmethod
    def _key_pool(cls, base_url: str, model: str = "") -> list[str]:
        """Пул ключей маршрута.

        Шлюз (OpenRouter/APInex): LLM_API_KEYS (csv) + LLM_API_KEY —
        у каждого ключа OpenRouter своя free-квота и свой баланс.
        Нативные провайдеры: {LABEL}_API_KEYS (csv) + {key_env}
        (GROQ_API_KEYS, MERCURY_API_KEYS, SAMBANOVA_API_KEYS и т.д.) —
        два ключа на аккаунт дают две квоты. Ключей нет — пустой пул,
        работает одиночный ключ из route.
        """
        url = base_url or ""
        if "openrouter" in url or "apinex" in url:
            names = ("LLM_API_KEYS", "LLM_API_KEY")
        else:
            spec = cls._spec_for(model)
            names = ((f"{spec.label}_API_KEYS", spec.key_env)
                     if spec else ())
        keys: list[str] = []
        for name in names:
            for k in (env(name) or "").split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys

    @staticmethod
    def _afford_of(exc: Exception) -> int:
        """OpenRouter 402: «...can only afford N» — сколько max_tokens
        реально покрывает баланс. 0, если не распознано."""
        m = _re.search(r"afford\s+(\d+)", str(exc))
        return int(m.group(1)) if m else 0

    @classmethod
    def _fallbacks(cls, role: str, failed: str,
                   reason: str | None) -> list[str]:
        """Кандидаты после отказа модели.

        402/429 → сначала бесплатные модели (LLM_MODEL и остальные :free
        модели роли): отказ по квоте или оплате не должен уводить на
        платную модель. 404/пустой ответ → сначала резерв роли из
        models.yaml.
        """
        yaml_fb = str(cls.role_config(role).get("fallback") or "").strip()
        free = env("LLM_MODEL").strip() or DEFAULT_FREE_MODEL
        # Изначально в candidates — только :free-модели роли. Но APInex
        # «free/*» не имеют суффикса :free, поэтому is_free_model() их не
        # ловит, и нативные модели из role_models (mercury/groq/snova/
        # agnes/mistral) выпадали из fallback — когда все free/*-ключи
        # 402, цикл не доходил до нативных пулов {LABEL}_API_KEYS и спал
        # 15 минут. Добавляем нативные модели, чьи пулы уже разрешены
        # _fallback_allowed (inception/groq/sambanova/agnes/mistral).
        free_models = [m for m in cls.role_models(role)
                       if cls.is_free_model(m)]
        native_models = [m for m in cls.role_models(role)
                         if not cls.is_free_model(m)
                         and not m.startswith("free/")
                         and cls._fallback_allowed(m)]
        if reason == "rate_limit":
            # платный резерв роли раньше free-карусели: один хоп вместо
            # перебора :free-моделей, которые лимитятся тем же аккаунтом.
            # За free/нативным резервом — нативные модели роли: свои
            # пулы {LABEL}_API_KEYS, не сгорают с бесплатным шлюзом.
            order = [free, yaml_fb] + free_models + native_models
        else:
            order = [yaml_fb, free] + free_models + native_models
        out: list[str] = []
        for model in order:
            if model and model != failed and model not in out \
                    and not cls.is_blacklisted(model) \
                    and cls._fallback_allowed(model):
                out.append(model)
        return out

    @classmethod
    def _fallback_allowed(cls, model: str) -> bool:
        """Резерв только бесплатный или нативный: при пустом балансе
        OpenRouter платный fallback (deepseek и пр.) сжигает ключи
        впустую — лучше безопасный HOLD, чем пустые запросы.
        """
        if cls.is_free_model(model):
            return True
        if (model or "").startswith("free/"):
            return True          # бесплатный пул APInex (free/*)
        # нативные бесплатные провайдеры: Mercury, Groq, SambaNova,
        # AGNES, Mistral — свои аккаунты и свои лимиты
        return cls.resolve(model).provider in (
            "inception", "groq", "sambanova", "agnes", "mistral")

    @staticmethod
    def _log(message: str) -> None:
        print(message, flush=True)

    @classmethod
    def complete(cls, *, role: str, model: str, messages: list[dict],
                 temperature: float = 0.1, max_tokens: int = 400,
                 response_format: dict | None = None,
                 _quota_retried: bool = False) -> str:
        """Запрос к LLM с защитой от трат и отказов.

        Квота :free-моделей — 20 запросов в минуту на аккаунт (шлюз);
        при исчерпании запрос уходит на резерв роли, а не в 429.
        402/429 → LLM_MODEL (бесплатный дефолт) с логом «[llm] Rate-limit,
        fallback to free model»; 404/пустой ответ → резерв роли с логом
        «[llm] Fallback triggered for {role}». 402 (нет баланса) гасит
        ключ пула сразу; 404/429 — после трёх отказов подряд модель уходит
        в чёрный список на 15 минут — к ней больше не ходят запросы.
        Возвращает текст ответа.
        """
        queue: list[str] = [model]
        attempted: dict[str, int] = {}
        tried_keys: dict[str, set] = {}
        caps: dict[str, int] = {}
        last_exc: Exception | None = None
        while queue:
            current = queue.pop(0)
            route = cls.resolve(current)
            pool = cls._key_pool(route.base_url, route.requested)
            # модель можно повторить: на каждый ключ пула + один
            # squeeze-повтор с ужатым max_tokens
            if attempted.get(current, 0) >= max(2, len(pool) + 1):
                continue
            tk = tried_keys.setdefault(current, set())
            keys = pool or ([route.api_key] if route.api_key else [])
            avail = [k for k in keys if k not in tk]
            if not avail and attempted.get(current, 0) > 0:
                continue                    # все ключи модели уже пробовали
            if avail:
                route = _replace(route, api_key=avail[0])
                tk.add(avail[0])
            attempted[current] = attempted.get(current, 0) + 1
            if cls.is_blacklisted(current, route.api_key, route.base_url):
                # модель в чёрном списке — запрос не тратим
                reason_txt = cls.blacklist_reason(current, route.api_key,
                                                  route.base_url)
                if cls._blacklist_hit(current) or \
                        not [k for k in pool if k not in tk]:
                    # модель мёртвая целиком (или ключи кончились)
                    cls._log(f"[llm] Skipped blacklisted model {current} "
                             f"(15 min, {reason_txt})")
                    queue.extend(cls._fallbacks(role, current, reason_txt))
                    continue
                # мёртв только ключ пула — модель жива, идём на следующий ключ
                cls._log(f"[llm] Skipped blacklisted key "
                         f"{cls._key_mask(route.api_key)} of {current} "
                         f"(15 min, {reason_txt}) → следующий ключ пула")
                queue.insert(0, current)
                continue
            if not route.live:
                last_exc = RuntimeError(
                    f"no LLM key for {route.provider}/{route.model}")
                queue.extend(cls._fallbacks(role, current, None))
                continue
            if cls.is_free_model(current):
                # квота 20 RPM — на ключ, а не на аккаунт: ротация
                acct = f"{route.base_url}|{route.api_key[-6:]}"
                used = cls.free_rpm_used(acct)
                if used >= FREE_RPM_LIMIT:
                    if any(k not in tk for k in pool):
                        queue.insert(0, current)   # следующий ключ — своя квота
                        continue
                    cls._log(f"[llm] Free-model quota reached ({current} "
                             f"{used}/{FREE_RPM_LIMIT} RPM) → fallback")
                    queue.extend(cls._fallbacks(role, current, None))
                    continue
                cls.note_free_call(acct)
            mt = max_tokens
            client = None
            try:
                client = cls._acquire_cached_client(route)
                kwargs: dict = {}
                if route.provider == "inception":
                    # mercury-*: с response_format отвечает пустотой/обрывом
                    mt = max(mt, 600)
                elif response_format is not False:
                    kwargs["response_format"] = response_format or {
                        "type": "json_object"}
                if cls.is_free_model(current):
                    # бесплатные модели тратят токены на reasoning: без запаса
                    # обрезанный JSON не распарсится (reason до 200 символов)
                    mt = max(mt, 800)
                mt = min(mt, caps.get(current, mt))
                resp = client.chat.completions.create(
                    model=route.model, temperature=temperature,
                    max_tokens=mt, messages=messages, **kwargs)
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise _EmptyResponse("empty response")
                cls._clear_strikes(current)
                return content
            except Exception as e:  # noqa: BLE001 — уходим на резерв
                last_exc = e
                reason = cls._reason_of(e)
                status = cls._status_of(e)
                if reason == "rate_limit" and status == 402:
                    # 402 «requires more credits… can only afford N»:
                    # повтор ТОЙ ЖЕ модели с ужатым max_tokens — но только
                    # если бюджет ≥ 400 токенов: меньше — JSON обрежется,
                    # смысла нет, уходим на следующую модель/резерв
                    afford = cls._afford_of(e)
                    budget = afford - 16
                    if budget >= MIN_SQUEEZE_TOKENS and budget < mt:
                        caps[current] = budget
                        cls._log(f"[llm] Token-budget squeeze: {current} "
                                 f"max_tokens→{budget} (afford {afford})")
                        # The same key must be eligible for this one squeezed retry.
                        tk.discard(route.api_key)
                        queue.insert(0, current)
                        continue
                    if any(k not in tk for k in pool):
                        # ключ без баланса — гасим его и идём на следующий
                        cls._block_model(current, status, route.api_key,
                                        pool, tk, route.base_url)
                        cls._log(f"[llm] Key rotation: {current} "
                                 f"→ следующий ключ пула")
                        queue.insert(0, current)
                        continue
                if status in (404, 429) or reason == "empty":
                    # три отказа подряд — модель в чёрный список на 15 минут
                    n = cls._count_strike(current)
                    if n >= BLACKLIST_STRIKES:
                        cls.blacklist_model(current, reason)
                        detail = (f"{status} {reason}" if status is not None
                                  else "empty response")
                        cls._log(f"[llm] Blacklisted model {current} for 15 min "
                                 f"({n}× {detail})")
                    elif status is not None:
                        cls._log(f"[llm] {status} {current} "
                                 f"({n}/{BLACKLIST_STRIKES}) → fallback")
                    else:
                        cls._log(f"[llm] Empty response from {current} "
                                 f"({n}/{BLACKLIST_STRIKES}) → fallback")
                elif status == 402:
                    # не хватило баланса — гасим ключ пула или модель
                    cls._block_model(current, status, route.api_key, pool,
                                     tk, route.base_url)
                targets = cls._fallbacks(role, current, reason)
                if not targets:
                    retry = cls._quota_sleep_retry(
                        exc=e, role=role, model=model, messages=messages,
                        temperature=temperature, max_tokens=max_tokens,
                        response_format=response_format,
                        quota_retried=_quota_retried)
                    if retry is not None:
                        return retry
                    raise
                target = targets[0]
                if reason == "rate_limit":
                    cls._log(f"[llm] Rate-limit, fallback to free model: "
                             f"{current} → {target}")
                cls._log(f"[llm] Fallback triggered for {role}: "
                         f"{current} → {target}")
                queue.extend(targets)
            finally:
                if client is not None:
                    cls._release_cached_client(client)
        retry = cls._quota_sleep_retry(
            exc=last_exc, role=role, model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, quota_retried=_quota_retried)
        if retry is not None:
            return retry
        raise last_exc or RuntimeError("no usable LLM route")

    @classmethod
    def _quota_sleep_retry(cls, *, exc, role: str, model: str,
                           messages: list[dict], temperature: float,
                           max_tokens: int, response_format,
                           quota_retried: bool) -> str | None:
        """Все маршруты упёрлись в лимиты API (429/402 по всем ключам
        и резервам) — цикл не роняем: спим до конца RPM-окна и делаем
        один повторный проход. Повторный сон не чаще QUOTA_SLEEP_S,
        иначе 13 агентов проспали бы по 15 минут каждый."""
        sleep_s = float(env("LLM_QUOTA_SLEEP_S") or QUOTA_SLEEP_S)
        if cls._reason_of(exc) != "rate_limit" or quota_retried or \
                sleep_s <= 0 or \
                time.time() - cls._quota_slept_at < sleep_s:
            return None
        cls._log("[llm] Quota exhausted. Sleeping for 15 mins...")
        time.sleep(sleep_s)
        cls._quota_slept_at = time.time()
        cls.reset_free_calls()
        cls.reset_blacklist()
        return cls.complete(role=role, model=model, messages=messages,
                            temperature=temperature, max_tokens=max_tokens,
                            response_format=response_format,
                            _quota_retried=True)

    # --- лог -----------------------------------------------------------

    @classmethod
    def active_model(cls) -> str:
        """Имя модели, которая сейчас активна (после отката)."""
        return cls.resolve().model

    @classmethod
    def log_lines(cls, route: LLMRoute | None = None, *,
                  new_only: bool = True) -> list[str]:
        """Строки лога о выборе модели. Откат печатается один раз."""
        r = route or cls.resolve()
        out: list[str] = []
        if r.fallback:
            line = f"[llm] Fallback to DeepSeek ({r.reason})"
            if r.provider == "openai-compatible":
                line += f" → резерв {r.base_url}: {r.model}"
            elif not r.live:
                line += " → ключей LLM нет, режим stub"
            out.append(line)
        elif not r.live:
            out.append(f"[llm] No LLM key for {r.provider} — режим stub")
        if new_only:
            out = [ln for ln in out if ln not in cls._logged]
            cls._logged.update(out)
        return out

    @classmethod
    def reset_log(cls) -> None:
        """Сбросить память «уже напечатанных» строк (тесты)."""
        cls._logged.clear()


# Модульные обёртки: код роя импортирует resolve/log_lines напрямую.
def resolve(model: str | None = None) -> LLMRoute:
    """Маршрут активной модели — короткая форма LLMFactory.resolve."""
    return LLMFactory.resolve(model)


def active_model() -> str:
    """Активная модель — короткая форма LLMFactory.active_model."""
    return LLMFactory.active_model()


def get_client(route: "LLMRoute | str | None" = None):
    """OpenAI-клиент — короткая форма LLMFactory.get_client."""
    return LLMFactory.get_client(route)


def client_for(api_key: str, base_url: str):
    """Клиент произвольного шлюза — короткая форма LLMFactory.client_for."""
    return LLMFactory.client_for(api_key, base_url)


def log_lines(route: LLMRoute | None = None, *,
              new_only: bool = True) -> list[str]:
    """Строки лога о выборе модели — короткая форма LLMFactory.log_lines."""
    return LLMFactory.log_lines(route, new_only=new_only)


def reset_log() -> None:
    """Сброс памяти «уже напечатанных» строк — короткая форма."""
    LLMFactory.reset_log()
