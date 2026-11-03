"""news_sentiment.py — ИИ-аналитик новостного фона (LLM).

Тень «Угодничество» в shadow_redteam.py раньше получала новостной фон
как булев флаг news_negative — то есть решение «фон плохой» принимал
кто-то снаружи (или человек). Здесь его принимает модель: LLM читает
последние заголовки и ставит оценку от -1.0 (паника) до +1.0 (эйфория).

Источники заголовков: CryptoPanic (если есть ключ) → CoinDesk RSS
(публичный, без ключа) → локальный мок-лист.

Провайдер оценки: активная модель из llm_factory.LLMFactory
(ACTIVE_LLM_MODEL — DeepSeek / GPT / Claude / шлюз репозитория) через
openai SDK; если её ключа нет, берётся OpenAI-совместимый эндпоинт
репозитория (LLM_API_KEY / LLM_BASE_URL). Без ключей вообще модуль
честно отдаёт mode="stub": оценка по лексикону заголовков. Режим всегда
указан в результате — stub никогда не притворяется live.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from research_agent_clients import env, _provider_pool
except ImportError:
    from tools.research_agent_clients import env, _provider_pool

try:
    from .llm_factory import LLMFactory
except ImportError:  # pragma: no cover — запуск без пакета
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from swarm.llm_factory import LLMFactory

try:
    from .json_sanitizer import _safe_parse_json
except ImportError:  # pragma: no cover — запуск без пакета
    from swarm.json_sanitizer import _safe_parse_json

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
#: Бесплатные ленты по активу (без ключа): актив → тег Cointelegraph.
#: Уровень 2 новостей без CryptoPanic-ключа иначе получил бы общую
#: ленту — и два сентимента совпали бы, то есть разницы между
#: уровнями не было бы вовсе.
ASSET_RSS_TAGS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp",
    "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche",
    "DOT": "polkadot", "TRX": "tron", "LTC": "litecoin",
    "POL": "polygon", "LINK": "chainlink",
}
ASSET_RSS_URL = "https://cointelegraph.com/rss/tag/{tag}"

HEADLINES_COUNT = 3
HTTP_TIMEOUT_S = 8.0
# RSS-лента CoinDesk — около 30 КБ; на медленном канале 8 с не хватает.
RSS_TIMEOUT_S = 25.0

# Порог «фон плохой» — ниже сентимент считается паникой.
PANIC_SCORE = -0.5

# Кэш оценки: новости меняются медленно, LLM звать каждый тик — дорого.
# TTL берётся из .env (NEWS_CACHE_TTL_MIN, по умолчанию 30 минут).
NEWS_CACHE_TTL_MIN = 30.0
CACHE_TTL_S = NEWS_CACHE_TTL_MIN * 60.0
NEWS_CACHE_TTL_ENV = "NEWS_CACHE_TTL_MIN"
# По-активный сентимент (News Engine v2.0): кэш на символ, TTL 45 минут.
ASSET_NEWS_TTL_MIN = 45.0
ASSET_TTL_S = ASSET_NEWS_TTL_MIN * 60.0
ASSET_TTL_ENV = "ASSET_NEWS_TTL_MIN"

SYSTEM_PROMPT = (
    "Ты — риск-аналитик крипто-фонда. Оцени новостной фон по шкале "
    "от -1.0 (паника: взломы, обвалы, запреты, отзывы лицензий) "
    "до +1.0 (эйфория: рекорды, одобрения, институциональные покупки), "
    "0.0 — нейтрально. Верни СТРОГО JSON без markdown: "
    '{"sentiment": float от -1.0 до 1.0, "summary": str до 120 символов}. '
    "Никакого текста вокруг JSON."
)

# Заголовки на случай, если новостной API недоступен.
MOCK_HEADLINES = [
    "Binance finds more breaches in $6.3 billion hack; AI spending fears hit tech",
    "Binance hacker dumps a little-known 2017 token and it crashes 90%",
    "Strategy CFO Andrew Kang resigns as company posts record operating profit",
]

# Лексикон для stub-режима: без ключа LLM оценка считается по словам.
_NEG_WORDS = ("hack", "breach", "crash", "exploit", "scam", "fraud",
              "ban", "banned", "lawsuit", "sec ", "outage", "freeze",
              "liquidation", "dump", "panic", "collapse", "crackdown")
_POS_WORDS = ("surge", "rally", "approval", "approved", "adoption",
              "partnership", "upgrade", "inflow", "breakthrough",
              "record high", "bullish", "etf", "institutional")


@dataclass
class SentimentResult:
    """Оценка новостного фона. score ∈ [-1.0, 1.0]."""
    score: float = 0.0
    mode: str = "stub"                # live | stub
    summary: str = ""
    headlines: list[str] = field(default_factory=list)
    provider: str = ""                # deepseek | openai | anthropic |
                                      # openai-compatible | lexicon
    model: str = ""
    error: str = ""
    from_cache: bool = False          # ответ из 15-минутного кэша
    age_mins: int = 0                 # возраст закэшированной оценки

    @property
    def is_panic(self) -> bool:
        return self.score < PANIC_SCORE

    @property
    def label(self) -> str:
        if self.score < PANIC_SCORE:
            return "PANIC"
        if self.score > 0.5:
            return "EUPHORIA"
        return "NEUTRAL"


def _http_get(url: str, headers: dict | None = None,
              timeout: float = HTTP_TIMEOUT_S,
              attempts: int = 2) -> bytes:
    """GET с повтором: RSS-ленты иногда отдаются по 20+ секунд."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            e.close()
            last = e
        except Exception as e:  # noqa: BLE001 — повторяем, потом отдаём мок
            last = e
    raise last if last else RuntimeError("http failed")


