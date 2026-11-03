"""Реальные клиенты Research Agent: Tavily Search + OpenAI-совместимый LLM.

Зависимости: httpx при наличии → requests → urllib (stdlib). Всегда
работает хотя бы одна цепочка — в изолированной среде хакатона ставить
пакеты может быть нельзя.

Источники ключей. env(name) идёт по именам — сначала прямое, потом
алиасы ai2 (см. _ALIASES); для каждого имени слои: файл настроек AI-2
(AI2_ENV_FILE, $env:X = "...", перекрывает окружение — семантика
ai2/env_loader) → os.environ → .env проекта. Прямое имя бьёт алиас
на любом слое: .env с LLM_* = явный конфиг standalone (Cohere/OpenRouter),
без LLM_* агент наследует настройки рантайма (mercury-3/inceptionlabs).

LLM_API_KEY — без алиасов: ключ выбирается по URL endpoint'а
(_llm_api_key), иначе чужой ключ уходит на несвой шлюз → 401.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- env (.env)

_AI2_CACHE: dict[str, str] | None = None
_DOTENV_CACHE: dict[str, str] | None = None

# Алиасы: имя агента → имена в настройках ai2 (тот же слот, другое имя).
# LLM_API_KEY намеренно без алиасов: ключ выбирается по URL endpoint'а
# (_llm_api_key), а не по имени переменной — иначе чужой ключ уходит
# на несвой шлюз и получаем 401.
_ALIASES = {
    "LLM_BASE_URL": ("OPENAI_BASE_URL",),
    "LLM_MODEL": ("AI2_MODEL_EXECUTOR",),
    "TAVILY_API_KEY": ("AI2_TAVILY_API_KEY",),
}

_PS1_ASSIGN = re.compile(r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]*)['\"]\s*$")


def _load_ps1(path: Path) -> dict[str, str]:
    """Простые $env:X = "значение" из env-файла ai2 — тот же формат,
    что разбирает ai2/env_loader.py. Значения с $/` пропускаются."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _PS1_ASSIGN.match(line)
        if m and "$" not in m.group(2) and "`" not in m.group(2):
            out.setdefault(m.group(1), m.group(2))
    return out


def _ai2_settings() -> dict[str, str]:
    """Файл настроек AI-2 (AI2_ENV_FILE). По семантике env_loader он
    перекрывает os.environ — это единственный источник правды для
    endpoint'ов и имён моделей (меркурий живёт в env.local.ps1, а
    user-env может держать другой шлюз)."""
    global _AI2_CACHE
    if _AI2_CACHE is not None:
        return _AI2_CACHE
    out: dict[str, str] = {}
    spec = os.environ.get("AI2_ENV_FILE", "").strip()
    if spec and spec.lower() != "off":
        p = Path(spec).expanduser()
        if p.is_file():
            out = _load_ps1(p)
    _AI2_CACHE = out
    return out


def _dotenv() -> dict[str, str]:
    """.env проекта — последний фолбэк после настроек ai2 и os.environ."""
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE
    out: dict[str, str] = {}
    # .env.local (личные секреты, gitignored) перекрывает .env.
    for base in (Path.cwd(), Path(__file__).resolve().parents[1]):
        for name in (".env.local", ".env"):
            f = base / name
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    _DOTENV_CACHE = out
    return out


def _lookup(name: str) -> str:
    """Три слоя для одного имени: файл настроек ai2 → os.environ → .env."""
    return (_ai2_settings().get(name) or os.environ.get(name)
            or _dotenv().get(name, ""))


def _normalize_endpoint(endpoint: str) -> str:
    """scheme://authority/path: host в нижний регистр, дефолтный порт
    опущен — как _normalize_provider_endpoint в ai2/models/openai_compat."""
    p = urlsplit(endpoint.strip())
    scheme = p.scheme.lower() or "https"
    host = (p.hostname or "").lower()
    default_port = 80 if scheme == "http" else 443
    authority = host if p.port is None or p.port == default_port else f"{host}:{p.port}"
    return f"{scheme}://{authority}{p.path.rstrip('/')}"


