"""
Аналайзер «Золотого догона v3.0», построенный непосредственно на
материалах Cybernagual (docs/lessons/).

Каркас решения воспроизводит последовательность шагов из урока
"Анализ коридоров времени и персонажей" (docs/lessons/corridors.md):

  1. По времени начала матча определяется коридор (288 × 5 мин).
     Для каждого целевого диапазона раундов (1..3 и 4..6) из истории
     считается P(F). Коридор проходит, если P(F) > corridor_prob_min,
     и только для того диапазона, где P(F) заметно выше другого
     (margin из config).

  2. Для выбранной стороны (П1 — слева, П2 — справа) проверяется
     «фатовость» персонажа (docs/lessons/Fatality.md): P(F) в
     соответствующем диапазоне раундов и бакете коэффициента
     (2–2.99 / 3–3.99 / 4–4.99) на 4-х выборках. Порог — из config.

  3. «Базовая закономерность» (docs/lessons/Fatality.md §Коэффициентный
     анализ): если кф на Fatality находится в конце своего целого
     диапазона (напр. 2.94), шансы меньше, чем у начала следующего
     (напр. 3.05). Ставим штраф, если коэффициент попадает в
     последние 15% диапазона.

  4. Кросс-фильтрация (docs/lessons/FatBrut.md §Объяснение
     противоречия): для ставки на Fatality срез правильнее строить
     по коэффициенту на Brutality, а не на Fatality. Используется
     для оценки схожести текущего матча с историей.

  5. Фильтры из ТЗ: «мёртвые минуты» (блок), «волна бритья» (блок
     при трёх подряд сухих матчах).

  6. Финальный сигнал: только если несколько независимых фильтров
     подтверждают один и тот же целевой диапазон.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .db_manager import DBManager, corridor_index, _coef_bucket
from .models import Match


# ---------- утилиты времени -------------------------------------------------

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(s: str) -> dtime:
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"Bad HH:MM: {s!r}")
    return dtime(hour=int(m.group(1)), minute=int(m.group(2)))


def is_dead_minute(dt: datetime, dead_spec: Tuple[str, ...]) -> bool:
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


# ---------- результат анализа ----------------------------------------------


@dataclass
class CorridorDecision:
    corridor: int
    p_fat_1_3: float
    p_fat_4_6: float
    samples_1_3: int
    samples_4_6: int
    target_range: Optional[str]       # '1-3' | '4-6' | None
    reason: str


@dataclass
class AnalysisResult:
    enter: bool
    kind: str                                  # 'FATALITY'
    target_range: str                          # '1-3' | '4-6' | ''
    reasons: Dict[str, str] = field(default_factory=dict)
    blockers: Dict[str, str] = field(default_factory=dict)
    corridor: Optional[int] = None
    corridor_prob: Optional[float] = None
    p1_fatovost: Optional[Dict[int, Tuple[int, float]]] = None
    p2_fatovost: Optional[Dict[int, Tuple[int, float]]] = None


# ---------- ядро ------------------------------------------------------------


class Analyzer:
    """Реализует методологию уроков Cybernagual + правила «Золотого догона»."""

    def __init__(self, db: DBManager, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # -------- нормализация имён ------------------------------------------

    @staticmethod
    def _norm(s: str) -> str:
        return (
            s.lower()
            .replace(" ", "")
            .replace("'", "")
            .replace("`", "")
            .replace("-", "")
        )

    @classmethod
    def _name_matches(cls, name: Optional[str], candidates) -> bool:
        if not name:
            return False
        n = cls._norm(name)
        return any(cls._norm(c) == n for c in candidates)

    def is_executioner(self, name: Optional[str]) -> bool:
        return self._name_matches(name, self.settings.executioners)

    def is_donor(self, name: Optional[str]) -> bool:
        return self._name_matches(name, self.settings.donors)

    # -------- фильтр Волна Бритья + мертвые минуты -----------------------

    def razor_wave_blocked(self) -> Optional[bool]:
        return self.db.last_n_all_dry(self.settings.razor_wave_len)

    def dead_minute(self, dt: datetime) -> bool:
        return is_dead_minute(dt, self.settings.dead_minutes)

    # -------- шаг 1: коридоры --------------------------------------------

    def analyze_corridor(self, dt: datetime) -> CorridorDecision:
        idx = corridor_index(dt, self.settings.corridor_minutes)
        n13, pF13, _, _ = self.db.corridor_stats(idx, "1-3")
        n46, pF46, _, _ = self.db.corridor_stats(idx, "4-6")

        target = None
        reason = ""

        if max(n13, n46) < self.settings.corridor_min_samples:
            reason = (
                f"коридор {idx}: истории мало "
                f"({n13}/{n46} матчей) — опираемся на остальные фильтры"
            )
            # Даже при пустой истории не блокируем, только отмечаем.
        else:
            min_p = self.settings.corridor_prob_min
            margin = self.settings.corridor_range_margin
            if pF13 >= min_p and pF13 - pF46 >= margin:
                target = "1-3"
                reason = (
                    f"коридор {idx}: P(F 1-3)={pF13:.2f} ≥ {min_p:.2f} "
                    f"и выше P(F 4-6)={pF46:.2f} на {pF13 - pF46:.2f}"
                )
            elif pF46 >= min_p and pF46 - pF13 >= margin:
                target = "4-6"
                reason = (
                    f"коридор {idx}: P(F 4-6)={pF46:.2f} ≥ {min_p:.2f} "
                    f"и выше P(F 1-3)={pF13:.2f} на {pF46 - pF13:.2f}"
                )
            else:
                reason = (
                    f"коридор {idx}: P(F 1-3)={pF13:.2f}, P(F 4-6)={pF46:.2f} "
                    f"— нет уверенного предпочтения"
                )
        return CorridorDecision(
            corridor=idx,
            p_fat_1_3=pF13, p_fat_4_6=pF46,
            samples_1_3=n13, samples_4_6=n46,
            target_range=target, reason=reason,
        )

    # -------- шаг 2: фатовость ------------------------------------------

    def fatovost_passes(
        self, windows: Dict[int, Tuple[int, float]], coef_bucket: str,
    ) -> Tuple[bool, str]:
        """Оценивает, проходит ли персонаж по фатовости.

        Правила из docs/lessons/corridors.md §Рекомендации по использованию
        таблиц для коэффициентов на Fatality:
          * ориентируемся на выборку 50 матчей (основная).
          * порог зависит от бакета кф на Fatality (80/70/60%).
          * остальные выборки не должны резко колебаться относительно
            основной (допуск — fatovost_volatility_max).
        """
        base_threshold = self.settings.fatovost_threshold_by_coef.get(
            coef_bucket, 0.7
        )
        n50, p50 = windows.get(50, (0, 0.0))
        if n50 == 0:
            return (False, f"выборка 50 матчей пуста — нельзя оценить")
        if p50 < base_threshold:
            return (False,
                    f"фатовость по 50-матчной выборке {p50:.2f} < {base_threshold:.2f}")
        max_delta = 0.0
        for w in (30, 10, 5):
            n, p = windows.get(w, (0, 0.0))
            if n == 0:
                continue
            max_delta = max(max_delta, abs(p - p50))
        if max_delta > self.settings.fatovost_volatility_max:
            return (False,
                    f"сильные колебания выборок (Δ={max_delta:.2f})")
        return (True,
                f"основная выборка {p50:.2f} ≥ {base_threshold:.2f}, "
                f"разброс Δ={max_delta:.2f}")

    # -------- шаг 3: «базовая закономерность» коэффициента ---------------

    def coef_base_law_passes(self, f_coef: Optional[float]) -> Tuple[bool, str]:
        """Докст-хвост диапазона (напр. 2.95..2.99, 3.95..3.99) — риск."""
        if f_coef is None:
            return (False, "кф на Fatality отсутствует в сообщении")
        # «Свой» целый диапазон — floor(f_coef)..floor+1.
        low = int(f_coef)
        fraction = (f_coef - low)  # 0..1 внутри диапазона
        if fraction >= self.settings.tail_of_range_threshold:
            return (
                False,
                f"кф {f_coef} — конец диапазона {low}.xx (хвост), "
                f"риск выше"
            )
        return (True, f"кф {f_coef} — начало/середина диапазона {low}.xx")

    # -------- шаг 4: кросс-фильтрация (FatBrut) --------------------------

    def crossfilter_evidence(self, m: Match) -> Tuple[bool, str]:
        """Проверяем, что матч хорошо «срезается» по методичке FatBrut:
        ищем историю с близким коэффициентом на Brutality (±step) и
        близкой длительностью первого раунда (±tolerance).

        Это компактная версия «правильного среза» из mk-lesson-two.md —
        мы не строим полный UI фильтрации, но убеждаемся, что слой
        истории вокруг матча существует. Если история пуста, критерий
        «срез с понятными алгоритмами» не выполнен.
        """
        if m.fbr_brutality is None:
            return (False, "кф на Brutality неизвестен — кросс-фильтр неприменим")
        step = self.settings.coef_slice_step

        # Ищем историю по близкому кф на Brutality.
        with self.db._conn() as c:  # noqa: SLF001
            cur = c.execute(
                """SELECT COUNT(*) AS n,
                          SUM(has_fatality_any) AS nf
                     FROM matches
                    WHERE finished=1
                      AND fbr_brutality IS NOT NULL
                      AND ABS(fbr_brutality - ?) <= ?""",
                (m.fbr_brutality, step),
            )
            row = cur.fetchone()
            n = int(row["n"] or 0)
            nf = int(row["nf"] or 0)

        if n < self.settings.slice_min_size:
            return (False, f"кросс-срез (BRUT ±{step}) пуст")
        if n > self.settings.slice_max_size * 20:
            # Просто пометим — срез слишком широкий, уроки говорят
            # о плохой фильтрации. Это не блок, а downgrade уверенности.
            return (True,
                    f"кросс-срез (BRUT ±{step}) слишком широкий: {n} матчей, "
                    f"P(F)={nf/n:.2f}")
        return (True, f"кросс-срез (BRUT ±{step}): {n} матчей, P(F)={nf/n:.2f}")

    # -------- главная точка входа ----------------------------------------

    def evaluate_new_match(self, m: Match) -> AnalysisResult:
        """Принимает только что заведённый (или отредактированный до
        полного набора коэффициентов) матч. Возвращает решение:
        входить ли в «Золотой догон» и на каком диапазоне раундов."""

        result = AnalysisResult(enter=False, kind="FATALITY", target_range="")

        # 0. Полнота данных
        p1 = m.p1_name_ru or m.p1_name_en
        p2 = m.p2_name_ru or m.p2_name_en
        if not all([p1, p2, m.p1m, m.p2m, m.fbr_fatality, m.fbr_brutality]):
            result.blockers["incomplete"] = "не все поля сообщения заполнены"
            return result

        # 1. Персонажи Палач/Донор
        if self.is_executioner(p1):
            result.reasons["char_p1"] = f"П1 — Палач: {p1}"
        else:
            result.blockers["char_p1"] = (
                f"П1={p1} — не Палач из списка стратегии «Золотой догон»"
            )
        if self.is_donor(p2):
            result.reasons["char_p2"] = f"П2 — Донор: {p2}"
        else:
            result.blockers["char_p2"] = (
                f"П2={p2} — не Донор из списка стратегии «Золотой догон»"
            )

        # 2. Время, dead minutes, razor wave
        try:
            dt = datetime.fromisoformat(m.match_time) if m.match_time else datetime.now()
        except Exception:
            dt = datetime.now()

        if self.dead_minute(dt):
            result.blockers["dead_minute"] = (
                f"{dt.strftime('%H:%M')} — мёртвая минута"
            )
        else:
            result.reasons["dead_minute"] = "вне мёртвых минут"

        rw = self.razor_wave_blocked()
        if rw is True:
            result.blockers["razor_wave"] = (
                f"последние {self.settings.razor_wave_len} матчей — "
                f"без F/B (Волна Бритья)"
            )
        elif rw is False:
            result.reasons["razor_wave"] = "Волна Бритья не активна"
        else:
            result.reasons["razor_wave"] = "истории для оценки Волны Бритья мало"

        # 3. Коридор
        cd = self.analyze_corridor(dt)
        result.corridor = cd.corridor
        if cd.target_range:
            result.reasons["corridor"] = cd.reason
            result.target_range = cd.target_range
            result.corridor_prob = (
                cd.p_fat_1_3 if cd.target_range == "1-3" else cd.p_fat_4_6
            )
        else:
            # При достаточной выборке без предпочтения — не блок,
            # но слабый сигнал: пробуем диапазон 1..3 по-умолчанию.
            if max(cd.samples_1_3, cd.samples_4_6) >= self.settings.corridor_min_samples:
                result.blockers["corridor"] = cd.reason
            else:
                result.reasons["corridor"] = cd.reason
                result.target_range = "1-3"
                result.corridor_prob = cd.p_fat_1_3 or 0.0

        # 4. Фатовость персонажей в нужном диапазоне и бакете кф
        coef_bucket = _coef_bucket(m.fbr_fatality) or "2-2.99"
        tr = result.target_range or "1-3"

        p1_win = self.db.character_fatovost(p1, "P1", tr, coef_bucket)
        p2_win = self.db.character_fatovost(p2, "P2", tr, coef_bucket)
        result.p1_fatovost = p1_win
        result.p2_fatovost = p2_win

        ok1, why1 = self.fatovost_passes(p1_win, coef_bucket)
        ok2, why2 = self.fatovost_passes(p2_win, coef_bucket)
        result.reasons["fatovost_p1"] = why1
        result.reasons["fatovost_p2"] = why2
        if not ok1:
            result.blockers["fatovost_p1"] = why1
        if not ok2:
            result.blockers["fatovost_p2"] = why2

        # 5. Базовая закономерность
        ok_coef, why_coef = self.coef_base_law_passes(m.fbr_fatality)
        if ok_coef:
            result.reasons["coef_base_law"] = why_coef
        else:
            result.blockers["coef_base_law"] = why_coef

        # 6. Кросс-фильтр FatBrut
        ok_cf, why_cf = self.crossfilter_evidence(m)
        if ok_cf:
            result.reasons["crossfilter"] = why_cf
        else:
            # При совсем пустой истории это слабый блок — в первые дни
            # работы бота лучше не выдавать сигнал вообще.
            result.blockers["crossfilter"] = why_cf

        # 7. Итог: ни одного блокера + target_range выбран
        result.enter = (not result.blockers) and bool(result.target_range)
        return result
