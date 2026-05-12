"""
Работа с SQLite. Схема и запросы, которые непосредственно питают логику
уроков Cybernagual:

  * Таблица коридоров (`corridor_stats`) — считается из matches+rounds
    и разбивается по трём целевым диапазонам раундов: 1..3, 4..6 и
    весь матч (docs/lessons/corridors.md).

  * Фатовость персонажа по 4 окнам (последние 50/30/10/5 матчей) и по
    трём диапазонам коэффициента на Fatality (2–2.99, 3–3.99, 4–4.99).
    docs/lessons/Fatality.md, раздел «Выборка».

  * «Срезы» — выборки схожих матчей по набору фильтров
    (docs/lessons/mk-lesson-two.md).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .models import Match, Round


SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_no        TEXT PRIMARY KEY,
    line            TEXT,
    match_time      TEXT,
    match_ts        INTEGER,
    p1_name_ru      TEXT,
    p2_name_ru      TEXT,
    p1_name_en      TEXT,
    p2_name_en      TEXT,
    p1m             REAL,
    p2m             REAL,
    p1_round        REAL,
    p2_round        REAL,
    fbr_fatality    REAL,
    fbr_brutality   REAL,
    fbr_none        REAL,
    message_id      INTEGER,
    raw_message     TEXT,
    has_fatality_1_3  INTEGER DEFAULT 0,
    has_fatality_4_6  INTEGER DEFAULT 0,
    has_fatality_any  INTEGER DEFAULT 0,
    has_brutality_any INTEGER DEFAULT 0,
    first_round_dur   INTEGER,
    finished        INTEGER DEFAULT 0,
    created_at      INTEGER,
    updated_at      INTEGER
);

CREATE TABLE IF NOT EXISTS rounds (
    match_no        TEXT,
    round_no        INTEGER,
    winner          TEXT,
    finisher        TEXT,
    duration_sec    INTEGER,
    tail            TEXT,
    PRIMARY KEY (match_no, round_no)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_no        TEXT,
    kind            TEXT,
    target_range    TEXT,          -- '1-3' | '4-6'
    reason          TEXT,
    sent_at         INTEGER,
    result          TEXT,          -- 'WIN' | 'LOSS' | NULL
    win_round       INTEGER,
    profit          REAL
);

CREATE TABLE IF NOT EXISTS balance_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER,
    balance         REAL,
    delta           REAL,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_matches_ts ON matches(match_ts);
CREATE INDEX IF NOT EXISTS idx_signals_sent ON signals(sent_at);
CREATE INDEX IF NOT EXISTS idx_rounds_match ON rounds(match_no);
"""


# -------------------------------------------------------------- utilities


def _coef_bucket(f_coef: Optional[float]) -> Optional[str]:
    """Ведро коэффициента на Fatality. Уроки считают фатовость раздельно
    для трёх диапазонов: 2.00-2.99, 3.00-3.99, 4.00-4.99."""
    if f_coef is None:
        return None
    if 2.0 <= f_coef < 3.0:
        return "2-2.99"
    if 3.0 <= f_coef < 4.0:
        return "3-3.99"
    if 4.0 <= f_coef < 5.0:
        return "4-4.99"
    return None


def corridor_index(dt: datetime, step_min: int = 5) -> int:
    """Номер пятиминутного коридора в сутках: 0..287."""
    return (dt.hour * 60 + dt.minute) // step_min


# -------------------------------------------------------------- manager


