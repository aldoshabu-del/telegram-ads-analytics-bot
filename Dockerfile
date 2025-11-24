FROM python:3.11-slim

# Не буферизовать вывод и не писать .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Системные зависимости (нужны для numpy / pandas / scikit-learn)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY TG_bot.py .

# Токен будем передавать через переменную окружения
ENV TELEGRAM_BOT_TOKEN=""

CMD ["python", "TG_bot.py"]
