"""
Парсинг сообщений канала @statamk10.

Оригинальный формат (пример из ТЗ, §5):

    22:05 12-05-2026 #N230 #L2
    #ХищникГоро
    #Хищник #Горо
    Predator - Goro
       P1m|P2m - 1.504|2.65
    P1/P2 - 1.74/2.19
    FBR - 3.6 | 3.45 | 1.955
    #t8v7     atv : 33.17
    TimeStat(Больше-Меньше:O-U)
    25.5 (1.24 - 3.95)   #m25
    33.5 (1.925 - 1.975)   #s33
    40.5 (4.23 - 1.216)   #b40
    FYes -3.6    FNo -1.31
    374393 377353
    5:3
    1. P1--B--22  TMM
    2. P2--R--26  TM
    ...
    8. P1--F--22  TMM
       #T8

Парсер извлекает:
  — время / дату / #N / #L
  — русские имена (#Хищник #Горо) и английские (Predator - Goro)
  — коэффициенты: P1m|P2m, P1/P2, FBR
  — раунды (`1. P1--B--22  TMM`) и признак завершения матча (#TN)

Повторные сообщения (MessageEdited) обрабатываются штатно: парсер возвращает
все поля, а в БД уже известные значения не затираются None'ами.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from .models import Match, ParsedMessage, Round


# --- Заголовок матча ---

RE_HEADER = re.compile(
    r"""^\s*
        (?P<time>\d{1,2}:\d{2})\s+
        (?P<date>\d{2}-\d{2}-\d{4})\s+
        \#N(?P<num>\d+)\s+
        \#L(?P<line>\d+)\s*$
    """,
    re.MULTILINE | re.VERBOSE,
)

# "#Хищник #Горо" — два хэштега-имени в одной строке
RE_SPLIT_TAGS = re.compile(
    r"^\s*#(?P<p1>[^\s#]+)\s+#(?P<p2>[^\s#]+)\s*$",
    re.MULTILINE,
)

# "Predator - Goro" — строка из двух английских имён
RE_EN_NAMES = re.compile(
    r"^\s*(?P<p1>[A-Za-z][A-Za-z .'\-]*?)\s+-\s+(?P<p2>[A-Za-z][A-Za-z .'\-]*?)\s*$",
    re.MULTILINE,
)

# Коэффициенты
RE_P1M_P2M = re.compile(
    r"P1m\s*\|\s*P2m\s*-\s*(?P<p1m>[\d.]+)\s*\|\s*(?P<p2m>[\d.]+)",
    re.IGNORECASE,
)
RE_P1_P2_ROUND = re.compile(
    r"(?<!m)P1\s*/\s*P2\s*-\s*(?P<p1>[\d.]+)\s*/\s*(?P<p2>[\d.]+)",
    re.IGNORECASE,
)
RE_FBR = re.compile(
    r"FBR\s*-\s*(?P<f>[\d.]+)\s*\|\s*(?P<b>[\d.]+)\s*\|\s*(?P<r>[\d.]+)",
    re.IGNORECASE,
)

# Раунд: "1. P1--B--22  TMM"
RE_ROUND = re.compile(
    r"^\s*(?P<n>\d+)\.\s*"
    r"(?P<winner>P[12])\s*--\s*(?P<fin>[FBR])\s*--\s*(?P<sec>\d+)"
    r"(?:\s+(?P<tail>\S+))?\s*$",
    re.MULTILINE,
)

# Завершение матча "#T8"
RE_MATCH_END = re.compile(r"#T(?P<n>\d+)\b")


def parse_message(text: str, message_id: Optional[int] = None) -> Optional[ParsedMessage]:
    """Возвращает ParsedMessage либо None, если сообщение не соответствует формату."""
    if not text:
        return None

    mh = RE_HEADER.search(text)
    if not mh:
        return None

    match_no = f"N{mh.group('num')}"
    line_no = mh.group("line")

    match_time = None
    match_ts = None
    try:
        dt = datetime.strptime(
            f"{mh.group('date')} {mh.group('time')}", "%d-%m-%Y %H:%M"
        )
        match_time = dt.isoformat()
        match_ts = int(dt.timestamp())
    except Exception:
        pass

    # Русские имена
    p1_ru = p2_ru = None
    for m in RE_SPLIT_TAGS.finditer(text):
        line = m.group(0)
        if "P1" in line or "P2" in line:
            continue
        p1_ru, p2_ru = m.group("p1"), m.group("p2")
        break

    # Английские имена — ищем строку, которая не содержит маркеров
    # коэффициентов и специальных двоеточий.
    p1_en = p2_en = None
    for m in RE_EN_NAMES.finditer(text):
        cand1 = m.group("p1").strip()
        cand2 = m.group("p2").strip()
        if cand1.lower() in ("p1m", "p1", "fyes", "fno", "timestat"):
            continue
        line = m.group(0)
        if any(k in line for k in ("P1m", "P1/P2", "FBR", "|", "/", ":")):
            continue
        p1_en, p2_en = cand1, cand2
        break

    p1m = p2m = None
    if (mm := RE_P1M_P2M.search(text)):
        p1m = float(mm.group("p1m"))
        p2m = float(mm.group("p2m"))

    p1r = p2r = None
    if (mm := RE_P1_P2_ROUND.search(text)):
        p1r = float(mm.group("p1"))
        p2r = float(mm.group("p2"))

    f = b = r = None
    if (mm := RE_FBR.search(text)):
        f = float(mm.group("f"))
        b = float(mm.group("b"))
        r = float(mm.group("r"))

    match = Match(
        match_no=match_no,
        line=line_no,
        match_time=match_time,
        match_ts=match_ts,
        p1_name_ru=p1_ru,
        p2_name_ru=p2_ru,
        p1_name_en=p1_en,
        p2_name_en=p2_en,
        p1m=p1m, p2m=p2m, p1_round=p1r, p2_round=p2r,
        fbr_fatality=f, fbr_brutality=b, fbr_none=r,
        message_id=message_id,
        raw_message=text,
    )

    rounds: List[Round] = []
    for rm in RE_ROUND.finditer(text):
        rounds.append(
            Round(
                round_no=int(rm.group("n")),
                winner=rm.group("winner"),
                finisher=rm.group("fin"),
                duration_sec=int(rm.group("sec")),
                tail=(rm.group("tail") or "").strip(),
            )
        )

    match_finished = bool(RE_MATCH_END.search(text))

    return ParsedMessage(match=match, rounds=rounds, match_finished=match_finished)