def _provider_pool() -> dict[str, list[str]] | None:
    """AI2_PROVIDER_KEYS_JSON → {нормализованный endpoint: [ключи]}.
    None — переменная не задана; {} — задана, но не распарсилась."""
    raw = _lookup("AI2_PROVIDER_KEYS_JSON").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return out
    for endpoint, keys in data.items():
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list):
            continue
        try:
            norm = _normalize_endpoint(endpoint)
        except (ValueError, TypeError):
            continue
        out[norm] = [k.strip() for k in keys
                     if isinstance(k, str) and k.strip()]
    return out


def _llm_api_key() -> str:
    """Ключ под конкретный LLM_BASE_URL (семантика resolve_openai_env):
    прямой LLM_API_KEY → пул AI2_PROVIDER_KEYS_JSON по URL (если пул задан —
    он авторитетен; промах = честный «нет ключа», чужой не подставляем) →
    OPENAI_API_KEY → INCEPTION_API_KEY (когда пула нет совсем)."""
    direct = _lookup("LLM_API_KEY")
    if direct:
        return direct
    pool = _provider_pool()
    if pool is not None:
        url = env("LLM_BASE_URL") or "https://api.openai.com/v1"
        keys = pool.get(_normalize_endpoint(url), [])
        return keys[0] if keys else ""
    return _lookup("OPENAI_API_KEY") or _lookup("INCEPTION_API_KEY")


def env(name: str, default: str = "") -> str:
    if name == "LLM_API_KEY":
        return _llm_api_key() or default
    for n in (name, *_ALIASES.get(name, ())):
        v = _lookup(n)
        if v:
            return v
    return default


# ------------------------------------------------------------- HTTP transport


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout_s: float) -> dict[str, Any]:
    """POST JSON → dict. httpx → requests → urllib по очереди."""
    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except ImportError:
        pass
    try:
        import requests  # type: ignore
        r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
        r.raise_for_status()
        return r.json()
    except ImportError:
        pass
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e


def _with_retry(fn, attempts: int = 3, backoff_s: float = 1.5, label: str = "запрос"):
    """Retry с экспоненциальной паузой; последняя ошибка поднимается."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — ретраим всё транспортное
            last = e
            if i < attempts - 1:
                time.sleep(backoff_s * (2**i))
    raise RuntimeError(f"{label}: {attempts} попытки, последняя ошибка — {last}")


def _sanitize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Убирает сообщения с пустым/None content — провайдеры (Cohere и др.)
    отклоняют запросы с пустыми сообщениями HTTP 400."""
    return [m for m in messages
            if isinstance(m.get("content"), str) and m["content"].strip()]


# -------------------------------------------------------------------- clients


class TavilyClient:
    """Tavily Search API → список результатов [{title, url, content, score}]."""

    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None, timeout_s: float = 30.0,
                 max_results: int = 5):
        self.api_key = api_key or env("TAVILY_API_KEY")
        self.timeout_s = timeout_s
        self.max_results = max_results

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY не задан — поиск недоступен")
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": self.max_results,
            "search_depth": "basic",
            "include_answer": False,
        }
        data = _with_retry(
            lambda: _post_json(self.ENDPOINT, payload, {}, self.timeout_s),
            label="tavily.search",
        )
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0.0),
            }
            for r in data.get("results", [])
        ]


class LLMClient:
    """OpenAI-совместимый /chat/completions → text."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout_s: float = 60.0,
                 temperature: float = 0.2):
        self.api_key = api_key or env("LLM_API_KEY")
        self.base_url = (base_url or env("LLM_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or env("LLM_MODEL") or "gpt-4o-mini"
        self.timeout_s = timeout_s
        self.temperature = temperature

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict[str, str]], *,
             max_tokens: int = 1200,
             temperature: float | None = None) -> str:
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY не задан — модель недоступна "
                "(проверь AI2_PROVIDER_KEYS_JSON: для активного endpoint'а "
                "нужна своя запись)")
        clean = _sanitize_messages(messages)
        if not clean:
            raise RuntimeError("llm.chat: все сообщения пустые — запрос не отправлен")
        payload = {
            "model": self.model,
            "messages": clean,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = _with_retry(
            lambda: _post_json(f"{self.base_url}/chat/completions", payload,
                               headers, self.timeout_s),
            label="llm.chat",
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"llm.chat: неожиданный ответ {str(data)[:200]}") from e
