import asyncio
import sqlite3
import csv
import os
import re
import io
from io import StringIO
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import FastAPI
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

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

# ==================== ПАРСЕРЫ ====================
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

async def fetch_article_image(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
        
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
            if image_url.startswith('//'):
                image_url = 'https:' + image_url
            return image_url
        return None
    except Exception as e:
        print(f"❌ Ошибка поиска фото: {e}")
        return None

async def fetch_article_text(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
        
        for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            tag.decompose()
        
        article = soup.find('article') or soup.find('div', class_=re.compile(r'(content|post-content|entry-content)'))
        paragraphs = article.find_all('p') if article else soup.find_all('p')
        
        text_parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if len(text) > 40:
                text_parts.append(text)
        
        full_text = '\n\n'.join(text_parts[:15]) if text_parts else "Текст статьи не найден."
        if len(full_text) > 800:
            full_text = full_text[:800] + "\n\n...(продолжение на сайте)"
        
        return full_text
    except Exception as e:
        print(f"❌ Ошибка получения текста: {e}")
        return "Не удалось загрузить текст статьи."

# ==================== ОБРАБОТКА ФОТО (С ОБВОДКОЙ И СЖАТИЕМ) ====================
def process_photo(photo_bytes: bytes, title_text: str) -> io.BytesIO:
    """Обрабатывает фото с обводкой текста и сжимает до 15MB"""
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    
    # Обрезка до 4:5
    w, h = img.size
    target_ratio = 4 / 5
    cur_ratio = w / h
    if cur_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    
    # Уменьшаем размер
    img = img.resize((1080, 1350), Image.Resampling.LANCZOS)
    img = ImageEnhance.Brightness(img).enhance(0.85)
    
    # Градиент снизу
    w, h = img.size
    gh = int(h * 0.48)
    if gh > 0:
        overlay_alpha = Image.new("L", (w, h), 0)
        grad = Image.new("L", (1, gh), 0)
        for y in range(gh):
            a = int(220 * (y / max(1, gh - 1)))
            grad.putpixel((0, y), a)
        grad = grad.resize((w, gh))
        overlay_alpha.paste(grad, (0, h - gh))
        black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        base = img.convert("RGBA")
        overlay = Image.composite(black, Image.new("RGBA", (w, h), (0, 0, 0, 0)), overlay_alpha)
        img = Image.alpha_composite(base, overlay).convert("RGB")
    
    draw = ImageDraw.Draw(img)
    
    # Загрузка шрифта
    font = None
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", 72)
        except:
            font = ImageFont.load_default()
    
    # Параметры текста
    margin_x = int(img.width * 0.06)
    margin_bottom = int(img.height * 0.1)
    max_text_width = img.width - 2 * margin_x
    
    # Разбивка на строки
    title = title_text.upper()
    words = title.split()
    lines = []
    current_line = []
    
    for word in words:
        test_line = ' '.join(current_line + [word])
        if font == ImageFont.load_default():
            width = len(test_line) * 15
        else:
            bbox = font.getbbox(test_line)
            width = bbox[2] - bbox[0]
        
        if width <= max_text_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
        if len(lines) >= 4:
            break
    
    if current_line and len(lines) < 4:
        lines.append(' '.join(current_line))
    
    # Вычисление высоты
    if font == ImageFont.load_default():
        line_height = 28
        spacing = 10
    else:
        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        spacing = int(line_height * 0.25)
    
    total_text_height = len(lines) * line_height + (len(lines) - 1) * spacing
    y = img.height - margin_bottom - total_text_height
    
    # Отрисовка с обводкой
    for line in lines:
        if font == ImageFont.load_default():
            line_width = len(line) * 15
        else:
            bbox = font.getbbox(line)
            line_width = bbox[2] - bbox[0]
        
        x = (img.width - line_width) // 2
        
        # Чёрная обводка
        offsets = [(-2, -2), (-2, 2), (2, -2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)]
        for dx, dy in offsets:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
        # Белый текст
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height + spacing
    
    # Сжатие
    output = io.BytesIO()
    quality = 85
    while quality >= 60:
        output.seek(0)
        output.truncate()
        img.save(output, format="JPEG", quality=quality, subsampling=0, optimize=True)
        size = output.tell() / (1024 * 1024)
        if size <= 15:
            break
        quality -= 10
    
    output.seek(0)
    print(f"✅ Фото готово: {output.tell() / (1024 * 1024):.1f}MB")
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

def get_post_preview_keyboard():
    keyboard = [[InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")]]
    return InlineKeyboardMarkup(keyboard)

def get_publish_preview_keyboard():
    keyboard = [[InlineKeyboardButton("✅ Опубликовать в канал", callback_data="publish_designed")]]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ РЕПОСТОВ ====================
async def handle_forwarded_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовый репост"""
    message = update.message
    if not message.text:
        return
    
    if "pending_post" not in context.user_data:
        context.user_data["pending_post"] = {}
    
    context.user_data["pending_post"]["text"] = message.text
    context.user_data["pending_post"]["has_photo"] = False
    
    await message.reply_text(
        f"📝 *Получен текст для оформления!*\n\n{message.text[:300]}...\n\nНажми «Оформить пост»",
        parse_mode="Markdown",
        reply_markup=get_post_preview_keyboard()
    )

async def handle_forwarded_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото с подписью"""
    message = update.message
    if not message.photo:
        return
    
    caption = message.caption or ""
    photo = message.photo[-1]
    
    if "pending_post" not in context.user_data:
        context.user_data["pending_post"] = {}
    
    context.user_data["pending_post"]["text"] = caption
    context.user_data["pending_post"]["has_photo"] = True
    
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        context.user_data["pending_post"]["photo_bytes"] = photo_bytes
    except Exception as e:
        print(f"❌ Ошибка скачивания фото: {e}")
        await message.reply_text("❌ Не удалось загрузить фото.")
        return
    
    preview_text = caption[:300] if caption else "без текста"
    await message.reply_photo(
        photo=photo_bytes,
        caption=f"📝 *Получен пост для оформления!*\n\n{preview_text}...\n\nНажми «Оформить пост»",
        parse_mode="Markdown",
        reply_markup=get_post_preview_keyboard()
    )

# ==================== ОФОРМЛЕНИЕ ПОСТА ====================
async def design_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.user_data.get("pending_post", {})
    
    if not pending or not pending.get("text"):
        await query.edit_message_text("❌ Нет данных для оформления. Пожалуйста, отправьте пост заново.")
        return
    
    text = pending.get("text", "")
    lines = text.split('\n')
    title = lines[0][:100] if lines else text[:100]
    
    photo_io = None
    if pending.get("has_photo") and pending.get("photo_bytes"):
        try:
            await query.edit_message_text("🎨 Оформляю пост...")
            photo_io = process_photo(pending["photo_bytes"], title)
            print(f"✅ Пост оформлен: {title[:50]}...")
        except Exception as e:
            print(f"❌ Ошибка оформления фото: {e}")
            await query.edit_message_text(f"⚠️ Ошибка при оформлении фото: {e}")
            return
    else:
        await query.edit_message_text("⚠️ Для оформления нужно фото с подписью. Отправьте фото с текстом.")
        return
    
    context.user_data["designed_post"] = {
        'title': title,
        'text': text,
        'photo': photo_io
    }
    
    caption = f"📰 *{title}*\n\n{text[:500]}...\n\n✅ Пост оформлен! Нажми «Опубликовать» для отправки в канал."
    
    await query.edit_message_media(
        media=InputMediaPhoto(media=photo_io, caption=caption, parse_mode="Markdown"),
        reply_markup=get_publish_preview_keyboard()
    )

async def publish_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Публикует оформленный пост в канал"""
    query = update.callback_query
    await query.answer()
    
    designed = context.user_data.get("designed_post", {})
    
    if not designed:
        await query.edit_message_text("❌ Нет оформленного поста для публикации.")
        return
    
    try:
        caption = f"📰 *{designed['title']}*\n\n{designed['text']}\n\n#Реклама"
        
        if designed.get('photo'):
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=designed['photo'],
                caption=caption,
                parse_mode="Markdown"
            )
            print(f"✅ Опубликован оформленный пост: {designed['title'][:50]}...")
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="Markdown"
            )
        
        await query.edit_message_text("✅ Пост успешно опубликован в канал!")
        context.user_data.pop("pending_post", None)
        context.user_data.pop("designed_post", None)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка публикации: {e}")

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот новостей Гродно*\n\n"
        "*Доступные функции:*\n\n"
        "📰 *Парсинг новостей* — нажми кнопку ниже\n"
        "🔄 *Оформление постов* — отправьте фото с подписью в бот\n\n"
        "*Как оформить пост:*\n"
        "1️⃣ Отправь фото с подписью\n"
        "2️⃣ Нажми «Оформить пост»\n"
        "3️⃣ Нажми «Опубликовать в канал»\n\n"
        "👇 *Нажми кнопку для парсинга новостей*",
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
            
            image_url = await fetch_article_image(item['url'])
            article_text = await fetch_article_text(item['url'])
            
            news_id = f"{i}_{abs(hash(item['url']))}"
            
            processed_photo = None
            if image_url:
                try:
                    await status_msg.edit_text(f"📸 Обрабатываю фото...")
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        resp = await client.get(image_url)
                        if resp.status_code == 200:
                            processed_photo = process_photo(resp.content, item['title'])
                            print(f"✅ Фото обработано: {item['title'][:50]}...")
                except Exception as e:
                    print(f"❌ Ошибка обработки фото: {e}")
            
            pending_news[news_id] = {
                'title': item['title'],
                'url': item['url'],
                'text': article_text,
                'photo': processed_photo
            }
            
            caption = f"📰 *{item['title']}*\n\n{article_text}\n\n🔗 [Читать на сайте]({item['url']})"
            
            await status_msg.delete()
            
            if processed_photo:
                await query.message.reply_photo(
                    photo=processed_photo,
                    caption=caption,
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
            
            if news.get('photo'):
                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=news['photo'],
                        caption=caption,
                        parse_mode="Markdown"
                    )
                    print(f"✅ Опубликовано с фото: {news['title'][:50]}...")
                except Exception as e:
                    print(f"⚠️ Ошибка отправки фото: {e}")
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=caption,
                        parse_mode="Markdown"
                    )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode="Markdown"
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
    
    elif data == "design_post":
        await design_post_callback(update, context)
    
    elif data == "publish_designed":
        await publish_designed_callback(update, context)

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_forwarded_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_forwarded_photo))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    print("✅ Бот запущен!")
    return application

if __name__ == "__main__":
    import threading
    import uvicorn
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    loop.create_task(run_bot())
    
    port = int(os.getenv("PORT", 10000))
    server_thread = threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=port))
    server_thread.start()
    
    loop.run_forever()
