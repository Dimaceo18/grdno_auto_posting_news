import asyncio
import sqlite3
import csv
import os
from io import StringIO
from datetime import datetime
from typing import List, Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI
from telegram import Bot
from telegram.ext import CommandHandler, Application
import httpx
import re

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

# ==================== БАЗА ДАННЫХ ====================
def init_db():
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
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute("SELECT 1 FROM sent_news WHERE url = ?", (url,)).fetchone()
        return result is not None

def save_news(url: str, title: str, published_at: str = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_news (url, title, sent_at, published_at) VALUES (?, ?, ?, ?)",
            (url, title, datetime.now(), published_at)
        )

def get_last_news(limit: int = 10):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT title, url, published_at FROM sent_news ORDER BY sent_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(row) for row in result]

def cleanup_old_news():
    """Удаляем новости старше 30 дней"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-30 days')")

# ==================== ПАРСЕР ====================
def clean_html(html_text: str) -> str:
    clean = re.sub(r'<[^>]+>', '', html_text)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()[:300]

async def fetch_new_news() -> List[Dict]:
    new_news = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            
            reader = csv.DictReader(StringIO(response.text))
            
            for row in reader:
                if is_news_sent(row['Link']):
                    continue
                
                new_news.append({
                    'title': row['Title'],
                    'url': row['Link'],
                    'description': clean_html(row.get('Description', '')),
                    'published_at': row.get('Date', datetime.now().isoformat()),
                    'author': row.get('Author', 'newgrodno.by')
                })
        
        print(f"📊 Новых новостей: {len(new_news)}")
        return new_news
    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        return []

# ==================== ОТПРАВКА ====================
async def send_news(news: Dict):
    bot = Bot(token=BOT_TOKEN)
    
    try:
        pub_date = datetime.fromisoformat(news['published_at'].replace('Z', '+00:00'))
        date_str = pub_date.strftime('%d.%m.%Y %H:%M')
    except:
        date_str = "только что"
    
    message = f"""
<b>{news['title']}</b>

{news['description']}

📅 {date_str}
🏷 {news['author']}

<a href="{news['url']}">🔗 Читать полностью</a>

#Гродно #Новости
    """
    
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message.strip(),
            parse_mode='HTML',
            disable_web_page_preview=False
        )
        print(f"✅ Отправлено: {news['title'][:50]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

# ==================== ПЛАНИРОВЩИК ====================
async def check_and_send():
    print(f"🔍 Проверка: {datetime.now().strftime('%H:%M:%S')}")
    new_news = await fetch_new_news()
    
    for news in reversed(new_news):
        success = await send_news(news)
        if success:
            save_news(news['url'], news['title'], news['published_at'])
        await asyncio.sleep(1)
    
    # Раз в сутки чистим БД
    if datetime.now().hour == 3:
        cleanup_old_news()

async def periodic_checker():
    while True:
        await check_and_send()
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== КОМАНДЫ ДЛЯ БОТА ====================
async def start_command(update, context):
    await update.message.reply_text(
        "🤖 Бот новостей Гродно работает!\n\n"
        "📰 Команды:\n"
        "/last - последние 5 новостей\n"
        "/stats - статистика"
    )

async def last_news_command(update, context):
    news_list = get_last_news(5)
    if not news_list:
        await update.message.reply_text("Новостей пока нет")
        return
    
    message = "📰 *Последние новости:*\n\n"
    for i, news in enumerate(news_list, 1):
        message += f"{i}. [{news['title'][:50]}]({news['url']})\n"
    
    await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)

async def stats_command(update, context):
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"📰 Отправлено: {count} новостей\n"
        f"⏱ Интервал: {CHECK_INTERVAL // 60} мин\n"
        f"✅ Статус: активен"
    )

# ==================== ВЕБ-СЕРВЕР ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск
    init_db()
    checker_task = asyncio.create_task(periodic_checker())
    
    # Запускаем Telegram бота для команд
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("last", last_news_command))
    telegram_app.add_handler(CommandHandler("stats", stats_command))
    telegram_task = asyncio.create_task(telegram_app.run_polling())
    
    print("✅ Бот запущен!")
    
    yield
    
    # Остановка
    checker_task.cancel()
    telegram_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Grodno News Bot", "interval_seconds": CHECK_INTERVAL}

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.get("/stats")
async def stats():
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    return {"total_news": count}
