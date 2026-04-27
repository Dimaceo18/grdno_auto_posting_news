import asyncio
import sqlite3
import csv
import os
import re
from io import StringIO
from datetime import datetime
from typing import List, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Bot
import httpx
from newspaper import Article

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 300 секунд = 5 минут

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    """Создаём таблицу для хранения отправленных новостей"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                sent_at TIMESTAMP,
                published_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON sent_news(url)")
    print("✅ База данных готова")

def is_news_sent(url: str) -> bool:
    """Проверяем, не отправляли ли эту новость"""
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute("SELECT 1 FROM sent_news WHERE url = ?", (url,)).fetchone()
        return result is not None

def save_news(url: str, title: str, published_at: str = None):
    """Сохраняем отправленную новость"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_news (url, title, sent_at, published_at) VALUES (?, ?, ?, ?)",
            (url, title, datetime.now(), published_at)
        )

def cleanup_old_news():
    """Удаляем новости старше 30 дней"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-30 days')")
        print("🧹 Очистка БД: удалены старые записи")

# ==================== ПАРСЕР CSV ====================
def clean_html(html_text: str) -> str:
    """Очистка HTML-тегов (для краткого описания, если понадобится)"""
    clean = re.sub(r'<[^>]+>', '', html_text)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()[:300]

async def fetch_new_news_from_csv() -> List[Dict]:
    """
    Скачиваем CSV-файл с RSS.app и возвращаем список новых ссылок
    """
    new_news = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(CSV_URL, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; GrodnoNewsBot/1.0)'
            })
            response.raise_for_status()

            reader = csv.DictReader(StringIO(response.text))

            for row in reader:
                url = row['Link']
                if is_news_sent(url):
                    continue

                new_news.append({
                    'url': url,
                    'published_at': row.get('Date', datetime.now().isoformat()),
                })

        print(f"📊 Найдено новых ссылок: {len(new_news)}")
        return new_news
    except Exception as e:
        print(f"❌ Ошибка при чтении CSV: {e}")
        return []

# ==================== ПАРСЕР ПОЛНОЙ СТАТЬИ ====================
async def fetch_full_article(url: str) -> Dict | None:
    """
    С помощью newspaper3k получаем заголовок, полный текст и главное изображение статьи
    """
    try:
        # Используем выполнение в отдельном потоке, т.к. newspaper синхронный
        loop = asyncio.get_event_loop()
        article = await loop.run_in_executor(None, lambda: Article(url, language='ru'))

        # Загрузка и парсинг
        article.download()
        article.parse()

        title = article.title or "Без заголовка"
        full_text = article.text or "Текст статьи не найден."

        # Обрезаем текст, если он слишком длинный (Telegram лимит ~4096 символов)
        if len(full_text) > 3800:
            full_text = full_text[:3800] + "\n\n...(продолжение на сайте)"

        top_image = article.top_image  # Может быть None

        return {
            'title': title,
            'text': full_text,
            'image_url': top_image
        }
    except Exception as e:
        print(f"❌ Ошибка при парсинге статьи {url}: {e}")
        return None

# ==================== ОТПРАВКА В TELEGRAM ====================
async def send_full_news(article_data: Dict, original_url: str) -> bool:
    """
    Отправляет новость в Telegram: фото (если есть) + заголовок + текст + ссылка под фото
    """
    bot = Bot(token=BOT_TOKEN)

    # Формируем подпись под фото
    caption = (
        f"<b>{article_data['title']}</b>\n\n"
        f"{article_data['text']}\n\n"
        f"<a href='{original_url}'>📖 Читать на сайте</a>"
    )

    # Telegram ограничивает длину подписи 1024 символами
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    try:
        if article_data['image_url']:
            # Отправляем фото с подписью
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=article_data['image_url'],
                caption=caption,
                parse_mode='HTML'
            )
        else:
            # Если картинки нет, отправляем просто текст
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode='HTML',
                disable_web_page_preview=False
            )
        print(f"✅ Отправлено: {article_data['title'][:50]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False

async def send_text_fallback(title: str, url: str) -> bool:
    """
    Резервная отправка только ссылки, если не удалось спарсить полную статью
    """
    bot = Bot(token=BOT_TOKEN)
    message = f"<b>{title}</b>\n\n<a href='{url}'>🔗 Читать на сайте</a>"

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=False
        )
        print(f"⚠️ Отправлена ссылка (не удалось получить полный текст): {title[:50]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки ссылки: {e}")
        return False

# ==================== ПЛАНИРОВЩИК ====================
async def check_and_send():
    """Основная логика: новые новости → парсинг → отправка"""
    print(f"🔍 Проверка новостей: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Получаем новые ссылки из CSV
    new_links = await fetch_new_news_from_csv()

    # 2. Для каждой ссылки парсим полную статью и отправляем
    for item in new_links:
        url = item['url']
        print(f"📰 Парсинг: {url}")

        article_data = await fetch_full_article(url)

        if article_data:
            # Успешно: отправляем с фото и полным текстом
            success = await send_full_news(article_data, url)
            if success:
                save_news(url, article_data['title'], item['published_at'])
        else:
            # Неудачно: отправляем заголовок из CSV + ссылку
            # Пытаемся хотя бы заголовок достать из CSV, но у нас его нет в new_links.
            # Поэтому в таком случае лучше пропустить, или сделать отдельный запрос к CSV ещё раз.
            print(f"⚠️ Не удалось распарсить {url}, пропускаем")
            # Минимальный fallback (можно удалить)
            fallback_title = "Новость (полный текст не загружен)"
            await send_text_fallback(fallback_title, url)
            save_news(url, fallback_title, item['published_at'])

        # Пауза между новостями, чтобы не спамить Telegram API
        await asyncio.sleep(3)

    # Раз в сутки чистим БД
    if datetime.now().hour == 3:
        cleanup_old_news()

    print(f"✅ Цикл завершён, следующая проверка через {CHECK_INTERVAL} секунд\n")

async def periodic_checker():
    """Бесконечный цикл проверки новостей"""
    while True:
        await check_and_send()
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== ВЕБ-СЕРВЕР (FASTAPI) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск планировщика при старте сервера
    init_db()
    task = asyncio.create_task(periodic_checker())
    print(f"✅ Бот запущен! Проверка новостей каждые {CHECK_INTERVAL} секунд")
    yield
    # Остановка при выключении
    task.cancel()
    print("🛑 Бот остановлен")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "Grodno Full News Bot",
        "interval_seconds": CHECK_INTERVAL,
        "version": "2.0 (with newspaper3k)"
    }

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.get("/stats")
async def stats():
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    return {"total_news_sent": count}
