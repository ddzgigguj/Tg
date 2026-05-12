"""
Учет виртуальных ставок и профита.

Ставки по ТЗ:
  Р1 = 100, Р2 = 220, Р3 = 480 (относительно стартового баланса 1000).
Масштабирование: ставки пропорциональны текущему балансу.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .db_manager import DBManager


@dataclass(frozen=True)
class BetSizes:
    r1: float
    r2: float
    r3: float


class BalanceManager:
    def __init__(
        self,
        db: DBManager,
        start_balance: float,
        base_r1: float,
        base_r2: float,
        base_r3: float,
    ) -> None:
        self.db = db
        self.start_balance = start_balance
        self.base = BetSizes(base_r1, base_r2, base_r3)

    # ---------------------------------------------------------------- balance
    def current(self) -> float:
        return self.db.get_current_balance(default=self.start_balance)

    def scaled_bets(self) -> BetSizes:
        """Ставки пропорциональны текущему балансу: base * (current/start)."""
        bal = self.current()
        factor = bal / self.start_balance if self.start_balance else 1.0
        return BetSizes(
            r1=round(self.base.r1 * factor, 2),
            r2=round(self.base.r2 * factor, 2),
            r3=round(self.base.r3 * factor, 2),
        )

    # ------------------------------------------------------------------ bets
    def apply_profit(self, delta: float, reason: str) -> float:
        """Мутация баланса. Возвращает новый баланс."""
        new_balance = self.current() + delta
        self.db.append_balance(new_balance, delta, reason)
        return new_balance

    def settle(
        self,
        *,
        win: bool,
        stage: int,
        coefficient: float,
        reason: str,
    ) -> float:
        """Проводим расчет исхода одного шага "Золотого догона".

        Args:
            win: победа (True) или проигрыш (False) на этом раунде.
            stage: 1 | 2 | 3 — номер попытки в догоне.
            coefficient: коэффициент, на котором ставили.
            reason: человекочитаемое пояснение.
        Returns:
            Новый баланс после расчета.
        """
        bets = self.scaled_bets()
        stake = {1: bets.r1, 2: bets.r2, 3: bets.r3}.get(stage, bets.r1)
        if win:
            delta = round(stake * (coefficient - 1.0), 2)
        else:
            delta = -stake
        return self.apply_profit(delta, reason=f"{reason}|stage{stage}|stake{stake}")

    # -------------------------------------------------------------- reporting
    def summary(self) -> dict:
        return {
            "start": self.start_balance,
            "current": self.current(),
            "bets": self.scaled_bets().__dict__,
        }