def fetch_headlines(symbol: str = "BTC", count: int = HEADLINES_COUNT
                    ) -> tuple[list[str], str]:
    """Последние заголовки по инструменту. → (заголовки, источник)."""
    token = env("CRYPTOPANIC_API_KEY")
    if token:
        try:
            # пустой символ → глобальная лента без фильтра по тикеру
            cur = f"&currencies={symbol.upper()}" if symbol else ""
            url = f"{CRYPTOPANIC_URL}?auth_token={token}{cur}"
            data = json.loads(_http_get(url).decode("utf-8"))
            titles = [p.get("title", "").strip()
                      for p in data.get("results", []) if p.get("title")]
            if titles:
                return titles[:count], "cryptopanic"
        except Exception:  # noqa: BLE001 — нет ключа/сети → следующий источник
            pass
    tag = ASSET_RSS_TAGS.get(symbol.upper()) if symbol else None
    if tag:
        try:
            root = ET.fromstring(_http_get(ASSET_RSS_URL.format(tag=tag),
                                           timeout=RSS_TIMEOUT_S))
            titles = [(item.findtext("title") or "").strip()
                      for item in root.iter("item")]
            titles = [t for t in titles if t]
            if titles:
                return titles[:count], f"cointelegraph/{tag}"
        except Exception:  # noqa: BLE001 — лента не легла → общая
            pass
    try:
        root = ET.fromstring(_http_get(COINDESK_RSS, timeout=RSS_TIMEOUT_S))
        titles = [(item.findtext("title") or "").strip()
                  for item in root.iter("item")]
        titles = [t for t in titles if t]
        if titles:
            return titles[:count], "coindesk"
    except Exception:  # noqa: BLE001 — оффлайн → мок
        pass
    return list(MOCK_HEADLINES[:count]), "mock"


def _lexicon_score(headlines: list[str]) -> float:
    """Fallback без LLM: тональность заголовков по словарю."""
    blob = " ".join(headlines).lower()
    neg = sum(1 for w in _NEG_WORDS if w in blob)
    pos = sum(1 for w in _POS_WORDS if w in blob)
    if neg == pos:
        return 0.0
    return round((pos - neg) / max(1, pos + neg), 3)


class ParseError(ValueError):
    """Ответ модели не разобран.

    Отличается от сетевой/платёжной ошибки: чинить ответ бессмысленно
    (провайдер обрезал JSON), и звать другие модели мы не будем —
    лимит не сжигаем впустую, отдаём честный stub по лексикону.
    """


def _parse_sentiment(raw: str) -> tuple[float, str]:
    """LLM-ответ → (score, summary). Мусор → ParseError.

    Обрезанный ответ чинится: _safe_parse_json дописывает закрывающие
    кавычки и скобки. Если и после этого поля sentiment нет — ParseError.
    """
    data = _safe_parse_json(raw)
    if not data or "sentiment" not in data:
        raise ParseError(f"не разобран JSON ответа: {str(raw)[:60]}")
    try:
        score = float(data["sentiment"])
    except (TypeError, ValueError):
        raise ParseError(f"sentiment не число: {data['sentiment']!r}") from None
    summary = str(data.get("summary", ""))[:120]
    return max(-1.0, min(1.0, score)), summary


def _ttl_s(env_name: str, default_s: float) -> float:
    """TTL кэша в секундах: env (минуты) → дефолт."""
    raw = (env(env_name) or "").strip()
    if not raw:
        return default_s
    try:
        return max(0.0, float(raw)) * 60.0
    except ValueError:
        return default_s


def _cache_ttl_s() -> float:
    """TTL глобального кэша (NEWS_CACHE_TTL_MIN в .env, 30 мин)."""
    return _ttl_s(NEWS_CACHE_TTL_ENV, CACHE_TTL_S)


