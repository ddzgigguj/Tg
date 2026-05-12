TELETHON_API_ID=21427577
TELETHON_API_HASH=db6682b9135f71c82905a59a34fd78f2
TELETHON_SESSION=mkx_session

# Канал-источник статистики (формат @statamk10 из ТЗ)
SOURCE_CHANNEL=@statamk10

# Bot API для публикации сигналов (python-telegram-bot)
TELEGRAM_BOT_TOKEN=6782718995:AAERZGo8zmD4PZ4B3rssSrB-9q321vKTlz0
# Куда публиковать сигналы.
# Варианты:
#   @my_signal_group           — публичная группа/канал по юзернейму
#   -1001234567890             — числовой chat_id приватной супергруппы/канала
# Как получить числовой id: добавьте бота в группу администратором, напишите в ней
# команду /start — бот один раз залогирует "Добавлен в чат с id=<N>".
SIGNAL_CHAT=-1003837177863

# База данных
DB_PATH=mkx_bot.db

# Банк и базовые ставки (масштабируются пропорционально балансу, см. balance.py)
START_BALANCE=1000
BET_R1=100
BET_R2=220
BET_R3=480