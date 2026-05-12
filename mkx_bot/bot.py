"""
Склейка: Telethon читает канал-источник, парсер → аналайзер →
публикация сигнала через python-telegram-bot → закрытие сигнала
после того, как заходит Fatality или исчерпан целевой диапазон.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

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
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("balance", self._cmd_balance))

    # -----------------------------------------------------------------
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
            await self._handle_message(event.message.id, event.raw_text or "")

        log.info("MKX bot started; listening %s", settings.source_channel)
        try:
            await self.userbot.run_until_disconnected()
        finally:
            await self.app.updater.stop()  # type: ignore[union-attr]
            await self.app.stop()
            await self.app.shutdown()

    # ----------------------------------------------------------- handle
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

        # Сигнал уже был — не дублируем
        if self.db.signal_by_match(match.match_no) is not None:
            return

        res = self.analyzer.evaluate_new_match(match)
        if res.enter:
            await self._send_signal(match.match_no, res)

    async def _send_signal(self, match_no: str, res: AnalysisResult) -> None:
        bets = self.balance.scaled_bets()
        reasons_lines = "\n".join(
            f"  • {k}: {v}" for k, v in res.reasons.items()
        )
        text = (
            "🎯 ВХОД в стратегию «Золотой догон v3.0»\n"
            f"Матч: {match_no}\n"
            f"Целевой диапазон раундов: {res.target_range}\n"
            f"Коридор: {res.corridor} "
            f"(P(F)={res.corridor_prob:.2f})" if res.corridor_prob is not None
            else f"Коридор: {res.corridor}"
        )
        text += "\nПодтверждения:\n" + reasons_lines
        text += (
            f"\nТекущий баланс: {self.balance.current():.2f}\n"
            f"Ставки (с масштабом): Р1={bets.r1}, Р2={bets.r2}, Р3={bets.r3}"
        )
        try:
            await self.app.bot.send_message(chat_id=settings.signal_chat, text=text)
        except Exception as e:
            log.exception("Failed to publish signal: %s", e)
        self.db.save_signal(
            match_no=match_no,
            kind=res.kind,
            target_range=res.target_range,
            reason={
                "reasons": res.reasons,
                "corridor": res.corridor,
                "prob": res.corridor_prob,
                "p1_fatovost": res.p1_fatovost,
                "p2_fatovost": res.p2_fatovost,
            },
        )

    async def _settle_signal_if_any(self, match_no: str) -> None:
        sig = self.db.signal_by_match(match_no)
        if sig is None or sig["result"] is not None:
            return
        tr = sig["target_range"]
        low, high = (1, 3) if tr == "1-3" else (4, 6)

        # Поднимаем раунды целевого диапазона и коэффициент на Fatality
        with self.db._conn() as c:  # noqa: SLF001
            cur = c.execute(
                """SELECT round_no, finisher FROM rounds
                   WHERE match_no=? AND round_no BETWEEN ? AND ?
                   ORDER BY round_no""",
                (match_no, low, high),
            )
            rounds_in_range: List[Tuple[int, str]] = [
                (int(r["round_no"]), r["finisher"]) for r in cur.fetchall()
            ]
            cur = c.execute(
                "SELECT fbr_fatality FROM matches WHERE match_no=?", (match_no,),
            )
            row = cur.fetchone()
            fat_coef = float(row["fbr_fatality"]) if row and row["fbr_fatality"] else 3.5

        result_label, total_delta, win_round = self.balance.settle_dogon(
            rounds_in_range=rounds_in_range,
            fat_coefficient=fat_coef,
            match_no=match_no,
        )
        self.db.close_signal(
            int(sig["id"]), result=result_label, profit=total_delta,
            win_round=win_round,
        )

        msg = (
            f"🏁 {match_no}: {result_label}. "
            f"Δ={total_delta:+.2f}, баланс={self.balance.current():.2f}"
        )
        if win_round:
            msg += f" (Fatality в раунде {win_round})"
        try:
            await self.app.bot.send_message(chat_id=settings.signal_chat, text=msg)
        except Exception as e:
            log.exception("Failed to publish settlement: %s", e)

    # ------------------------------------------------------ PTB commands
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "MKX Signal Bot (Cybernagual v2) запущен. Команды: /balance, /stats"
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

        def fmt(label, s):
            wr = f"{s['wins']/s['total']*100:.1f}%" if s["total"] else "-"
            return (f"{label}: всего {s['total']} "
                    f"(W {s['wins']} / L {s['losses']}, WR {wr}, "
                    f"Δ {s['profit']:+.2f})")

        await update.message.reply_text(
            "\n".join([
                fmt("День", day),
                fmt("Неделя", week),
                fmt("Месяц", month),
                f"Баланс: {self.balance.current():.2f}",
            ])
        )


# ------------------------------------------------------------ entry point

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bot = MKXBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