def _asset_ttl_s() -> float:
    """TTL по-активного кэша (ASSET_NEWS_TTL_MIN в .env, 45 мин)."""
    return _ttl_s(ASSET_TTL_ENV, ASSET_TTL_S)


def _resolve_providers() -> list[tuple[str, str, str, str]]:
    """Кандидаты (api_key, base_url, model, provider) в порядке приоритета.

    DeepSeek — как просил заказчик. Дальше OpenRouter и пул провайдеров
    репозитория (AI2_PROVIDER_KEYS_JSON): модель в пуле подбирается по
    /models — там нет deepseek-chat, и жёстко зашитое имя молча упало бы
    в 404. Список, а не один: заблокированный ключ не должен ронять
    аналитику в stub, пока живой провайдер есть рядом.
    """
    out: list[tuple[str, str, str, str]] = []
    ds_key = env("DEEPSEEK_API_KEY")
    if ds_key:
        out.append((ds_key,
                    env("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL,
                    env("DEEPSEEK_MODEL") or DEEPSEEK_MODEL,
                    "deepseek"))
    key = env("LLM_API_KEY")
    if key:
        out.append((key,
                    env("LLM_BASE_URL") or "https://openrouter.ai/v1",
                    env("LLM_MODEL") or "gpt-4o-mini",
                    "openai-compatible"))
    for endpoint, keys in (_provider_pool() or {}).items():
        if not keys:
            continue
        for model in _pick_models(endpoint, keys[0]):
            out.append((keys[0], endpoint, model, "openai-compatible"))
    # один ключ на провайдера достаточно: следующий ключ того же пула
    # падает с той же 403, а модель в списке уже перебрана
    seen: set[tuple[str, str]] = set()
    uniq = []
    for api_key, base_url, model, provider in out:
        if (base_url, model) in seen:
            continue
        seen.add((base_url, model))
        uniq.append((api_key, base_url, model, provider))
    return uniq


def _pick_models(endpoint: str, api_key: str) -> list[str]:
    """Chat-модели провайдера по /models, дешёвые первыми.

    Жёстко зашитое имя модели молча упало бы в 404: в пуле репозитория
    deepseek-chat нет. Возвращаем несколько — у провайдера одни модели
    без квоты, другие без JSON-режима, и одна попытка не должна
    ронять аналитику в stub.
    """
    try:
        data = json.loads(_http_get(
            f"{endpoint}/models",
            {"Authorization": f"Bearer {api_key}"},
            timeout=15.0).decode("utf-8"))
    except Exception:  # noqa: BLE001 — нет /models → не этот провайдер
        return []
    ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    for wanted in ("mercury-3", "deepseek-chat", "gpt-4o-mini"):
        if wanted in ids:
            return [wanted]
    chat = [i for i in ids
            if not any(t in i.lower() for t in ("image", "video", "embed"))]
    # дешёвые/быстрые первыми: у apihub quota-0 модели стоят в конце
    chat.sort(key=lambda i: (0 if any(t in i.lower() for t in
                                       ("flash", "mini", "haiku")) else 1, i))
    return chat[:3]


def _ask_llm(client, model: str, symbol: str, headlines: list[str]) -> str:
    """Один запрос к LLM → сырой текст ответа.

    JSON-режим просим первым; модель без него (ответ в ```json) — второй
    попыткой без response_format: парсер всё равно снимает обёртку.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(
            {"symbol": symbol.upper(), "headlines": headlines},
            ensure_ascii=False)},
    ]
    try:
        r = client.chat.completions.create(
            model=model, temperature=0.1, max_tokens=300,
            messages=messages, response_format={"type": "json_object"})
    except Exception:  # noqa: BLE001 — нет JSON-режима → обычный запрос
        r = client.chat.completions.create(
            model=model, temperature=0.1, max_tokens=300, messages=messages)
    return r.choices[0].message.content or ""


#: Кэш глобального фона: символ → (время запроса, результат).
_NEWS_CACHE: dict[str, tuple[float, SentimentResult]] = {}
#: Кэш по-активного фона (v2.0): символ → (время, результат).
_ASSET_CACHE: dict[str, tuple[float, SentimentResult]] = {}


def _cached(cache: dict, key: str, ttl_s: float) -> SentimentResult | None:
    """Закэшированная оценка: копия с from_cache/age_mins — первый
    (свежий) результат не должен помечаться как закэшированный."""
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return replace(hit[1], from_cache=True,
                       age_mins=int((time.time() - hit[0]) / 60))
    return None


def _evaluate(sym: str, headlines: list[str]) -> SentimentResult:
    """LLM-оценка заголовков → SentimentResult (общее ядро обоих уровней).

    Без ключа LLM (или при сбое API) — режим "stub": оценка по лексикону
    заголовков, mode честно помечен как stub. Неразобранный ответ модели
    (обрезанный JSON) — тоже stub: другие модели не зовём.
    """
    route = LLMFactory.resolve()
    prov: list[tuple[str, str, str, str]] = []
    if route.live:
        # активная модель из ACTIVE_LLM_MODEL — первым кандидатом
        prov.append((route.api_key, route.base_url, route.model,
                     route.provider))
    for api_key, base_url, model, provider in _resolve_providers():
        if any(base_url == p[1] and model == p[2] for p in prov):
            continue
        prov.append((api_key, base_url, model, provider))
    if not prov:
        result = SentimentResult(score=_lexicon_score(headlines), mode="stub",
                                 summary="no LLM key — lexicon stub",
                                 headlines=list(headlines),
                                 provider="lexicon",
                                 model=route.model)
    else:
        result = None
        errors: list[str] = []
        for api_key, base_url, model, provider in prov:
            client = None
            try:
                client = LLMFactory.client_for(api_key, base_url)
                score, summary = _parse_sentiment(
                    _ask_llm(client, model, sym, headlines))
                result = SentimentResult(score=score, mode="live",
                                         summary=summary,
                                         headlines=list(headlines),
                                         provider=provider, model=model)
                break
            except ParseError as e:
                # ответ не разобран: чинить и звать другие модели смысла
                # нет — лимит не сжигаем впустую
                errors.append(f"{provider}: {str(e)[:80]}")
                break
            except Exception as e:  # noqa: BLE001 — следующий провайдер
                errors.append(f"{provider}: {str(e)[:80]}")
            finally:
                if client is not None:
                    LLMFactory._close_client(client)
        if result is None:
            result = SentimentResult(score=_lexicon_score(headlines),
                                     mode="stub",
                                     summary=f"llm_error: {errors[0][:110]}",
                                     headlines=list(headlines),
                                     provider="lexicon",
                                     model=route.model,
                                     error="; ".join(errors)[:300])
    return result


def get_market_sentiment(symbol: str = "BTC",
                         headlines: list[str] | None = None,
                         use_cache: bool = True) -> SentimentResult:
    """Глобальный новостной фон рынка: -1.0 (паника) … +1.0 (эйфория).

    Уровень 1 News Engine v2.0: общие новости (без привязки к тикеру —
    передавай symbol="" для глобальной ленты). Кэш — NEWS_CACHE_TTL_MIN
    (по умолчанию 30 минут). Из кэша результат идёт с from_cache=True
    и age_mins — TUI это видит.
    """
    sym = symbol.upper() or "GLOBAL"
    if use_cache and headlines is None:
        hit = _cached(_NEWS_CACHE, sym, _cache_ttl_s())
        if hit is not None:
            return hit
    if headlines is None:
        headlines, _source = fetch_headlines(symbol)
    result = _evaluate(sym, headlines)
    if use_cache:
        _NEWS_CACHE[sym] = (time.time(), result)
    return result


def get_asset_sentiment(symbol: str,
                        headlines: list[str] | None = None,
                        use_cache: bool = True) -> SentimentResult:
    """Уровень 2: сентимент конкретного актива (SOL, ETH…).

    CryptoPanic с фильтром currencies=SYMBOL → общий RSS → мок.
    Отдельный кэш _ASSET_CACHE на символ, TTL — ASSET_NEWS_TTL_MIN
    (по умолчанию 45 минут). Зовётся только для активов портфеля —
    лимиты не жгутся на весь скринер-лист.
    """
    sym = symbol.upper()
    if use_cache and headlines is None:
        hit = _cached(_ASSET_CACHE, sym, _asset_ttl_s())
        if hit is not None:
            return hit
    if headlines is None:
        headlines, _source = fetch_headlines(sym)
    result = _evaluate(sym, headlines)
    if use_cache:
        _ASSET_CACHE[sym] = (time.time(), result)
    return result


def invalidate_cache(reason: str = "") -> dict:
    """Emergency reset: сброс обоих кэшей (ATR-спайк, гэп цены, сдвиг
    глобального фона). Возвращает, что сброшено — для лога."""
    cleared = {"global": len(_NEWS_CACHE), "assets": len(_ASSET_CACHE)}
    _NEWS_CACHE.clear()
    _ASSET_CACHE.clear()
    return cleared


def sentiment_for(symbol: str = "BTC") -> float:
    """Только число — для вызова в горячем пути."""
    return get_market_sentiment(symbol).score


def reset_cache() -> None:
    """Сброс кэшей — для тестов и ручного перезапроса."""
    _NEWS_CACHE.clear()
    _ASSET_CACHE.clear()
