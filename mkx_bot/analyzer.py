"""
Фундаментальная логика анализа Cybernagual.

Ключевые идеи:
  * Фрактальность: закономерности повторяются на разных уровнях фильтрации.
  * Коридоры времени: сутки делятся на 288 пятиминутных коридоров,
    каждый со своей вероятностью исхода.
  * Пересечение срезов: сигнал выдается только если несколько
    независимых фильтров (персонаж, коридор, волна, пересечение
    коэффициентов) дают совместное подтверждение.

В данном модуле собрана «оркестрация» фильтров. Таблица вероятностей
коридоров заполняется фактическими данными по мере накопления истории
в SQLite — до этого используется усредненная эвристика.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .db_manager import DBManager
from .models import Match


# ---------- утилиты времени -------------------------------------------------

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(s: str) -> dtime:
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"Bad HH:MM: {s!r}")
    return dtime(hour=int(m.group(1)), minute=int(m.group(2)))


def is_dead_minute(dt: datetime, dead_spec: Tuple[str, ...]) -> bool:
    """Проверка попадания момента в "мертвые минуты".

    Поддерживает записи формата "HH:MM" (точное совпадение минуты) и
    "HH:MM-HH:MM" (интервал включительно).
    """
    cur = dt.time().replace(second=0, microsecond=0)
    for spec in dead_spec:
        if "-" in spec:
            a, b = spec.split("-", 1)
            ta, tb = _parse_hhmm(a), _parse_hhmm(b)
            if ta <= cur <= tb:
                return True
        else:
            if cur == _parse_hhmm(spec):
                return True
    return False


def corridor_index(dt: datetime, step_min: int = 5) -> int:
    """Порядковый номер временного коридора (0..287) для шага 5 минут."""
    total_minutes = dt.hour * 60 + dt.minute
    return total_minutes // step_min


# --------- таблица вероятностей коридоров ----------------------------------

@dataclass
class CorridorStats:
    """Эмпирическая таблица вероятностей.

    Заполняется из БД. Для каждого (corridor_index, kind) хранится
    counter и positives. Вероятность = positives / counter.
    """
    kind: str  # 'F' | 'B' | 'ANY'
    probabilities: Dict[int, float] = field(default_factory=dict)
    samples: Dict[int, int] = field(default_factory=dict)

    DEFAULT_PROB = 0.08

    def prob(self, idx: int) -> float:
        return self.probabilities.get(idx, self.DEFAULT_PROB)

    def has_enough(self, idx: int, min_samples: int = 30) -> bool:
        return self.samples.get(idx, 0) >= min_samples


def build_corridor_stats(db: DBManager, kind: str = "F") -> CorridorStats:
    """Считает эмпирическую вероятность добивания типа kind для каждого
    коридора на основании завершенных матчей из БД."""
    stats = CorridorStats(kind=kind)
    with db._conn() as c:  # noqa: SLF001 — узкое место, пользуемся приватным доступом
        cur = c.execute(
            """SELECT m.match_ts, m.match_time,
                      SUM(CASE WHEN r.finisher = ? THEN 1 ELSE 0 END) AS positives
                 FROM matches m
                 LEFT JOIN rounds r ON r.match_no = m.match_no
                WHERE m.finished = 1
                GROUP BY m.match_no""",
            (kind,),
        )
        for row in cur.fetchall():
            if not row["match_time"]:
                continue
            try:
                dt = datetime.fromisoformat(row["match_time"])
            except Exception:
                continue
            idx = corridor_index(dt)
            stats.samples[idx] = stats.samples.get(idx, 0) + 1
            positives_bool = 1 if (row["positives"] or 0) > 0 else 0
            p_new = (
                stats.probabilities.get(idx, 0.0) * (stats.samples[idx] - 1)
                + positives_bool
            ) / stats.samples[idx]
            stats.probabilities[idx] = p_new
    return stats


# ---------- результат анализа ----------------------------------------------

@dataclass
class AnalysisResult:
    enter: bool
    kind: str                         # 'FATALITY' | 'BRUTALITY' | ''
    reasons: Dict[str, str] = field(default_factory=dict)
    blockers: Dict[str, str] = field(default_factory=dict)
    corridor: Optional[int] = None
    prob: Optional[float] = None


# ---------- сам аналитик ----------------------------------------------------

class Analyzer:
    """Реализует методологию «Золотого догона v3.0» из ТЗ."""

    def __init__(self, db: DBManager, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ---------- элементарные проверки --------------------------------------

    @staticmethod
    def _name_matches(name: Optional[str], candidates) -> bool:
        """Точное совпадение: имя (без пробелов/апострофов/регистра)
        равно любому из кандидатов после такой же нормализации."""
        if not name:
            return False
        def norm(s: str) -> str:
            return (
                s.lower()
                .replace(" ", "")
                .replace("'", "")
                .replace("`", "")
                .replace("-", "")
            )
        n = norm(name)
        return any(norm(c) == n for c in candidates)

    def _is_executioner(self, name: Optional[str]) -> bool:
        return self._name_matches(name, self.settings.executioners)

    def _is_donor(self, name: Optional[str]) -> bool:
        return self._name_matches(name, self.settings.donors)

    def razor_wave_blocked(self) -> Optional[bool]:
        """True  — последние N матчей без добиваний, вход заблокирован.
        False — всё ок.
        None  — недостаточно данных."""
        return self.db.last_n_all_dry(self.settings.razor_wave_len)

    def dead_minute(self, dt: datetime) -> bool:
        return is_dead_minute(dt, self.settings.dead_minutes)

    def coefficient_slice_match(self, m: Match, ref: Match) -> bool:
        """«Срез коэффициентов ±0.1»: два матча считаются в одном срезе,
        если их коэффициенты на матч отличаются не более чем на 0.1."""
        if None in (m.p1m, m.p2m, ref.p1m, ref.p2m):
            return False
        return abs(m.p1m - ref.p1m) <= 0.1 and abs(m.p2m - ref.p2m) <= 0.1

    # ---------- главный сценарий ------------------------------------------

    def evaluate_new_match(self, m: Match) -> AnalysisResult:
        """Принимает только что созданный (или отредактированный до полных
        коэффициентов) матч. Принимает решение: входить в сигнал или нет."""

        result = AnalysisResult(enter=False, kind="FATALITY")

        # 0. Требуем, чтобы были заполнены обе пары имен и коэффициенты
        if not all([m.p1_name_ru or m.p1_name_en,
                    m.p2_name_ru or m.p2_name_en,
                    m.p1m, m.p2m]):
            result.blockers["incomplete"] = "недостаточно данных в сообщении"
            return result

        # 1. Персонажи: П1 — Палач, П2 — Донор
        p1 = m.p1_name_ru or m.p1_name_en
        p2 = m.p2_name_ru or m.p2_name_en
        if not self._is_executioner(p1):
            result.blockers["char_p1"] = f"{p1} не входит в список Палачей"
        else:
            result.reasons["char_p1"] = f"Палач: {p1}"
        if not self._is_donor(p2):
            result.blockers["char_p2"] = f"{p2} не входит в список Доноров"
        else:
            result.reasons["char_p2"] = f"Донор: {p2}"

        # 2. Время матча и зависящие от него фильтры
        if m.match_time:
            try:
                dt = datetime.fromisoformat(m.match_time)
            except Exception:
                dt = datetime.now()
        else:
            dt = datetime.now()

        if self.dead_minute(dt):
            result.blockers["dead_minute"] = (
                f"{dt.strftime('%H:%M')} — мертвая минута"
            )
        else:
            result.reasons["dead_minute"] = "вне мертвых минут"

        # 3. Временной коридор
        idx = corridor_index(dt, self.settings.corridor_minutes)
        result.corridor = idx
        stats = build_corridor_stats(self.db, kind="F")
        prob = stats.prob(idx)
        result.prob = prob
        if stats.has_enough(idx):
            if prob < 0.2:
                result.blockers["corridor"] = (
                    f"коридор {idx}: P(F)={prob:.2f} — ниже порога"
                )
            else:
                result.reasons["corridor"] = (
                    f"коридор {idx}: P(F)={prob:.2f}"
                )
        else:
            # Данных мало — доверяем остальным фильтрам, но отмечаем
            result.reasons["corridor"] = (
                f"коридор {idx}: данных мало ({stats.samples.get(idx, 0)})"
            )

        # 4. Волна Бритья
        rw = self.razor_wave_blocked()
        if rw is True:
            result.blockers["razor_wave"] = (
                f"последние {self.settings.razor_wave_len} матчей — без добиваний"
            )
        elif rw is False:
            result.reasons["razor_wave"] = "Волна Бритья не активна"
        else:
            result.reasons["razor_wave"] = "недостаточно истории"

        # 5. Пересечение: требуем минимум 3 положительных фактора и ни одного блокера
        positive = len(result.reasons)
        result.enter = (not result.blockers) and positive >= 3

        return result
