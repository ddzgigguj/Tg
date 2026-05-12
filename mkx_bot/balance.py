"""
Учёт виртуальных ставок и профита для «Золотого догона v3.0» (ТЗ §4).

  * Стартовый баланс 1000. Ставки Р1=100, Р2=220, Р3=480.
  * Масштабирование: при изменении баланса размер ставок пропорционально
    меняется относительно стартового значения.
  * Догон идёт внутри целевого диапазона раундов (1..3 либо 4..6).
    На каждом шаге (stage) — своя ставка: stage 1 → Р1, stage 2 → Р2,
    stage 3 → Р3.
  * Если в ходе догона заходит Fatality — шаги закрываются, прибыль
    рассчитывается по коэффициенту на Fatality текущего матча.
  * Если диапазон закрыт без Fatality — все три ставки проиграны.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .db_manager import DBManager


@dataclass(frozen=True)
class BetSizes:
    r1: float
    r2: float
    r3: float

    def for_stage(self, stage: int) -> float:
        return {1: self.r1, 2: self.r2, 3: self.r3}.get(stage, self.r1)


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

    # ---------------------------------------------------------------- bank

    def current(self) -> float:
        return self.db.get_current_balance(default=self.start_balance)

    def scaled_bets(self) -> BetSizes:
        bal = self.current()
        # Ограничиваем масштаб неотрицательным значением: после глубокой
        # просадки баланс может уйти в минус — это не должно превратить
        # ставки в "отрицательные" (иначе проигрыш вдруг начнёт давать +Δ).
        if self.start_balance <= 0:
            k = 1.0
        else:
            k = max(bal, 0.0) / self.start_balance
        return BetSizes(
            r1=round(self.base.r1 * k, 2),
            r2=round(self.base.r2 * k, 2),
            r3=round(self.base.r3 * k, 2),
        )

    def apply_delta(self, delta: float, reason: str) -> float:
        new_balance = round(self.current() + delta, 2)
        self.db.append_balance(new_balance, delta, reason)
        return new_balance

    # ------------------------------------------------- settle a dogon cycle

    def settle_dogon(
        self,
        *,
        rounds_in_range: List[Tuple[int, str]],  # [(round_no, finisher), ...]
        fat_coefficient: float,
        match_no: str,
    ) -> Tuple[str, float, int | None]:
        """Закрывает «Золотой догон» по факту сыгранного целевого диапазона.

        `rounds_in_range` — кортежи (номер раунда, тип финишера 'F'/'B'/'R'),
        отсортированные по возрастанию round_no. Если Fatality случается на
        stage k (k=1..3), то stages 1..k-1 проиграны (−ставка), stage k
        выигран (+ставка · (кф−1)); дальнейшие раунды не торгуем.

        Возвращает (result_label, total_delta, win_round_or_None).

        Особые случаи:
          * пустой `rounds_in_range` → ('VOID', 0.0, None). Бывает, если
            матч вообще не дошёл до целевого диапазона (например, сигнал
            на 4–6, а матч закончился на 3-м раунде). Ни одна ставка не
            делалась, поэтому это не проигрыш и не выигрыш.
          * `fat_coefficient` ≤ 1.0 защищён: чистый P/L по Fatality может
            стать отрицательным при кф < 1, но в этом модуле мы такое не
            допускаем и возвращаем ('VOID', 0.0, None) — это явная
            аномалия данных, сигнал обрабатывать нельзя.
        """
        if not rounds_in_range:
            return ("VOID", 0.0, None)
        if fat_coefficient is None or fat_coefficient <= 1.0:
            return ("VOID", 0.0, None)

        bets = self.scaled_bets()
        stage = 0
        total_delta = 0.0
        win_round: int | None = None
        for (rn, fin) in rounds_in_range:
            stage += 1
            if stage > 3:
                break
            stake = bets.for_stage(stage)
            if fin == "F":
                delta = round(stake * (fat_coefficient - 1.0), 2)
                total_delta += delta
                self.apply_delta(delta, f"{match_no} dogon WIN stage{stage} r{rn}")
                win_round = rn
                return ("WIN", round(total_delta, 2), win_round)
            else:
                # Brutality / Regular: для догона на Fatality это проигрыш.
                delta = -stake
                total_delta += delta
                self.apply_delta(delta, f"{match_no} dogon LOSS stage{stage} r{rn}")

        # Все три шага отыграны без Fatality: догон проигран.
        return ("LOSS", round(total_delta, 2), None)

    # --------------------------------------------------------- reporting

    def summary(self) -> dict:
        b = self.scaled_bets()
        return {
            "start": self.start_balance,
            "current": self.current(),
            "bets": {"r1": b.r1, "r2": b.r2, "r3": b.r3},
        }