class DBManager:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- writes

    def upsert_match(self, m: Match) -> None:
        now = int(time.time())
        data = asdict(m)
        with self._conn() as c:
            cur = c.execute("SELECT 1 FROM matches WHERE match_no = ?", (m.match_no,))
            exists = cur.fetchone() is not None
            if exists:
                c.execute(
                    """UPDATE matches SET
                          line = COALESCE(?, line),
                          match_time = COALESCE(?, match_time),
                          match_ts = COALESCE(?, match_ts),
                          p1_name_ru = COALESCE(?, p1_name_ru),
                          p2_name_ru = COALESCE(?, p2_name_ru),
                          p1_name_en = COALESCE(?, p1_name_en),
                          p2_name_en = COALESCE(?, p2_name_en),
                          p1m = COALESCE(?, p1m),
                          p2m = COALESCE(?, p2m),
                          p1_round = COALESCE(?, p1_round),
                          p2_round = COALESCE(?, p2_round),
                          fbr_fatality = COALESCE(?, fbr_fatality),
                          fbr_brutality = COALESCE(?, fbr_brutality),
                          fbr_none = COALESCE(?, fbr_none),
                          message_id = COALESCE(?, message_id),
                          raw_message = COALESCE(?, raw_message),
                          finished = COALESCE(?, finished),
                          updated_at = ?
                       WHERE match_no = ?""",
                    (
                        data["line"], data["match_time"], data["match_ts"],
                        data["p1_name_ru"], data["p2_name_ru"],
                        data["p1_name_en"], data["p2_name_en"],
                        data["p1m"], data["p2m"],
                        data["p1_round"], data["p2_round"],
                        data["fbr_fatality"], data["fbr_brutality"], data["fbr_none"],
                        data["message_id"], data["raw_message"],
                        data.get("finished", 0), now, m.match_no,
                    ),
                )
            else:
                c.execute(
                    """INSERT INTO matches
                       (match_no, line, match_time, match_ts,
                        p1_name_ru, p2_name_ru, p1_name_en, p2_name_en,
                        p1m, p2m, p1_round, p2_round,
                        fbr_fatality, fbr_brutality, fbr_none,
                        message_id, raw_message, finished, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["match_no"], data["line"], data["match_time"], data["match_ts"],
                        data["p1_name_ru"], data["p2_name_ru"],
                        data["p1_name_en"], data["p2_name_en"],
                        data["p1m"], data["p2m"],
                        data["p1_round"], data["p2_round"],
                        data["fbr_fatality"], data["fbr_brutality"], data["fbr_none"],
                        data["message_id"], data["raw_message"],
                        data.get("finished", 0), now, now,
                    ),
                )

    def upsert_rounds(self, match_no: str, rounds: Iterable[Round]) -> None:
        first_dur = None
        with self._conn() as c:
            for r in rounds:
                if r.round_no == 1:
                    first_dur = r.duration_sec
                c.execute(
                    """INSERT INTO rounds(match_no, round_no, winner, finisher,
                                          duration_sec, tail)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(match_no, round_no) DO UPDATE SET
                           winner = excluded.winner,
                           finisher = excluded.finisher,
                           duration_sec = excluded.duration_sec,
                           tail = excluded.tail""",
                    (match_no, r.round_no, r.winner, r.finisher,
                     r.duration_sec, r.tail),
                )
            # Обновим агрегаты матча.
            cur = c.execute(
                """SELECT MAX(CASE WHEN round_no BETWEEN 1 AND 3 AND finisher='F' THEN 1 ELSE 0 END) AS f13,
                          MAX(CASE WHEN round_no BETWEEN 4 AND 6 AND finisher='F' THEN 1 ELSE 0 END) AS f46,
                          MAX(CASE WHEN finisher='F' THEN 1 ELSE 0 END) AS fany,
                          MAX(CASE WHEN finisher='B' THEN 1 ELSE 0 END) AS bany
                     FROM rounds WHERE match_no = ?""",
                (match_no,),
            )
            row = cur.fetchone()
            c.execute(
                """UPDATE matches SET
                       has_fatality_1_3 = ?,
                       has_fatality_4_6 = ?,
                       has_fatality_any = ?,
                       has_brutality_any = ?,
                       first_round_dur = COALESCE(?, first_round_dur)
                   WHERE match_no = ?""",
                (int(row["f13"] or 0), int(row["f46"] or 0),
                 int(row["fany"] or 0), int(row["bany"] or 0),
                 first_dur, match_no),
            )

    def mark_match_finished(self, match_no: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE matches SET finished = 1 WHERE match_no = ?",
                (match_no,),
            )

    # --------------------------------------------------------------- signals

    def save_signal(
        self,
        match_no: str,
        kind: str,
        target_range: str,
        reason: dict,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO signals(match_no, kind, target_range, reason, sent_at)
                   VALUES (?,?,?,?,?)""",
                (match_no, kind, target_range,
                 json.dumps(reason, ensure_ascii=False),
                 int(time.time())),
            )
            return int(cur.lastrowid)

    def close_signal(
        self, signal_id: int, result: str, profit: float,
        win_round: Optional[int] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE signals SET result=?, profit=?, win_round=? WHERE id=?",
                (result, profit, win_round, signal_id),
            )

    def signal_by_match(self, match_no: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM signals WHERE match_no=? ORDER BY id DESC LIMIT 1",
                (match_no,),
            )
            return cur.fetchone()

    # --------------------------------------------------------------- balance

    def append_balance(self, balance: float, delta: float, reason: str) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO balance_history(ts, balance, delta, reason)
                   VALUES (?,?,?,?)""",
                (int(time.time()), balance, delta, reason),
            )

    def get_current_balance(self, default: float) -> float:
        with self._conn() as c:
            cur = c.execute(
                "SELECT balance FROM balance_history ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            return float(row["balance"]) if row else default

    # --------------------------------------------------------------- history

    def recent_finished(self, limit: int = 50) -> List[sqlite3.Row]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT * FROM matches WHERE finished=1
                   ORDER BY match_ts DESC LIMIT ?""",
                (limit,),
            )
            return list(cur.fetchall())

    def last_n_all_dry(self, n: int) -> Optional[bool]:
        """True — в последних n завершённых матчах НИ разу не было F/B.
        False — хотя бы в одном было добивание.
        None — истории не хватает."""
        rows = self.recent_finished(limit=n)
        if len(rows) < n:
            return None
        return all(
            int(r["has_fatality_any"]) == 0 and int(r["has_brutality_any"]) == 0
            for r in rows
        )

    # ------------------------------------------------------- corridor stats

    def corridor_stats(
        self,
        corridor: int,
        target_range: str,
    ) -> Tuple[int, float, float, float]:
        """Возвращает (n_samples, p_fat, p_brut, p_bd) для коридора и
        целевого диапазона раундов ('1-3' | '4-6' | 'match')."""
        if target_range == "1-3":
            sql = """
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 1 AND 3
                            AND finisher='F') THEN 1 ELSE 0 END) AS nf,
                    SUM(CASE WHEN EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 1 AND 3
                            AND finisher='B') THEN 1 ELSE 0 END) AS nb,
                    SUM(CASE WHEN NOT EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 1 AND 3
                            AND finisher IN ('F','B')) THEN 1 ELSE 0 END) AS nr
            """
        elif target_range == "4-6":
            sql = """
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 4 AND 6
                            AND finisher='F') THEN 1 ELSE 0 END) AS nf,
                    SUM(CASE WHEN EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 4 AND 6
                            AND finisher='B') THEN 1 ELSE 0 END) AS nb,
                    SUM(CASE WHEN NOT EXISTS(SELECT 1 FROM rounds
                          WHERE match_no=m.match_no AND round_no BETWEEN 4 AND 6
                            AND finisher IN ('F','B')) THEN 1 ELSE 0 END) AS nr
            """
        else:  # match (1..6)
            sql = """
                SELECT
                    COUNT(*) AS n,
                    SUM(has_fatality_any) AS nf,
                    SUM(has_brutality_any) AS nb,
                    SUM(CASE WHEN has_fatality_any=0 AND has_brutality_any=0
                             THEN 1 ELSE 0 END) AS nr
            """

        sql += """
            FROM matches m
            WHERE finished=1
              AND match_time IS NOT NULL
              AND CAST(substr(match_time, 12, 2) AS INTEGER)*12
                 + CAST(substr(match_time, 15, 2) AS INTEGER)/5 = ?
        """
        with self._conn() as c:
            row = c.execute(sql, (corridor,)).fetchone()
            n = int(row["n"] or 0)
            if n == 0:
                return 0, 0.0, 0.0, 0.0
            return (
                n,
                (row["nf"] or 0) / n,
                (row["nb"] or 0) / n,
                (row["nr"] or 0) / n,
            )

    # --------------------------------------------------- character fatovost

    def character_fatovost(
        self,
        character: str,
        side: str,             # 'P1' | 'P2'
        target_range: str,     # '1-3' | '4-6'
        coef_bucket: str,      # '2-2.99' | '3-3.99' | '4-4.99'
        windows: Tuple[int, ...] = (50, 30, 10, 5),
    ) -> Dict[int, Tuple[int, float]]:
        """Вероятность захода Fatality у персонажа на конкретной стороне,
        в целевом диапазоне раундов и только по матчам c соответствующим
        бакетом кф на Fatality. Возвращает {window: (n_samples, p_fat)}."""
        bucket_low, bucket_hi = {
            "2-2.99": (2.0, 3.0),
            "3-3.99": (3.0, 4.0),
            "4-4.99": (4.0, 5.0),
        }.get(coef_bucket, (0.0, 99.0))

        if target_range == "1-3":
            target_clause = ("has_fatality_1_3", 1)
        elif target_range == "4-6":
            target_clause = ("has_fatality_4_6", 1)
        else:
            target_clause = ("has_fatality_any", 1)

        # Выбираем колонку имени по стороне: приоритет русскому имени.
        if side == "P1":
            name_col_clauses = ("p1_name_ru", "p1_name_en")
        else:
            name_col_clauses = ("p2_name_ru", "p2_name_en")

        sql = f"""
            SELECT match_ts, {target_clause[0]} AS hit
              FROM matches
             WHERE finished=1
               AND fbr_fatality >= ? AND fbr_fatality < ?
               AND ({name_col_clauses[0]} = ? OR {name_col_clauses[1]} = ?)
             ORDER BY match_ts DESC
             LIMIT ?
        """
        max_win = max(windows)
        with self._conn() as c:
            rows = c.execute(
                sql, (bucket_low, bucket_hi, character, character, max_win)
            ).fetchall()
        result: Dict[int, Tuple[int, float]] = {}
        for w in windows:
            sub = rows[:w]
            if not sub:
                result[w] = (0, 0.0)
            else:
                hits = sum(int(r["hit"]) for r in sub)
                result[w] = (len(sub), hits / len(sub))
        return result

    # ---------------------------------------------- first-round duration slice

    def matches_near_first_round_duration(
        self, duration: int, tolerance: int = 3, limit: int = 200,
    ) -> List[sqlite3.Row]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT * FROM matches
                   WHERE finished=1
                     AND first_round_dur IS NOT NULL
                     AND ABS(first_round_dur - ?) <= ?
                   ORDER BY match_ts DESC LIMIT ?""",
                (duration, tolerance, limit),
            )
            return list(cur.fetchall())

    # ----------------------------------------------------------- /stats cmds

    def stats_range(self, since_ts: int) -> dict:
        with self._conn() as c:
            cur = c.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(profit),0) AS profit
                     FROM signals WHERE sent_at >= ?""",
                (since_ts,),
            )
            row = cur.fetchone()
            return {
                "total": int(row["total"] or 0),
                "wins": int(row["wins"] or 0),
                "losses": int(row["losses"] or 0),
                "profit": float(row["profit"] or 0.0),
            }

    def stats_day(self) -> dict:
        return self.stats_range(int(time.time()) - 24 * 3600)

    def stats_week(self) -> dict:
        return self.stats_range(int(time.time()) - 7 * 24 * 3600)

    def stats_month(self) -> dict:
        return self.stats_range(int(time.time()) - 30 * 24 * 3600)
