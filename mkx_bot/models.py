"""
Датаклассы, описывающие доменную модель: матч и раунд.
Оптимизированы для частичного заполнения (MessageEdited).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Match:
    match_no: str
    line: Optional[str] = None
    match_time: Optional[str] = None       # ISO, локальная TZ
    match_ts: Optional[int] = None         # unix epoch

    p1_name_ru: Optional[str] = None
    p2_name_ru: Optional[str] = None
    p1_name_en: Optional[str] = None
    p2_name_en: Optional[str] = None

    p1m: Optional[float] = None
    p2m: Optional[float] = None
    p1_round: Optional[float] = None
    p2_round: Optional[float] = None

    fbr_fatality: Optional[float] = None
    fbr_brutality: Optional[float] = None
    fbr_none: Optional[float] = None

    message_id: Optional[int] = None
    raw_message: Optional[str] = None

    finished: int = 0


@dataclass
class Round:
    round_no: int
    winner: str          # 'P1' | 'P2'
    finisher: str        # 'F' | 'B' | 'R'
    duration_sec: int
    tail: str = ""       # TMM/TM/TB — сохраняем как есть


@dataclass
class ParsedMessage:
    """Результат парсинга одного сообщения канала."""
    match: Match
    rounds: list = field(default_factory=list)  # list[Round]
    match_finished: bool = False                # встречен тег #T<N>
