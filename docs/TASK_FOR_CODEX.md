# TASK_FOR_CODEX — NearIntentPayer stub

ЦЕЛЬ: связать успешный ответ Research Agent с оплатой через заглушку NEAR Intents.

## Что сделать

1. Скопировать `tools/research_agent_demo.py` из `D:\hackathon-research\tools\` в этот репо (`tools/`).
2. Добавить класс `NearIntentPayer`:
   - `pay(worker_id: str, task_id: str, amount: float) -> dict`
   - возвращает receipt `{"intent_id": "intent-<hash>", "solver": worker_id, "status": "settled", "amount": amount, "ts": <unix>}`
   - чистая заглушка — без сети, без кошелька; `intent_id` = sha256(worker+task+ts)[:16]
3. В `run_research(query)`:
   - после успешного `cite_check` (0 unverified) вызвать `payer.pay(worker_id="research-agent", task_id=<task hash>, amount=1.0)`
   - добавить в возвращаемый dict поле `"payment": receipt`
   - если unverified>0 или sources пусты — `payment: null` (не платить за непроверенный ответ)
4. Ключи только через `os.environ` — новых env не требуется (stub).

## ПРОВЕРКА

- `python tools/research_agent_demo.py "test"` → в выводе есть `"payment"` с `intent_id` при мок-успехе.
- Когда sources пусты → `"payment": null`.
- Никаких сетевых вызовов NEAR.
