FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Копируем шрифт из репозитория
COPY Montserrat-Bold.ttf /usr/share/fonts/truetype/montserrat/
RUN fc-cache -fv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["sh", "-c", "python bot.py"]
