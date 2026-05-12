"""
Склейка:

  * Telethon-userbot (вход ПО НОМЕРУ + КОДУ из SMS/Telegram-сессии) читает
    канал-источник. При первом запуске запрашивает номер телефона,
    SMS-код и, если включена двухфакторка, пароль. Дальше авторизация
    сохраняется в файле `<TELETHON_SESSION>.session` и повторный вход
    не требуется.

  * python-telegram-bot (Bot API, токен от @BotFather) используется
    только для публикации сигналов и команд. Бот пишет НЕ пользователю,
    а в группу/канал `SIGNAL_CHAT` (из .env).

При первом запуске полезно добавить бота в целевую группу и написать
в ней `/start` — бот залогирует её chat_id, который затем прописывается
в переменную окружения SIGNAL_CHAT.
"""
from __future__ import annotations

import asyncio
import logging
import os
from getpass import getpass
from typing import List, Tuple

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

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
                "бот не сможет читать канал. Получите их на "
                "https://my.telegram.org и пропишите в .env"
            )
        self.userbot = TelegramClient(
            settings.telethon_session,
            settings.telethon_api_id,
            settings.telethon_api_hash,
        )

        if not settings.telegram_bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN не задан. Получите токен у @BotFather."
            )
        if not settings.signal_chat:
            # Не блокируем старт — даём возможность попросить id через /start,
            # но громко предупреждаем в логе.
            log.warning(
                "SIGNAL_CHAT пуст. Бот не сможет публиковать сигналы, пока "
                "вы не добавите его в группу и не пропишете в .env её chat_id. "
                "Подсказка: добавьте бота в целевую группу и отправьте в ней /start."
            )

        self.app: Application = (
            ApplicationBuilder().token(settings.telegram_bot_token).build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("balance", self._cmd_balance))

    # ----------------------------------------------------------- run
    async def run(self) -> None:
        await self._userbot_login()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()  # type: ignore[union-attr]

        @self.userbot.on(events.NewMessage(chats=settings.source_channel))
        async def _on_new(event):
            await self._handle_message(event.message.id, event.raw_text or "")

        @self.userbot.on(events.MessageEdited(chats=settings.source_channel))
        async def _on_edit(event):
            await self._handle_message(event.message.id, event.raw_text or "")

        log.info(
            "MKX bot started. Listening %s → publishing to %r",
            settings.source_channel,
            settings.signal_chat or "(SIGNAL_CHAT не задан)",
        )
        try:
            await self.userbot.run_until_disconnected()
        finally:
            await self.app.updater.stop()  # type: ignore[union-attr]
            await self.app.stop()
            await self.app.shutdown()

    # ---------------------------------------------------- Telethon login
    async def _userbot_login(self) -> None:
        """Первый запуск: Telethon спросит номер и код (и пароль 2FA, если
        включён). Сессия сохранится в файл `<session>.session` — при
        последующих запусках авторизация произойдёт без вопросов.

        Все интерактивные подсказки делаем на русском, чтобы владелец
        аккаунта понимал, что именно вводит.
        """
        session_file = f"{settings.telethon_session}.session"
        is_first = not os.path.exists(session_file)
        if is_first:
            log.info(
                "Первый запуск: нужно войти в Telegram-аккаунт, с которого "
                "бот будет читать канал %s.",
                settings.source_channel,
            )
            print("=" * 60)
            print("  ВХОД В АККАУНТ TELEGRAM (Telethon / userbot)")
            print("=" * 60)
            print("Введите номер телефона (например, +79001234567):")
        await self.userbot.connect()
        if not await self.userbot.is_user_authorized():
            phone = input("Phone: ").strip()
            try:
                await self.userbot.send_code_request(phone)
            except Exception as e:
                raise RuntimeError(
                    f"Не удалось отправить код на {phone}: {e}"
                ) from e
            code = input("Введите код из SMS / Telegram: ").strip()
            try:
                await self.userbot.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                pwd = getpass("Включена двухфакторная защита. Пароль 2FA: ")
                await self.userbot.sign_in(password=pwd)
            me = await self.userbot.get_me()
            log.info("Вход выполнен: @%s (id=%s). Сессия сохранена в %s",
                     me.username, me.id, session_file)
        else:
            me = await self.userbot.get_me()
            log.info(
                "Сессия уже авторизована: @%s (id=%s). Файл сессии: %s",
                me.username, me.id, session_file,
            )

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

    # --------------------------------------------------- publish helpers
    async def _publish(self, text: str) -> None:
        """Отправка сообщения в целевой чат. Все ошибки логируются, но
        дальнейшей работе бота не мешают."""
        if not settings.signal_chat:
            log.error(
                "Не могу опубликовать сообщение — SIGNAL_CHAT не задан. "
                "Текст был бы: %s", text.replace("\n", " | ")[:200],
            )
            return
        try:
            await self.app.bot.send_message(
                chat_id=settings.signal_chat, text=text,
            )
        except Exception as e:
            log.exception("Ошибка отправки в %r: %s", settings.signal_chat, e)

    async def _send_signal(self, match_no: str, res: AnalysisResult) -> None:
        bets = self.balance.scaled_bets()
        reasons_lines = "\n".join(
            f"  • {k}: {v}" for k, v in res.reasons.items()
        )
        corridor_line = (
            f"Коридор: {res.corridor} (P(F)={res.corridor_prob:.2f})"
            if res.corridor_prob is not None
            else f"Коридор: {res.corridor}"
        )
        text = (
            "🎯 ВХОД в стратегию «Золотой догон v3.0»\n"
            f"Матч: {match_no}\n"
            f"Целевой диапазон раундов: {res.target_range}\n"
            f"{corridor_line}\n"
            "Подтверждения:\n"
            f"{reasons_lines}\n"
            f"Текущий баланс: {self.balance.current():.2f}\n"
            f"Ставки (с масштабом): Р1={bets.r1}, Р2={bets.r2}, Р3={bets.r3}"
        )
        await self._publish(text)
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
        if tr == "1-3":
            low, high = 1, 3
        elif tr == "4-6":
            low, high = 4, 6
        else:
            log.error(
                "Signal %s has unknown target_range=%r — cannot settle",
                match_no, tr,
            )
            return

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
                "SELECT fbr_fatality FROM matches WHERE match_no=?",
                (match_no,),
            )
            row = cur.fetchone()
            fat_coef = (
                float(row["fbr_fatality"]) if row and row["fbr_fatality"]
                else 3.5
            )

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
        await self._publish(msg)

    # ------------------------------------------------------ PTB commands
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        log.info(
            "/start from chat id=%s type=%s title=%r by user=%s",
            chat.id, chat.type, chat.title, user.username if user else None,
        )
        # Если это групповой чат — подскажем пользователю его id, чтобы он
        # смог прописать его в .env как SIGNAL_CHAT.
        if chat.type in ("group", "supergroup", "channel"):
            text = (
                "MKX Signal Bot запущен.\n"
                f"chat_id этой группы: <code>{chat.id}</code>\n"
                "Пропишите его в переменную SIGNAL_CHAT в .env и "
                "перезапустите бота, чтобы сигналы приходили сюда."
            )
            await update.message.reply_html(text)
        else:
            await update.message.reply_text(
                "MKX Signal Bot (Cybernagual v2) запущен. "
                "Команды: /balance, /stats.\n"
                "Чтобы получать сигналы в группу, добавьте меня туда, "
                "отправьте в ней /start и пропишите chat_id в SIGNAL_CHAT."
            )

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.balance.summary()
        await update.message.reply_text(
            f"Баланс: {s['current']:.2f} (старт {s['start']:.0f})\n"
            f"Ставки: Р1={s['bets']['r1']}, Р2={s['bets']['r2']}, "
            f"Р3={s['bets']['r3']}"
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
