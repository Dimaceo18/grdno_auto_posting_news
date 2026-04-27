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

def cleanup_old_news():
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
    
    if datetime.now().hour == 3:
        cleanup_old_news()

async def periodic_checker():
    while True:
        await check_and_send()
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== ВЕБ-СЕРВЕР ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск планировщика
    init_db()
    task = asyncio.create_task(periodic_checker())
    print("✅ Бот запущен! Проверка новостей каждые", CHECK_INTERVAL, "секунд")
    
    yield
    
    # Остановка
    task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Grodno News Bot"}

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.get("/stats")
async def stats():
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    return {"total_news": count}
