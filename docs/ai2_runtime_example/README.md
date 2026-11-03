# AI-2 runtime example — Research Agent

`research_agent.py` здесь — **reference implementation from the main
NEXUS AiOS runtime** (дословная копия `ai2/runtime/research_agent.py`
из приватного репозитория).

Файл не запускается отдельно — он импортирует рантайм-контекст
(`..models`, `.roles`). Его назначение для судей — показать, как тот же
агент-цикл работает внутри рантайма:

- `roles.call_model(ctx, "executor", ...)` — вызовы модели идут через
  бюджет, метрики и router_choice рантайма, а не напрямую в HTTP;
- `ctx.traj.emit("tool_call", tool="tavily_search", ...)` — поиск как
  инструмент-событие с длительностью и retrieval scores;
- `ctx.traj.emit("research_iteration", step="plan|search|observe|answer")`
  — полный цикл агента в траектории прогона, а не один анонимный вызов;
- честный отказ без ключа Tavily до первого вызова модели.

Рабочая standalone-версия этого же цикла (тот же промпт-стек, дедуп
запросов, честные отказы) — `../../tools/research_agent.py`, клиенты —
`../../tools/research_agent_clients.py`. Резолв ключей повторяет
семантику рантайма: файл настроек `AI2_ENV_FILE` перекрывает окружение,
ключ выбирается по URL endpoint'а из пула `AI2_PROVIDER_KEYS_JSON`.
