# Образ бота «Мафия Онлайн»
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала зависимости (кэшируется слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код
COPY bot ./bot
COPY alembic.ini .
COPY alembic ./alembic

# Каталоги для БД и логов
RUN mkdir -p /app/data /app/logs

# Неопривилегированный пользователь
RUN useradd -m mafia && chown -R mafia:mafia /app
USER mafia

CMD ["python", "-m", "bot.main"]
