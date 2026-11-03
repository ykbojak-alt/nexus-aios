# Research Agent — архитектура MVP (ODSC × Nebius)

Трек: Research Agent. Критерии: 30% цитируемость/проверяемость, 10% использование Nebius Token Factory + Tavily.

## Пайплайн

```
Вопрос пользователя
  → search_tavily(query)            — Tavily API: до 8 результатов {title, url, content, published}
  → filter_context(results)         — дедупликация по домену, обрезка content до ~1200 символов на источник, нумерация [1..n]
  → synthesize_nebius(query, ctx)   — Nebius Token Factory (OpenAI-compatible API): синтез ответа строго по контексту
  → cite_check(answer, sources)     — верификация: каждое утверждение несёт [Source: URL]; URL сверяется со списком источников
  → Ответ пользователю + блок «Источники»
```

## Связывание утверждений с URL (критично, 30%)

1. Каждый источник получает индекс и URL ещё до синтеза: `{i, url, title, content}`.
2. Системный промпт обязывает: каждое предложение с фактом заканчивается маркером `[Source: <url>]`.
3. `cite_check` после синтеза:
   - извлекает все `[Source: X]` из ответа;
   - X должен быть одним из полученных URL → иначе предложение помечается `⚠ unverified` или удаляется;
   - предложения без маркера, содержащие фактические утверждения — помечаются.
4. Формат ответа: текст с инлайн-ссылками + раздел `### Источники` (нумерованный список url + title).
5. Fallback: если Tavily вернул 0 результатов или контекст пуст — агент отвечает «В доступных источниках информации не найдено» и НЕ генерирует факты.

## Компоненты

- `tools/research_agent_demo.py` — каркас: `search_tavily`, `synthesize_nebius`, `cite_check`, `run_research`.
- Модель синтеза: Nebius Token Factory endpoint (OpenAI-compatible), env `NEBIUS_API_KEY`, `NEBIUS_MODEL`.
- Поиск: `TAVILY_API_KEY`, endpoint `https://api.tavily.com/search`.

## Безопасность/честность

- Ключи только через `os.environ`.
- Ответ без источников = проваленный ответ; лучше отказ, чем галлюцинация.
