FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    fontconfig \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Скачиваем и устанавливаем шрифт Montserrat Bold
RUN wget -O Montserrat.zip "https://fonts.google.com/download?family=Montserrat" && \
    unzip -o Montserrat.zip -d /usr/share/fonts/truetype/montserrat/ && \
    fc-cache -fv && \
    rm Montserrat.zip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["sh", "-c", "python bot.py"]
