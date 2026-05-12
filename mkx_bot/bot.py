"""
Связующий модуль:
  * Telethon-клиент слушает канал SOURCE_CHANNEL и ловит NewMessage /
    MessageEdited (результаты раундов дописываются постепенно).
  * Парсер преобразует текст в Match + Round.
  * Аналайзер решает, выдавать ли сигнал.
  * python-telegram-bot (Application) публикует сигнал в SIGNAL_CHAT
    и обслуживает команду /stats.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from telethon import TelegramClient, events

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from .analyzer import Analyzer, AnalysisResult
from .balance import BalanceManager
from .config import settings
from .db_manager import DBManager
from .parser import parse_message


log = logging.getLogger("mkx_bot")


# ----------------------------------------------------------------------- core

class MKXBot:
    def __init__(self) -> None:
        self.db = DBManager(settings.db_path)
        self.balance = BalanceManager(
            self.db,
            start_balance=settings.start_balance,
            base_r1=settings.bet_r1,
            base_r2=settings.bet_r2,
            base_r3=settings.bet_r3,
        )
        self.analyzer = Analyzer(self.db, settings)

        if not settings.telethon_api_id or not settings.telethon_api_hash:
            raise RuntimeError(
                "TELETHON_API_ID / TELETHON_API_HASH не заданы — "
                "бот не сможет читать канал."
            )
        self.userbot = TelegramClient(
            settings.telethon_session,
            settings.telethon_api_id,
            settings.telethon_api_hash,
        )

        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан.")
        self.app: Application = (
            ApplicationBuilder().token(settings.telegram_bot_token).build()
        )
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("balance", self._cmd_balance))
        self.app.add_handler(CommandHandler("start", self._cmd_start))

    # ---------------------------------------------------------------- wiring

    async def run(self) -> None:
        await self.userbot.start()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()  # type: ignore[union-attr]

        @self.userbot.on(events.NewMessage(chats=settings.source_channel))
        async def _on_new(event):
            await self._handle_message(event.message.id, event.raw_text or "")

        @self.userbot.on(events.MessageEdited(chats=settings.source_channel))
        async def _on_edit(event):
            # Результаты раундов дописываются через редактирование.
            await self._handle_message(event.message.id, event.raw_text or "")

        log.info("MKX bot started; listening %s", settings.source_channel)
        try:
            await self.userbot.run_until_disconnected()
        finally:
            await self.app.updater.stop()  # type: ignore[union-attr]
            await self.app.stop()
            await self.app.shutdown()

    # ----------------------------------------------------------------- logic

    async def _handle_message(self, message_id: int, text: str) -> None:
        parsed = parse_message(text, message_id=message_id)
        if parsed is None:
            return

        match = parsed.match
        self.db.upsert_match(match)

        if parsed.rounds:
            self.db.upsert_rounds(match.match_no, parsed.rounds)

        if parsed.match_finished:
            self.db.mark_match_finished(match.match_no)
            await self._settle_signal_if_any(match.match_no)
            return

        # Если сигнал для этого матча еще не выпускали — пробуем
        existing = self.db.signal_by_match(match.match_no)
        if existing is None:
            result = self.analyzer.evaluate_new_match(match)
            if result.enter:
                await self._send_signal(match.match_no, result)

    async def _send_signal(self, match_no: str, result: AnalysisResult) -> None:
        bets = self.balance.scaled_bets()
        text = (
            f"ВХОД в стратегию Fatality\n"
            f"Матч: {match_no}\n"
            f"Коридор: {result.corridor} (P(F)={result.prob:.2f})\n"
            f"Подтверждения:\n"
            + "\n".join(f"  • {k}: {v}" for k, v in result.reasons.items())
            + f"\nТекущий баланс: {self.balance.current():.2f}\n"
            f"Ставки: Р1={bets.r1}, Р2={bets.r2}, Р3={bets.r3}"
        )
        try:
            await self.app.bot.send_message(
                chat_id=settings.signal_chat, text=text
            )
        except Exception as e:
            log.exception("Failed to publish signal: %s", e)
        # Сохраняем в БД
        self.db.save_signal(
            match_no=match_no,
            kind=result.kind,
            reason={"reasons": result.reasons, "corridor": result.corridor,
                    "prob": result.prob},
        )

    async def _settle_signal_if_any(self, match_no: str) -> None:
        sig = self.db.signal_by_match(match_no)
        if sig is None or sig["result"] is not None:
            return

        # Поднимем раунды и посчитаем — было ли Fatality в 1-3 раундах
        with self.db._conn() as c:  # noqa: SLF001
            cur = c.execute(
                """SELECT round_no, finisher FROM rounds
                    WHERE match_no = ? AND round_no <= 3
                    ORDER BY round_no""",
                (match_no,),
            )
            rounds = cur.fetchall()

        stage_index = {1: 1, 2: 2, 3: 3}
        coefficient = 3.6  # коэф. по умолчанию для Fatality
        # Пробуем подтянуть точное значение из матча
        with self.db._conn() as c:  # noqa: SLF001
            cur = c.execute(
                "SELECT fbr_fatality FROM matches WHERE match_no = ?",
                (match_no,),
            )
            row = cur.fetchone()
            if row and row["fbr_fatality"]:
                coefficient = float(row["fbr_fatality"])

        profit_total = 0.0
        win = False
        for r in rounds:
            stage = stage_index.get(int(r["round_no"]), 1)
            if r["finisher"] == "F":
                new_bal = self.balance.settle(
                    win=True, stage=stage, coefficient=coefficient,
                    reason=f"{match_no} F r{r['round_no']}",
                )
                profit_total += new_bal  # просто отметим, точный delta ниже
                win = True
                break
            else:
                self.balance.settle(
                    win=False, stage=stage, coefficient=coefficient,
                    reason=f"{match_no} miss r{r['round_no']}",
                )

        result_label = "WIN" if win else "LOSS"
        self.db.close_signal(int(sig["id"]), result=result_label, profit=profit_total)

        # Коротко отчитаемся в канал сигналов о результате
        msg = f"Результат {match_no}: {result_label}. Баланс: {self.balance.current():.2f}"
        try:
            await self.app.bot.send_message(chat_id=settings.signal_chat, text=msg)
        except Exception as e:
            log.exception("Failed to publish settlement: %s", e)

    # ------------------------------------------------------------- PTB hooks

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "MKX Signal Bot запущен. Доступно: /stats, /balance"
        )

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.balance.summary()
        await update.message.reply_text(
            f"Баланс: {s['current']:.2f} (старт {s['start']:.0f})\n"
            f"Ставки: Р1={s['bets']['r1']}, Р2={s['bets']['r2']}, Р3={s['bets']['r3']}"
        )

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        day = self.db.stats_day()
        week = self.db.stats_week()
        month = self.db.stats_month()

        def fmt(label: str, s: dict) -> str:
            wr = f"{(s['wins']/s['total']*100):.1f}%" if s["total"] else "-"
            return (f"{label}: всего {s['total']} "
                    f"(W {s['wins']} / L {s['losses']}, WR {wr}, "
                    f"Profit {s['profit']:+.2f})")

        await update.message.reply_text(
            "\n".join([
                fmt("День", day),
                fmt("Неделя", week),
                fmt("Месяц", month),
                f"Баланс сейчас: {self.balance.current():.2f}",
            ])
        )


# --------------------------------------------------------------- entry point


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bot = MKXBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
