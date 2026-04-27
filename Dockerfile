FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости для Pillow
RUN apt-get update && apt-get install -y \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Скачиваем шрифт Montserrat Black по работающей ссылке
RUN wget -O Montserrat-Black.ttf https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat-Black.ttf

# Копируем и устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY bot.py .

# Команда для запуска
CMD ["sh", "-c", "python bot.py"]
