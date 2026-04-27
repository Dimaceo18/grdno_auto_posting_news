import asyncio
import sqlite3
import csv
import os
from io import StringIO
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import FastAPI
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import httpx
from bs4 import BeautifulSoup

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"

# Хранилище
pending_news: Dict[str, Dict] = {}

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                published_at TIMESTAMP
            )
        """)
    print("✅ База данных готова")

def is_already_published(url: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute("SELECT 1 FROM published_news WHERE url = ?", (url,)).fetchone()
        return result is not None

def save_published(url: str, title: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO published_news (url, title, published_at) VALUES (?, ?, ?)",
            (url, title, datetime.now())
        )

# ==================== ПАРСЕР ====================
async def fetch_news_from_csv(limit: int = 10) -> List[Dict]:
    news_list = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(CSV_URL, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; GrodnoBot/1.0)'
            })
            response.raise_for_status()
            reader = csv.DictReader(StringIO(response.text))
            for row in reader:
                news_list.append({
                    'url': row['Link'],
                    'title': row.get('Title', ''),
                    'description': row.get('Description', ''),
                    'image': None,  # Будем искать позже
                    'published_at': row.get('Date', datetime.now().isoformat()),
                })
            return news_list[:limit]
    except Exception as e:
        print(f"❌ Ошибка при чтении CSV: {e}")
        return []

async def fetch_article_image(url: str) -> Optional[str]:
    """Ищет главное изображение статьи через og:image"""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            response.raise_for_status()
            html_content = response.text
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Ищем og:image (самый надёжный способ)
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
            if image_url.startswith('//'):
                image_url = 'https:' + image_url
            print(f"✅ Найдено фото: {image_url[:80]}...")
            return image_url
        
        # Альтернатива: twitter:image
        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if twitter_image and twitter_image.get('content'):
            image_url = twitter_image['content']
            print(f"✅ Найдено фото (twitter): {image_url[:80]}...")
            return image_url
        
        print(f"⚠️ Фото не найдено для {url}")
        return None
    except Exception as e:
        print(f"❌ Ошибка поиска фото {url}: {e}")
        return None

async def fetch_article_text(url: str) -> str:
    """Извлекает текст статьи"""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
        
        # Удаляем скрипты и стили
        for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            tag.decompose()
        
        # Ищем контейнер со статьёй
        article = soup.find('article') or soup.find('div', class_=re.compile(r'(content|post-content|entry-content)'))
        
        if article:
            paragraphs = article.find_all('p')
        else:
            paragraphs = soup.find_all('p')
        
        text_parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if len(text) > 40:
                text_parts.append(text)
        
        full_text = '\n\n'.join(text_parts[:15]) if text_parts else "Текст статьи не найден."
        
        # Ограничиваем длину
        if len(full_text) > 800:
            full_text = full_text[:800] + "\n\n...(продолжение на сайте)"
        
        return full_text
    except Exception as e:
        print(f"❌ Ошибка получения текста {url}: {e}")
        return "Не удалось загрузить текст статьи."

# ==================== КНОПКИ ====================
def get_main_keyboard():
    keyboard = [[InlineKeyboardButton("📰 Начать парсинг (10 новостей)", callback_data="start_parsing")]]
    return InlineKeyboardMarkup(keyboard)

def get_news_keyboard(news_id: str):
    keyboard = [[
        InlineKeyboardButton("✅ Опубликовать в канал", callback_data=f"publish:{news_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{news_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот новостей Гродно*\n\n"
        "1️⃣ Нажми «Начать парсинг»\n"
        "2️⃣ Я покажу 10 последних новостей\n"
        "3️⃣ Под каждой новостью выбери «Опубликовать» или «Пропустить»\n\n"
        "👇 Нажми кнопку ниже",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "start_parsing":
        await query.edit_message_text("⏳ Парсинг новостей... Загружаю 10 последних материалов...")
        
        news_items = await fetch_news_from_csv(10)
        if not news_items:
            await query.message.reply_text("❌ Не удалось загрузить новости.", reply_markup=get_main_keyboard())
            return
        
        pending_news.clear()
        
        for i, item in enumerate(news_items):
            if is_already_published(item['url']):
                await query.message.reply_text(f"⏭️ *Уже было опубликовано:*\n{item['title'][:80]}...", parse_mode="Markdown")
                continue
            
            status_msg = await query.message.reply_text(f"📡 Загружаю: {item['title'][:60]}...")
            
            # Ищем фото
            image_url = await fetch_article_image(item['url'])
            
            # Получаем текст
            article_text = await fetch_article_text(item['url'])
            
            news_id = f"{i}_{abs(hash(item['url']))}"
            
            pending_news[news_id] = {
                'title': item['title'],
                'url': item['url'],
                'text': article_text,
                'image_url': image_url
            }
            
            caption = f"📰 *{item['title']}*\n\n{article_text}\n\n🔗 [Читать на сайте]({item['url']})"
            
            await status_msg.delete()
            
            # Отправляем с фото, если есть
            if image_url:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(image_url)
                        if resp.status_code == 200:
                            await query.message.reply_photo(
                                photo=resp.content,
                                caption=caption,
                                parse_mode="Markdown",
                                reply_markup=get_news_keyboard(news_id)
                            )
                            print(f"✅ Отправлено с фото: {item['title'][:50]}...")
                        else:
                            await query.message.reply_text(
                                caption,
                                parse_mode="Markdown",
                                reply_markup=get_news_keyboard(news_id)
                            )
                except Exception as e:
                    print(f"❌ Ошибка отправки фото: {e}")
                    await query.message.reply_text(
                        caption,
                        parse_mode="Markdown",
                        reply_markup=get_news_keyboard(news_id)
                    )
            else:
                await query.message.reply_text(
                    caption,
                    parse_mode="Markdown",
                    reply_markup=get_news_keyboard(news_id)
                )
            
            await asyncio.sleep(0.5)
        
        await query.message.reply_text("✅ Готово!", reply_markup=get_main_keyboard())
    
    elif data.startswith("publish:"):
        news_id = data.split(":")[1]
        news = pending_news.get(news_id)
        if not news:
            return
        
        try:
            caption = f"📰 *{news['title']}*\n\n{news['text']}\n\n🔗 [Читать полностью]({news['url']})\n\n#Гродно #Новости"
            
            if news.get('image_url'):
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(news['image_url'])
                        if resp.status_code == 200:
                            await context.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=resp.content,
                                caption=caption,
                                parse_mode="Markdown"
                            )
                            print(f"✅ Опубликовано с фото: {news['title'][:50]}...")
                        else:
                            await context.bot.send_message(
                                chat_id=CHANNEL_ID,
                                text=caption,
                                parse_mode="Markdown"
                            )
                except Exception as e:
                    print(f"❌ Ошибка: {e}")
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=caption,
                        parse_mode="Markdown"
                    )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode="Markdown",
                    disable_web_page_preview=False
                )
            
            save_published(news['url'], news['title'])
            await query.edit_message_caption(caption="✅ Опубликовано в канал!")
            pending_news.pop(news_id, None)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")
    
    elif data.startswith("skip:"):
        news_id = data.split(":")[1]
        pending_news.pop(news_id, None)
        try:
            await query.edit_message_caption(caption="⏭️ Пропущено")
        except:
            pass

# ==================== ВЕБ-СЕРВЕР ====================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Grodno News Bot"}

@app.get("/health")
async def health():
    return {"status": "alive"}

# ==================== ЗАПУСК ====================
async def run_bot():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    print("✅ Бот запущен!")
    return application

if __name__ == "__main__":
    import threading
    import uvicorn
    import re
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    loop.create_task(run_bot())
    
    port = int(os.getenv("PORT", 10000))
    server_thread = threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=port))
    server_thread.start()
    
    loop.run_forever()
