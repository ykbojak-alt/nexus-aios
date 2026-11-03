# AI2 Live Trading Agent (`tools/trading_ai2/`)

Слой 2: LLM принимает решения, Risk Manager — последнее слово, биржа
видит только одобренные ордера. **Testnet / paper только. Mainnet-исполнения
нет по архитектуре** — ключи mainnet в коде не предусмотрены.

## Поток

```
[data]  ccxt → OHLCV → RSI/MACD/EMA (pandas; pandas-ta опционален)
          │ snapshot JSON
[llm]   DeepSeek (openai SDK, base_url=api.deepseek.com) → строгий JSON
          → Pydantic TradeDecision; мусор / API-сбой → HOLD
[guard] RiskManager: позиция ≤2% equity · daily-loss ≤5% → halt до
          завтра · BUY без stop-loss — блок · стоп выше цены — блок
[execution]  только approved: paper-филлы или Binance Spot Testnet
          (set_sandbox_mode) → SQLite trades/positions + PnL
```

LLM физически не имеет объекта биржи — единственный `ccxt`-клиент с
ключами живёт в `Executor` за гейтом. Stop-loss открытой позиции
срабатывает на каждом тике **вне LLM** (`Executor.stop_out`).

## Запуск

```bash
pip install -r tools/trading_ai2/requirements.txt
cp tools/trading_ai2/.env.example .env   # testnet-ключи

python -m tools.trading_ai2.main --backtest 60 --timeframe 1h   # история, paper
python -m tools.trading_ai2.main --demo-guard                   # блокировка 50%-ордера
python -m tools.trading_ai2.main --once                         # 1 live-цикл (paper)
python -m tools.trading_ai2.main --mode testnet                 # цикл 5 мин, testnet
python tools/trading_ai2/test_trading_ai2.py                    # 15 тестов
```

Без `DEEPSEEK_API_KEY` стратегия работает в честном stub-режиме
(RSI-cross правила, `mode="stub"` в решениях — не маскируется под LLM).

## Env

| Переменная | Назначение |
|---|---|
| `ACTIVE_LLM_MODEL` | активная модель LLM: `deepseek-chat` / `gpt-4o` / `claude-3-5-sonnet` / id шлюза — переключается на лету, без перезапуска |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI-совместимый шлюз репозитория — дефолт и резерв переключателя |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | LLM-мозг |
| `BINANCE_API_KEY` / `BINANCE_SECRET` | **только** ключи testnet.binance.vision |
| `TRADING_MAX_POSITION_PCT` (2) / `TRADING_MAX_DAILY_LOSS_PCT` (5) / `TRADING_REQUIRE_STOP_LOSS` (1) | лимиты Risk Manager |

## Известные ограничения

- `pandas-ta` не собирается на Python 3.15 (numba <3.14) — индикаторы
  считает встроенный pandas-фолбэк (те же формулы Уайлдера/EMA).
- `day_start_equity` приблизительный: стартовый капитал + реализованный
  PnL до сегодня (консервативно — занижает, а не завышает лимит убытка).
- Публичный рыночный фид читает основной `api.binance.com` (котировки
  не требуют ключей); ордера — только `testnet.binance.vision`.
