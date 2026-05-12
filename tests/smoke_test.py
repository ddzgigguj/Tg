"""
Smoke-тест, который гоняет парсер, БД, аналайзер и баланс:

  1. Парсит пример матча из ТЗ §5 (Хищник-Горо, Райдэн-Соня).
  2. Заполняет БД синтетической историей, чтобы:
       - коридор 22:05 имел P(F) высокую именно в 4..6 раундах;
       - Райдэн (П1, слева) и Соня Блейд (П2, справа) имели высокую
         фатовость по 50-матчной выборке в бакете кф 4-4.99.
  3. Проверяет, что аналайзер отдаёт enter=True и target_range='4-6'
     для матча Райдэн/Соня с кф на Fatality 4.01.
  4. Проверяет, что матч Хищник-Горо остаётся заблокированным (не
     Палач/не Донор).
  5. Проверяет «Золотой догон»: Fatality во 2-м раунде диапазона 1-3
     даёт WIN на stage 2 и положительный Δ.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mkx_bot.analyzer import Analyzer
from mkx_bot.balance import BalanceManager
from mkx_bot.config import settings
from mkx_bot.db_manager import DBManager
from mkx_bot.models import Match, Round
from mkx_bot.parser import parse_message


SAMPLE_1 = """22:05 12-05-2026 #N230 #L2
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
3. P1--B--35  TB
4. P2--R--28  TM
5. P2--R--26  TM
6. P1--F--31  TM
7. P1--F--20  TMM
8. P1--F--22  TMM
   #T8
"""

SAMPLE_2 = """22:00 12-05-2026 #N229 #L1
#РайдэнСоняБлейд
#Райдэн #СоняБлейд
Raiden - Sonya Blade
   P1m|P2m - 1.53|2.58
