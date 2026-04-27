import asyncio
import sqlite3
import csv
import os
import io
from io import StringIO
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import FastAPI
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import httpx
from newspaper import Article
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"

# Настройки для обработки фото
TARGET_WIDTH = 750
TARGET_HEIGHT = 938
GRADIENT_HEIGHT_PCT = 0.48
FONT_PATH = "Montserrat-Black.ttf"

# Хранилище для найденных новостей
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON published_news(url)")
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

# ==================== ПАРСЕР CSV ====================
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
                    'published_at': row.get('Date', datetime.now().isoformat()),
                })
            return news_list[:limit]
    except Exception as e:
        print(f"❌ Ошибка при чтении CSV: {e}")
        return []

# ==================== ПАРСЕР СТАТЬИ ====================
async def fetch_full_article(url: str) -> Optional[Dict]:
    try:
        loop = asyncio.get_event_loop()
        article = await loop.run_in_executor(None, lambda: Article(url, language='ru'))
        article.download()
        article.parse()
        
        title = article.title or "Без заголовка"
        full_text = article.text or "Текст статьи не найден."
        if len(full_text) > 800:
            full_text = full_text[:800] + "\n\n...(продолжение на сайте)"
        
        return {
            'title': title,
            'text': full_text,
            'image_url': article.top_image
        }
    except Exception as e:
        print(f"❌ Ошибка парсинга {url}: {e}")
        return None

# ==================== ОБРАБОТКА ФОТО ====================
def crop_to_4x5(img: Image.Image) -> Image.Image:
    w, h = img.size
    target_ratio = 4 / 5
    cur_ratio = w / h
    if cur_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))

def apply_bottom_gradient(img: Image.Image, height_pct: float, max_alpha: int = 220) -> Image.Image:
    w, h = img.size
    gh = int(h * height_pct)
    if gh <= 0:
        return img
    overlay_alpha = Image.new("L", (w, h), 0)
    grad = Image.new("L", (1, gh), 0)
    for y in range(gh):
        a = int(max_alpha * (y / max(1, gh - 1)))
        grad.putpixel((0, y), a)
    grad = grad.resize((w, gh))
    overlay_alpha.paste(grad, (0, h - gh))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    base = img.convert("RGBA")
    overlay = Image.composite(black, Image.new("RGBA", (w, h), (0, 0, 0, 0)), overlay_alpha)
    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 4) -> List[str]:
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = ' '.join(current_line + [word])
        bbox = font.getbbox(test_line)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
        if len(lines) >= max_lines:
            break
    if current_line and len(lines) < max_lines:
        lines.append(' '.join(current_line))
    return lines

def process_photo(photo_bytes: bytes, title_text: str) -> io.BytesIO:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    img = crop_to_4x5(img)
    img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.Resampling.LANCZOS)
    img = ImageEnhance.Brightness(img).enhance(0.9)
    img = apply_bottom_gradient(img, GRADIENT_HEIGHT_PCT, max_alpha=220)
    
    try:
        font = ImageFont.truetype(FONT_PATH, 52)
    except:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        except:
            font = ImageFont.load_default()
    
    draw = ImageDraw.Draw(img)
    margin_x = int(img.width * 0.06)
    margin_bottom = int(img.height * 0.08)
    max_text_width = img.width - 2 * margin_x
    text_lines = wrap_text(title_text.upper(), font, max_text_width, max_lines=4)
    line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    spacing = int(line_height * 0.2)
    total_text_height = len(text_lines) * line_height + (len(text_lines) - 1) * spacing
    y = img.height - margin_bottom - total_text_height
    
    for line in text_lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        x = (img.width - line_width) // 2
        draw.text((x, y), line, font=font, fill="white")
        y += line_height + spacing
    
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
    output.seek(0)
    return output

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
        "📌 *Как работать:*\n"
        "1️⃣ Нажми «Начать парсинг»\n"
        "2️⃣ Я покажу 10 последних новостей\n"
        "3️⃣ Под каждой новостью выбери «Опубликовать» или «Пропустить»\n\n"
        "👇 *Нажми кнопку ниже*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "start_parsing":
        await query.edit_message_text("⏳ Парсинг новостей... Загружаю 10 последних материалов...")
        
        news_items = await fetch_news_from_csv(10)
        if not news_items:
            await query.edit_message_text("❌ Не удалось загрузить новости. Попробуй позже.", reply_markup=get_main_keyboard())
            return
        
        pending_news.clear()
        await query.edit_message_text(f"📰 Найдено {len(news_items)} новостей. Загружаю подробности...")
        
        for i, item in enumerate(news_items):
            if is_already_published(item['url']):
                continue
            
            article = await fetch_full_article(item['url'])
            if not article:
                continue
            
            news_id = f"{i}_{abs(hash(item['url']))}"
            
            photo_io = None
            if article.get('image_url'):
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(article['image_url'])
                        if resp.status_code == 200:
                            photo_io = process_photo(resp.content, article['title'])
                except Exception as e:
                    print(f"Ошибка фото: {e}")
            
            pending_news[news_id] = {
                'title': article['title'],
                'url': item['url'],
                'text': article['text'],
                'photo': photo_io
            }
            
            caption = f"📰 *{article['title']}*\n\n{article['text']}\n\n🔗 [Читать на сайте]({item['url']})"
            
            if photo_io:
                await query.message.reply_photo(photo=photo_io, caption=caption, parse_mode="Markdown", reply_markup=get_news_keyboard(news_id))
            else:
                await query.message.reply_text(caption, parse_mode="Markdown", disable_web_page_preview=False, reply_markup=get_news_keyboard(news_id))
            
            await asyncio.sleep(0.5)
        
        await query.message.reply_text("✅ Готово! Нажми «Опубликовать» под понравившимися новостями.", reply_markup=get_main_keyboard())
    
    elif data.startswith("publish:"):
        news_id = data.split(":")[1]
        news = pending_news.get(news_id)
        if not news:
            await query.edit_message_text("❌ Новость не найдена.")
            return
        
        try:
            caption = f"📰 *{news['title']}*\n\n{news['text']}\n\n🔗 [Читать полностью]({news['url']})\n\n#Гродно #Новости"
            
            if news['photo']:
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=news['photo'], caption=caption, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode="Markdown", disable_web_page_preview=False)
            
            save_published(news['url'], news['title'])
            await query.edit_message_caption(caption="✅ Опубликовано!", reply_markup=None)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")
    
    elif data.startswith("skip:"):
        news_id = data.split(":")[1]
        pending_news.pop(news_id, None)
        await query.edit_message_caption(caption="⏭️ Пропущено", reply_markup=None)

# ==================== ВЕБ-СЕРВЕР ====================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Grodno News Bot with manual publishing"}

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
    
    print("✅ Бот запущен! Жду команды...")
    return application

if __name__ == "__main__":
    import threading
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    bot_task = loop.create_task(run_bot())
    
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    server_thread = threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=port))
    server_thread.start()
    
    loop.run_forever()
