"""AI2 Live Trading Agent — LLM-решения через Risk Manager.

Слои: market_data (ccxt+индикаторы) → strategy_agent (DeepSeek→JSON→
Pydantic) → loop_guard (лимиты, обязательный gate перед биржей) →
executor (paper/testnet через ccxt + SQLite). LLM никогда не трогает
биржу напрямую — только через loop_guard.
"""
