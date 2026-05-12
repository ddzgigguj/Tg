"""
Конфигурация бота. Читает переменные окружения (поддержка .env через
python-dotenv, если установлен).

Все значения имеют разумные дефолты по ТЗ, чтобы модуль можно было
импортировать без настройки при запуске тестов/отладки.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv — опциональная зависимость
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


@dataclass(frozen=True)
class Settings:
    # Telethon (userbot) — нужен чтобы читать канал со статистикой
    telethon_api_id: int = _env_int("TELETHON_API_ID", 0)
    telethon_api_hash: str = _env("TELETHON_API_HASH", "") or ""
    telethon_session: str = _env("TELETHON_SESSION", "mkx_session") or "mkx_session"

    # Канал-источник
    source_channel: str = _env("SOURCE_CHANNEL", "@statamk10") or "@statamk10"

    # Bot API для отправки сигналов
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN", "") or ""
    signal_chat: str = _env("SIGNAL_CHAT", "") or ""

    # Хранилище
    db_path: str = _env("DB_PATH", "mkx_bot.db") or "mkx_bot.db"

    # Баланс и ставки
    start_balance: float = float(_env("START_BALANCE", "1000") or 1000)
    bet_r1: float = float(_env("BET_R1", "100") or 100)
    bet_r2: float = float(_env("BET_R2", "220") or 220)
    bet_r3: float = float(_env("BET_R3", "480") or 480)

    # Локальная TZ для фильтров
    timezone: str = _env("TIMEZONE", "Europe/Moscow") or "Europe/Moscow"

    # ------------------------------------------------------------------
    # Стратегические константы Cybernagual («Золотой догон» v3.0)
    # ------------------------------------------------------------------

    # Персонажи-Палачи (П1) — из ТЗ
    executioners: Tuple[str, ...] = field(
        default_factory=lambda: (
            "Джейсон", "Милина", "Райдэн", "Рептилия", "Триборг",
            # Английские эквиваленты для устойчивости парсера
            "Jason", "Mileena", "Raiden", "Reptile", "Triborg",
        )
    )

    # Персонажи-Доноры (П2) — из ТЗ
    donors: Tuple[str, ...] = field(
        default_factory=lambda: (
            "Соня Блейд", "СоняБлейд", "Джакс", "Кэсси Кейдж", "КэссиКейдж",
            "Ди'Вора", "ДиВора", "Ди Вора",
            # Английские варианты
            "Sonya Blade", "Sonya", "Jax", "Cassie Cage", "Cassie",
            "D'Vorah", "Dvorah",
        )
    )

    # Мертвые минуты (локальное время HH:MM). Формат либо "HH:MM"
    # (точное совпадение), либо "HH:MM-HH:MM" (полуинтервал включительно).
    dead_minutes: Tuple[str, ...] = field(
        default_factory=lambda: (
            "03:00-04:30",
            "12:10",
            "12:40",
            "13:15",
            "23:50-23:59",
        )
    )

    # Длина "Волны Бритья": если последние N матчей прошли без добиваний —
    # блокируем вход до первого Fatality.
    razor_wave_len: int = 3

    # Шаг временных коридоров: 288 коридоров по 5 минут.
    corridor_minutes: int = 5


settings = Settings()
