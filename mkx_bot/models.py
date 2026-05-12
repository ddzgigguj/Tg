"""
Датаклассы, описывающие доменную модель MKX: матч и раунд.
Оптимизированы для частичного заполнения (MessageEdited — поля могут
приходить постепенно).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Match:
    match_no: str
    line: Optional[str] = None
    match_time: Optional[str] = None       # ISO-строка локального времени
    match_ts: Optional[int] = None         # unix epoch того же локального времени

    p1_name_ru: Optional[str] = None
    p2_name_ru: Optional[str] = None
    p1_name_en: Optional[str] = None
    p2_name_en: Optional[str] = None

    p1m: Optional[float] = None            # коэф. на матч P1
    p2m: Optional[float] = None            # коэф. на матч P2
    p1_round: Optional[float] = None       # коэф. на раунд P1
    p2_round: Optional[float] = None       # коэф. на раунд P2

    fbr_fatality: Optional[float] = None
    fbr_brutality: Optional[float] = None
    fbr_none: Optional[float] = None       # без добивания (БД)

    message_id: Optional[int] = None
    raw_message: Optional[str] = None

    finished: int = 0


@dataclass
class Round:
    round_no: int
    winner: str          # 'P1' | 'P2'
    finisher: str        # 'F' (Fatality) | 'B' (Brutality) | 'R' (Regular/БД)
    duration_sec: int
    tail: str = ""       # 'TMM' / 'TM' / 'TB' — разметка времени; храним сырой


@dataclass
class ParsedMessage:
    """Результат парсинга одного сообщения канала."""
    match: Match
    rounds: List[Round] = field(default_factory=list)
    match_finished: bool = False           # в тексте встречен тег #T<N>
