FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Скачиваем шрифт
RUN wget -O Montserrat-Black.ttf https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-Black.ttf || \
    cp /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf Montserrat-Black.ttf

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["sh", "-c", "python bot.py"]
