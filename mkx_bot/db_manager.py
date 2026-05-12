"""
Работа с SQLite: схема, апсерты матчей и раундов, расчет серий
«Волны Бритья» и статистика для команды /stats.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, List, Optional

from .models import Match, Round


SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_no        TEXT PRIMARY KEY,
    line            TEXT,
    match_time      TEXT,                  -- ISO local datetime из сообщения
    match_ts        INTEGER,               -- UNIX epoch, для фильтрации
    p1_name_ru      TEXT,
    p2_name_ru      TEXT,
    p1_name_en      TEXT,
    p2_name_en      TEXT,
    p1m             REAL,                  -- коэф. на матч P1
    p2m             REAL,                  -- коэф. на матч P2
    p1_round        REAL,                  -- коэф. на раунд P1
    p2_round        REAL,                  -- коэф. на раунд P2
    fbr_fatality    REAL,
    fbr_brutality   REAL,
    fbr_none        REAL,
    message_id      INTEGER,
    raw_message     TEXT,                  -- оригинальный текст для отладки
    has_finisher    INTEGER DEFAULT 0,     -- 1 если в матче был F или B
    finished        INTEGER DEFAULT 0,     -- 1 если матч помечен завершенным (#TN)
    created_at      INTEGER,
    updated_at      INTEGER
);

CREATE TABLE IF NOT EXISTS rounds (
    match_no        TEXT,
    round_no        INTEGER,
    winner          TEXT,                  -- 'P1' | 'P2'
    finisher        TEXT,                  -- 'F' | 'B' | 'R'
    duration_sec    INTEGER,
    tail            TEXT,                  -- TMM/TM/TB... — оставляем сырым
    PRIMARY KEY (match_no, round_no)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_no        TEXT,
    kind            TEXT,                  -- 'FATALITY' | 'BRUTALITY' | ...
    reason          TEXT,                  -- JSON: какие фильтры подтвердились
    sent_at         INTEGER,
    result          TEXT,                  -- 'WIN' | 'LOSS' | NULL (пока ждем)
    profit          REAL                   -- итоговое изменение баланса
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
"""


class DBManager:
    def __init__(self, path: str):
        self.path = path
        self._init()

    # ------------------------------------------------------------------ infra
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

    # ------------------------------------------------------------------ write
    def upsert_match(self, m: Match) -> None:
        """Вставляет или обновляет матч. Повторный вызов при MessageEdited
        только обновит изменившиеся поля, не стирая уже известного."""
        now = int(time.time())
        data = asdict(m)
        data["updated_at"] = now
        data["created_at"] = now
        with self._conn() as c:
            cur = c.execute(
                "SELECT 1 FROM matches WHERE match_no = ?", (m.match_no,)
            )
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
                        data["p1m"], data["p2m"], data["p1_round"], data["p2_round"],
                        data["fbr_fatality"], data["fbr_brutality"], data["fbr_none"],
                        data["message_id"], data["raw_message"], data.get("finished", 0),
                        now, m.match_no,
                    ),
                )
            else:
                c.execute(
                    """INSERT INTO matches
                       (match_no, line, match_time, match_ts,
                        p1_name_ru, p2_name_ru, p1_name_en, p2_name_en,
                        p1m, p2m, p1_round, p2_round,
                        fbr_fatality, fbr_brutality, fbr_none,
                        message_id, raw_message, finished,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["match_no"], data["line"], data["match_time"], data["match_ts"],
                        data["p1_name_ru"], data["p2_name_ru"],
                        data["p1_name_en"], data["p2_name_en"],
                        data["p1m"], data["p2m"], data["p1_round"], data["p2_round"],
                        data["fbr_fatality"], data["fbr_brutality"], data["fbr_none"],
                        data["message_id"], data["raw_message"], data.get("finished", 0),
                        now, now,
                    ),
                )

    def upsert_rounds(self, match_no: str, rounds: Iterable[Round]) -> None:
        with self._conn() as c:
            has_finisher = False
            for r in rounds:
                if r.finisher in ("F", "B"):
                    has_finisher = True
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
            if has_finisher:
                c.execute(
                    "UPDATE matches SET has_finisher = 1 WHERE match_no = ?",
                    (match_no,),
                )

    def mark_match_finished(self, match_no: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE matches SET finished = 1 WHERE match_no = ?",
                (match_no,),
            )

    def save_signal(self, match_no: str, kind: str, reason: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO signals(match_no, kind, reason, sent_at)
                   VALUES (?,?,?,?)""",
                (match_no, kind, json.dumps(reason, ensure_ascii=False),
                 int(time.time())),
            )
            return int(cur.lastrowid)

    def close_signal(self, signal_id: int, result: str, profit: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE signals SET result = ?, profit = ? WHERE id = ?",
                (result, profit, signal_id),
            )

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

    # ------------------------------------------------------------------- read
    def recent_matches(self, limit: int = 5) -> List[sqlite3.Row]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT * FROM matches
                   WHERE finished = 1
                   ORDER BY match_ts DESC LIMIT ?""",
                (limit,),
            )
            return list(cur.fetchall())

    def last_n_had_finisher(self, n: int) -> Optional[bool]:
        """True/False если достаточно истории, иначе None.
        True = во ВСЕХ последних n матчах были добивания.
        False = хотя бы в одном не было добивания.
        """
        rows = self.recent_matches(limit=n)
        if len(rows) < n:
            return None
        return all(int(r["has_finisher"]) == 1 for r in rows)

    def last_n_all_dry(self, n: int) -> Optional[bool]:
        """True если в последних n матчах не было ни одного добивания."""
        rows = self.recent_matches(limit=n)
        if len(rows) < n:
            return None
        return all(int(r["has_finisher"]) == 0 for r in rows)

    def signal_by_match(self, match_no: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM signals WHERE match_no = ? ORDER BY id DESC LIMIT 1",
                (match_no,),
            )
            return cur.fetchone()

    # ----------------------------------------------------------- /stats
    def stats_range(self, since_ts: int) -> dict:
        with self._conn() as c:
            cur = c.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(profit),0) AS profit
                     FROM signals
                    WHERE sent_at >= ?""",
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