P1/P2 - 1.755/2.17
FBR - 4.01 | 6.02 | 1.456
#t4v9     atv : 25.17
TimeStat(Больше-Меньше:O-U)
19.5 (1.245 - 3.93)   #m19
23.5 (1.91 - 1.99)   #s23
32.5 (3.84 - 1.255)   #b32
FYes -4.01    FNo -1.245
"""


def _seed_history(db: DBManager) -> None:
    """50 матчей в коридоре 22:05 (265): из них в 40 было F в 4..6 раунде.
    Те же матчи имеют Райдэна слева и Соню справа, кф Fatality в
    бакете 4-4.99 → фатовость обоих ≥ 80%."""
    base_ts = int(datetime(2026, 5, 1, 22, 5).timestamp())
    for i in range(50):
        # Слегка смещаем время, чтобы попасть в 22:05 (минута идентична).
        dt = datetime(2026, 5, 1, 22, 5) - timedelta(days=i)
        mn = f"N{100 + i}"
        m = Match(
            match_no=mn,
            line="1",
            match_time=dt.isoformat(),
            match_ts=int(dt.timestamp()),
            p1_name_ru="Райдэн",
            p2_name_ru="СоняБлейд",
            p1_name_en="Raiden",
            p2_name_en="Sonya Blade",
            p1m=1.53, p2m=2.58,
            p1_round=1.755, p2_round=2.17,
            fbr_fatality=4.05,   # бакет 4-4.99
            fbr_brutality=6.02,  # → для кросс-фильтра
            fbr_none=1.456,
            finished=1,
        )
        db.upsert_match(m)
        # В 40 матчах из 50 — Fatality в 4..6 раунде; в остальных — R.
        if i < 40:
            rounds = [
                Round(1, "P1", "R", 25),
                Round(2, "P2", "R", 28),
                Round(3, "P1", "R", 30),
                Round(4, "P1", "F", 22),
            ]
        else:
            rounds = [
                Round(1, "P1", "R", 25),
                Round(2, "P2", "R", 28),
                Round(3, "P1", "R", 30),
                Round(4, "P1", "R", 31),
                Round(5, "P2", "R", 27),
                Round(6, "P1", "R", 29),
            ]
        db.upsert_rounds(mn, rounds)
        db.mark_match_finished(mn)


def run() -> None:
    # 1. Парсер
    p1 = parse_message(SAMPLE_1, message_id=1)
    p2 = parse_message(SAMPLE_2, message_id=2)
    assert p1 and p2, "parser failed"
    assert p1.match.match_no == "N230"
    assert p2.match.match_no == "N229"
    assert p2.match.p1_name_ru == "Райдэн", p2.match.p1_name_ru
    assert p2.match.p2_name_ru == "СоняБлейд", p2.match.p2_name_ru
    assert p2.match.p1_name_en == "Raiden"
    assert p2.match.p2_name_en == "Sonya Blade"
    assert p1.rounds and p1.rounds[0].winner == "P1" and p1.rounds[0].finisher == "B"
    assert p1.match_finished is True
    assert p2.match_finished is False
    print("[1] parser OK")

    # 2. БД + история
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        _seed_history(db)

        # 3. Корректность корзинной статистики
        n13, pF13, _, _ = db.corridor_stats(265, "1-3")
        n46, pF46, _, _ = db.corridor_stats(265, "4-6")
        assert n13 == 50 and n46 == 50, (n13, n46)
        assert pF13 == 0.0, pF13
        assert pF46 == 40 / 50, pF46
        print(f"[2] corridor 265 -> 1-3 P(F)={pF13:.2f}, 4-6 P(F)={pF46:.2f}  (samples {n13}/{n46})")

        # 4. Фатовость
        fvP1 = db.character_fatovost("Райдэн", "P1", "4-6", "4-4.99")
        fvP2 = db.character_fatovost("СоняБлейд", "P2", "4-6", "4-4.99")
        print(f"    P1 fatovost windows: {fvP1}")
        print(f"    P2 fatovost windows: {fvP2}")
        assert fvP1[50][1] == 40 / 50
        assert fvP2[50][1] == 40 / 50

        # 5. Аналайзер на Райдэн-Соня с коридором 22:05 (idx=264 для 22:00)
        # Важно: match_time у примера — 22:00 (N229). Изменим время, чтобы
        # совпало с «заполненным» коридором 22:05 из истории.
        m = p2.match
        m.match_time = "2026-05-13T22:05:00"
        m.match_ts = int(datetime(2026, 5, 13, 22, 5).timestamp())
        # Баланс и analyzer
        bal = BalanceManager(db, 1000, 100, 220, 480)
        az = Analyzer(db, settings)
        res = az.evaluate_new_match(m)
        print(f"[3] analyzer (Raiden vs Sonya, 22:05): enter={res.enter}, "
              f"target_range={res.target_range!r}")
        print(f"    corridor={res.corridor}, corridor_prob={res.corridor_prob}")
        print(f"    reasons: {list(res.reasons.keys())}")
        print(f"    blockers: {res.blockers}")
        assert res.enter is True, res.blockers
        assert res.target_range == "4-6"

        # 6. Predator vs Goro должен оставаться заблокированным
        res1 = az.evaluate_new_match(p1.match)
        print(f"[4] analyzer (Predator vs Goro): enter={res1.enter}, "
              f"blockers={list(res1.blockers.keys())}")
        assert res1.enter is False
        assert "char_p1" in res1.blockers and "char_p2" in res1.blockers

        # 7. «Золотой догон» — проверка balance.settle_dogon
        before = bal.current()
        result, delta, win_round = bal.settle_dogon(
            rounds_in_range=[(1, "R"), (2, "F")],  # WIN на stage 2
            fat_coefficient=4.0,
            match_no="TEST",
        )
        after = bal.current()
        print(f"[5] dogon WIN stage2: result={result}, Δ={delta:+.2f}, "
              f"win_round={win_round}, balance: {before} -> {after}")
        assert result == "WIN"
        assert win_round == 2
        # Потеряли stage1 (100), выиграли stage2 220 * (4-1) = 660. Чистый Δ=+560.
        assert delta == 560.0, delta

        # 8. Догон полностью проигран
        result2, delta2, _ = bal.settle_dogon(
            rounds_in_range=[(1, "R"), (2, "R"), (3, "B")],
            fat_coefficient=4.0,
            match_no="TEST2",
        )
        print(f"[6] dogon LOSS full: result={result2}, Δ={delta2:+.2f}")
        assert result2 == "LOSS"
        # После WIN на stage2 баланс стал 1560, ставки масштабируются на 1.56:
        # LOSS = -(100+220+480) * 1.56 = -1248.0. Это проверка масштабирования.
        assert abs(delta2 - (-(100 + 220 + 480) * 1.56)) < 0.01, delta2

        # 9. Razor Wave: последние 3 матча без F/B -> блок. Зальём три
        # сухих и один проверочный, совершенно чистый матч, должен быть
        # заблокирован razor_wave.
        for i, mn in enumerate(("DRY1", "DRY2", "DRY3")):
            dt = datetime(2026, 5, 14, 10, 0) + timedelta(minutes=i)
            db.upsert_match(Match(
                match_no=mn, line="1",
                match_time=dt.isoformat(), match_ts=int(dt.timestamp()),
                p1_name_ru="Райдэн", p2_name_ru="СоняБлейд",
                p1_name_en="Raiden", p2_name_en="Sonya Blade",
                p1m=1.53, p2m=2.58, p1_round=1.755, p2_round=2.17,
                fbr_fatality=4.05, fbr_brutality=6.02, fbr_none=1.456,
                finished=1,
            ))
            db.upsert_rounds(mn, [Round(1, "P1", "R", 25), Round(2, "P2", "R", 26)])
            db.mark_match_finished(mn)
        # Теперь DRY1..3 — самые свежие. razor_wave должен сработать.
        # (Мы специально оставили старую «хорошую» историю ДО них.)
        res2 = az.evaluate_new_match(m)
        print(f"[7] after 3 dry matches: enter={res2.enter}, "
              f"blockers={list(res2.blockers.keys())}")
        assert "razor_wave" in res2.blockers
        print("\nAll smoke checks passed.")
    finally:
        os.unlink(dbpath)


def _run_regression_tests() -> None:
    """Дополнительные регрессии после bug-sweep."""
    from mkx_bot.parser import parse_message
    from mkx_bot.db_manager import DBManager
    from mkx_bot.balance import BalanceManager
    from mkx_bot.analyzer import Analyzer
    from mkx_bot.config import settings, _parse_signal_chat
    from mkx_bot.models import Match, Round

    # R1. MessageEdited не должен сбросить finished=1 обратно в 0.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        m = Match(
            match_no="R1", line="1",
            match_time="2026-05-12T10:00:00", match_ts=1,
            p1_name_ru="Райдэн", p2_name_ru="СоняБлейд",
            p1_name_en="Raiden", p2_name_en="Sonya Blade",
            p1m=1.5, p2m=2.5, p1_round=1.7, p2_round=2.1,
            fbr_fatality=3.0, fbr_brutality=5.0, fbr_none=1.5,
            finished=1,
        )
        db.upsert_match(m)                              # finished=1
        m.finished = 0                                  # как если бы пришёл edit
        db.upsert_match(m)
        with db._conn() as c:
            row = c.execute(
                "SELECT finished FROM matches WHERE match_no='R1'"
            ).fetchone()
        assert int(row["finished"]) == 1, (
            f"finished должен остаться 1 после MessageEdited, "
            f"получили {row['finished']}"
        )
        print("[R1] MessageEdited keeps finished=1 OK")
    finally:
        os.unlink(dbpath)

    # R2. Пустой целевой диапазон → VOID, никакой записи в баланс.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        bal = BalanceManager(db, 1000, 100, 220, 480)
        result, delta, wr = bal.settle_dogon(
            rounds_in_range=[], fat_coefficient=4.0, match_no="E1"
        )
        assert result == "VOID" and delta == 0.0 and wr is None
        assert bal.current() == 1000.0  # баланс не изменился
        print("[R2] empty target range -> VOID, balance untouched OK")
    finally:
        os.unlink(dbpath)

    # R3. fat_coefficient <= 1 → VOID, не считаем как WIN.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        bal = BalanceManager(db, 1000, 100, 220, 480)
        result, delta, _ = bal.settle_dogon(
            rounds_in_range=[(1, "F")], fat_coefficient=0.8, match_no="E2"
        )
        assert result == "VOID" and delta == 0.0
        print("[R3] fat_coef<=1 -> VOID OK")
    finally:
        os.unlink(dbpath)

    # R4. Отрицательный баланс не должен давать отрицательные ставки.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        bal = BalanceManager(db, 1000, 100, 220, 480)
        bal.apply_delta(-1500, "test big loss")  # банк -500
        bets = bal.scaled_bets()
        assert bets.r1 == 0 and bets.r2 == 0 and bets.r3 == 0, bets
        print("[R4] negative balance clamps bets to 0 OK")
    finally:
        os.unlink(dbpath)

    # R5. Кф вне [2,5) должен давать блокер coef_bucket.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        az = Analyzer(db, settings)
        m = Match(
            match_no="E5", line="1",
            match_time="2026-05-12T11:00:00", match_ts=1,
            p1_name_ru="Райдэн", p2_name_ru="СоняБлейд",
            p1_name_en="Raiden", p2_name_en="Sonya Blade",
            p1m=1.5, p2m=2.5, p1_round=1.7, p2_round=2.1,
            fbr_fatality=6.5,                             # вне 2..4.99
            fbr_brutality=5.0, fbr_none=1.5,
        )
        res = az.evaluate_new_match(m)
        assert res.enter is False
        assert "coef_bucket" in res.blockers, res.blockers
        print(f"[R5] out-of-range coef -> blocker={res.blockers['coef_bucket']!r}")
    finally:
        os.unlink(dbpath)

    # R6. Битое время матча → блокер time, вход заблокирован.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        dbpath = f.name
    try:
        db = DBManager(dbpath)
        az = Analyzer(db, settings)
        m = Match(
            match_no="E6", line="1",
            match_time=None, match_ts=None,     # нет времени
            p1_name_ru="Райдэн", p2_name_ru="СоняБлейд",
            p1_name_en="Raiden", p2_name_en="Sonya Blade",
            p1m=1.5, p2m=2.5, p1_round=1.7, p2_round=2.1,
            fbr_fatality=3.0, fbr_brutality=5.0, fbr_none=1.5,
        )
        res = az.evaluate_new_match(m)
        assert res.enter is False and "time" in res.blockers
        print("[R6] missing match_time -> blocked (no server TZ fallback) OK")
    finally:
        os.unlink(dbpath)

    # R7. _parse_signal_chat обрабатывает разные форматы правильно.
    assert _parse_signal_chat("-1001234567890") == -1001234567890
    assert _parse_signal_chat("@my_group") == "@my_group"
    assert _parse_signal_chat("my_group") == "@my_group"
    assert _parse_signal_chat("https://t.me/my_group") == "@my_group"
    # инвайт-ссылки Bot API не резолвит — должны быть пусты
    assert _parse_signal_chat("https://t.me/+abcdEFGhij") == ""
    assert _parse_signal_chat("https://t.me/joinchat/xxxx") == ""
    assert _parse_signal_chat("") == ""
    assert _parse_signal_chat("  ") == ""
    print("[R7] SIGNAL_CHAT parsing covers invite links, urls, @names, ids OK")


if __name__ == "__main__":
    run()
    print()
    print("=" * 60)
    print("Regression suite (post bug-sweep):")
    print("=" * 60)
    _run_regression_tests()
    print("\nAll regression checks passed.")
