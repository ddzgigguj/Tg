"""
Конфигурация бота. Загружает .env (если установлен python-dotenv) и
выставляет все пороги строго в соответствии с первоисточниками —
уроками Cybernagual из docs/lessons/ и ТЗ из PDF.

Ссылки на источники:
  * docs/lessons/Fatality.md       — пороги P(F) по фатовости
  * docs/lessons/corridors.md      — пороги P(F) по коридорам
  * docs/lessons/FatBrut.md        — кросс-фильтрация и длительность 1-го раунда
  * docs/lessons/mk-lesson-two.md  — критерии правильного среза
  * docs/lessons/intuition_vs_stats.md, comparison_approaches.md — обоснование
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv — опциональная зависимость
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _parse_signal_chat(raw: str) -> str | int:
    """SIGNAL_CHAT может быть:
      - "@username" публичной группы/канала,
      - "https://t.me/…" — вырежем @username,
      - числовой chat_id супергруппы ("-1001234567890") или лички ("123456").
    Возвращаем подходящий для send_message объект: int или строку.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("https://t.me/"):
        s = "@" + s.rsplit("/", 1)[-1]
    # Чисто число (возможно с минусом)
    try:
        return int(s)
    except ValueError:
        pass
    if not s.startswith("@"):
        s = "@" + s
    return s


@dataclass(frozen=True)
class Settings:
    # ---------- интеграции Telegram ------------------------------------------

    telethon_api_id: int = _env_int("TELETHON_API_ID", 0)
    telethon_api_hash: str = _env("TELETHON_API_HASH", "") or ""
    telethon_session: str = _env("TELETHON_SESSION", "mkx_session") or "mkx_session"

    source_channel: str = _env("SOURCE_CHANNEL", "@statamk10") or "@statamk10"

    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN", "") or ""
    signal_chat: str | int = field(
        default_factory=lambda: _parse_signal_chat(_env("SIGNAL_CHAT", "") or "")
    )

    db_path: str = _env("DB_PATH", "mkx_bot.db") or "mkx_bot.db"

    # ---------- банк и ставки (ТЗ §4, «Золотой догон v3.0») -----------------

    start_balance: float = float(_env("START_BALANCE", "1000") or 1000)
    bet_r1: float = float(_env("BET_R1", "100") or 100)
    bet_r2: float = float(_env("BET_R2", "220") or 220)
    bet_r3: float = float(_env("BET_R3", "480") or 480)

    # ВНИМАНИЕ: никакой настройки TIMEZONE. Время матча (для коридоров и
    # "мёртвых минут") берётся исключительно из первой строки сообщения
    # канала — "HH:MM DD-MM-YYYY". Канал сам задаёт локальную шкалу.

    # ---------- стратегия «Золотой догон v3.0» из ТЗ -------------------------

    # Персонажи-Палачи (П1) — высокая частота Fatality слева.
    executioners: Tuple[str, ...] = field(
        default_factory=lambda: (
            "Джейсон", "Милина", "Райдэн", "Рептилия", "Триборг",
            "Jason", "Mileena", "Raiden", "Reptile", "Triborg",
        )
    )

    # Персонажи-Доноры (П2) — часто «отдают» Fatality справа.
    donors: Tuple[str, ...] = field(
        default_factory=lambda: (
            "Соня Блейд", "СоняБлейд", "Джакс", "Кэсси Кейдж", "КэссиКейдж",
            "Ди'Вора", "ДиВора", "Ди Вора",
            "Sonya Blade", "Sonya", "Jax", "Cassie Cage", "Cassie",
            "D'Vorah", "Dvorah",
        )
    )

    # Мёртвые минуты из ТЗ: либо точные HH:MM, либо диапазон HH:MM-HH:MM.
    dead_minutes: Tuple[str, ...] = field(
        default_factory=lambda: (
            "03:00-04:30",
            "12:10",
            "12:40",
            "13:15",
            "23:50-23:59",
        )
    )

    # Волна Бритья: последние N завершённых матчей без F и без B → блок.
    razor_wave_len: int = 3

    # 288 коридоров по 5 минут.
    corridor_minutes: int = 5

    # ---------- пороги из уроков Cybernagual --------------------------------

    # docs/lessons/corridors.md: коридор подходит, если P(F) в целевом
    # диапазоне раундов > MIN. Оптимально > OPTIMAL.
    corridor_prob_min: float = 0.65
    corridor_prob_optimal: float = 0.88

    # docs/lessons/Fatality.md: желательно, чтобы вероятность была сильно
    # разной между 1..3 и 4..6 (т.е. коридор «указывает» на конкретный
    # диапазон). Порог разницы — не менее +10 процентных пунктов.
    corridor_range_margin: float = 0.10

    # docs/lessons/corridors.md: порог «фатовости» персонажа по самой
    # большой выборке (последние 50 матчей), индексированный по диапазону
    # коэффициента на Fatality.
    fatovost_threshold_by_coef: Dict[str, float] = field(
        default_factory=lambda: {
            "2-2.99": 0.80,
            "3-3.99": 0.70,
            "4-4.99": 0.60,
        }
    )

    # Остальные выборки (30/10/5 матчей) — не должны резко расходиться
    # с основной (разница ≤ этого допуска).
    fatovost_volatility_max: float = 0.30

    # Минимальная «надёжная» выборка для корректной оценки коридора.
    corridor_min_samples: int = 30

    # Допуск для кросс-фильтрации по коэффициентам (FatBrut): ± шаг.
    coef_slice_step: float = 0.10

    # «Базовая закономерность»: начало нового диапазона коэффициента
    # (напр. 2.0–2.19) имеет большую проходимость, чем конец предыдущего
    # (напр. 4.7–4.94). Пороги: внутри одного целого диапазона коэффициент
    # не должен быть слишком близко к концу.
    tail_of_range_threshold: float = 0.85  # последние 15% диапазона = risk

    # Длина первого раунда — фильтр из FatBrut. Считается критичным, если
    # в срезе есть совпадение с точностью ± этого значения.
    first_round_duration_tolerance: int = 3  # секунд

    # Оптимальный размер среза: 1..5 матчей (mk-lesson-two).
    slice_min_size: int = 1
    slice_max_size: int = 6


settings = Settings()
