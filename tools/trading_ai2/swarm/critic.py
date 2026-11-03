"""critic.py — Критик: второй ум роя.

Смотрит на предложение Генератора и ищет слабые места: перегрев RSI,
новостной фон против сделки, отсутствие стоп-лосса, слабую
уверенность роя. Возражения двух уровней:

  block — сделку отменять (Арбитр обязан отменить);
  warn  — замечание (Арбитр решает сам).

Критик ничего не исполняет: он только говорит.
"""

from __future__ import annotations

from dataclasses import dataclass

from .generator import Proposal

RSI_OVERHEATED = 70.0        # перегрев — покупать нельзя
PANIC_SCORE = -0.5           # новостной фон в панике
WEAK_SCORE = 0.6             # ниже — уверенности роя нет

#: Жёсткие абсолютные лимиты — действуют поверх любого профиля риска
#: Арбитра: даже в AGGRESSIVE маржа портфеля ≤20% equity и стоп не шире
#: 1.5% от входа. Профиль может предложить больше — Критик обязан резать.
PORTFOLIO_MARGIN_CAP_PCT = 20.0
ABSOLUTE_STOP_PCT = 1.5


@dataclass
class Objection:
    level: str                # block | warn
    text: str

    @property
    def blocking(self) -> bool:
        return self.level == "block"


class Critic:
    def __init__(self, rsi_overheated: float = RSI_OVERHEATED,
                 panic_score: float = PANIC_SCORE,
                 weak_score: float = WEAK_SCORE):
        self.rsi_overheated = rsi_overheated
        self.panic_score = panic_score
        self.weak_score = weak_score

    def review(self, proposal: Proposal, *, rsi: float | None = None,
               sentiment_score: float | None = None,
               sentiment_mode: str = "",
               tech_signal: str | None = None) -> list[Objection]:
        """Возражения по предложению. Пустой список — претензий нет."""
        out: list[Objection] = []
        if proposal.action == "HOLD":
            return out
        buying = proposal.action == "BUY"

        if buying and proposal.stop_loss <= 0:
            out.append(Objection(
                "block", "нет стоп-лосса — риск без границы"))

        if buying and rsi is not None and rsi > self.rsi_overheated:
            out.append(Objection(
                "block", f"RSI {rsi:.1f} — покупка на перегреве"))

        if (buying and sentiment_score is not None
                and sentiment_score < self.panic_score):
            mode = sentiment_mode or "n/a"
            out.append(Objection(
                "block", f"новостной фон {sentiment_score:+.2f} ({mode}) — "
                         "против покупки"))

        if abs(proposal.score) < self.weak_score:
            out.append(Objection(
                "warn", f"уверенность роя слабая {proposal.score:+.2f} — "
                        "края нет"))

        if buying and tech_signal == "SELL":
            out.append(Objection(
                "warn", "тренд вниз (EMA9<21), а предлагаем покупку"))

        if (buying and sentiment_score is not None
                and self.panic_score <= sentiment_score < 0):
            out.append(Objection(
                "warn", f"фон скорее негативный {sentiment_score:+.2f}"))

        return out

    # ------------------------------------------------- жёсткие лимиты
    def harden(self, con, *, equity_usd: float,
               margin_in_use_usd: float) -> list[str]:
        """Абсолютные лимиты поверх вердикта Арбитра.

        Агрессивный профиль может просить 5% на сделку — Критик урезает
        до свободного остатка под 20%-кап портфельной маржи и переписывает
        стоп-лосс шире ±1.5%. Мутирует con; вернёт строки для лога.
        """
        notes: list[str] = []
        if not getattr(con, "approved", False):
            return notes
        eq = float(equity_usd or 0.0)
        if eq > 0:
            free_pct = max(0.0, PORTFOLIO_MARGIN_CAP_PCT
                           - float(margin_in_use_usd or 0.0) / eq * 100.0)
            if float(con.amount_percent) > free_pct:
                con.amount_percent = free_pct
                notes.append(f"Size reduced to {free_pct:.1f}% "
                             "(portfolio margin limit reached)")
        sl = float(getattr(con, "stop_loss", 0.0) or 0.0)
        if abs(sl) > ABSOLUTE_STOP_PCT:
            con.stop_loss = ABSOLUTE_STOP_PCT * (-1.0 if sl < 0 else 1.0)
            notes.append(f"stop-loss capped to {con.stop_loss:+.1f}% "
                         f"({ABSOLUTE_STOP_PCT}% absolute limit)")
        return notes
