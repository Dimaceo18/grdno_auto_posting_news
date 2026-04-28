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
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/grodno_news")
SUGGEST_LINK = os.getenv("SUGGEST_LINK", "https://t.me/grodno_news_bot?start=suggest")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"

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

# ==================== ОЧИСТКА ТЕКСТА ====================
def clean_text(text: str) -> str:
    """Удаляет смайлики и активные ссылки из текста"""
    if not text:
        return ""
    
    # Удаляем emoji (смайлики)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # смайлики
        "\U0001F300-\U0001F5FF"  # символы
        "\U0001F680-\U0001F6FF"  # транспорт
        "\U0001F1E0-\U0001F1FF"  # флаги
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # дополнительные смайлики
        "\U0001FA70-\U0001FAFF"  # новые эмодзи
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub(r'', text)
    
    # Удаляем активные ссылки (URL)
    url_pattern = re.compile(r'https?://\S+', re.IGNORECASE)
    text = url_pattern.sub(r'', text)
    
    # Удаляем ссылки вида t.me/...
    tg_link_pattern = re.compile(r't\.me/\S+', re.IGNORECASE)
    text = tg_link_pattern.sub(r'', text)
    
    # Убираем лишние пробелы и пустые строки
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    text = '\n'.join(lines)
    
    return text.strip()

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
        if len(full_text) > 600:
            full_text = full_text[:600] + "\n\n...(продолжение на сайте)"
        return full_text
    except Exception as e:
        print(f"❌ Ошибка получения текста: {e}")
        return "Не удалось загрузить текст статьи."

# ==================== ОБРАБОТКА ФОТО ====================
def wrap_text_auto(text: str, font, max_width: int, max_lines: int = 6) -> List[str]:
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = ' '.join(current_line + [word])
        try:
            bbox = font.getbbox(test_line)
            width = bbox[2] - bbox[0]
        except:
            width = len(test_line) * 20
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
    if not photo_bytes or len(photo_bytes) == 0:
        raise ValueError("Фото пустое")
    print(f"🖼️ Обработка фото, размер: {len(photo_bytes) / 1024:.1f}KB")
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
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
    img = img.resize((1080, 1350), Image.Resampling.LANCZOS)
    img = ImageEnhance.Brightness(img).enhance(0.85)
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
    font = None
    font_size = 68
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", font_size)
        except:
            font = ImageFont.load_default()
    margin_x = int(img.width * 0.05)
    margin_bottom = int(img.height * 0.08)
    max_text_width = img.width - 2 * margin_x
    title = title_text.upper()
    lines = wrap_text_auto(title, font, max_text_width, max_lines=6)
    if font == ImageFont.load_default():
        line_height = 35
        spacing = 10
    else:
        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        spacing = int(line_height * 0.25)
    total_text_height = len(lines) * line_height + (len(lines) - 1) * spacing
    y = img.height - margin_bottom - total_text_height
    for line in lines:
        if font == ImageFont.load_default():
            line_width = len(line) * 20
        else:
            bbox = font.getbbox(line)
            line_width = bbox[2] - bbox[0]
        x = (img.width - line_width) // 2
        offsets = [(-2, -2), (-2, 2), (2, -2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)]
        for dx, dy in offsets:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height + spacing
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
    if output.getbuffer().nbytes == 0:
        raise ValueError("Результирующий файл пустой")
    print(f"✅ Фото готово: {output.getbuffer().nbytes / (1024 * 1024):.2f}MB, строк: {len(lines)}")
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

def get_video_keyboard():
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать описание", callback_data="edit_video_text")],
        [InlineKeyboardButton("📹 Опубликовать видео", callback_data="publish_video")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_preview_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_publish_button():
    keyboard = [[InlineKeyboardButton("✅ Опубликовать в канал", callback_data="publish_designed")]]
    return InlineKeyboardMarkup(keyboard)

def get_post_publish_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Прислать нам новость", url=SUGGEST_LINK)]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ ====================
async def handle_forwarded_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.photo:
        return
    
    caption = message.caption or ""
    photo = message.photo[-1]
    
    # Очищаем текст от смайликов и ссылок
    cleaned_caption = clean_text(caption)
    
    print(f"📸 Получено фото. ID: {photo.file_id}")
    
    context.chat_data["pending_post"] = {
        "type": "photo",
        "text": cleaned_caption,
        "file_id": photo.file_id,
        "original_text": caption
    }
    
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        context.chat_data["pending_post"]["photo_bytes"] = photo_bytes
        print(f"✅ Фото скачано: {len(photo_bytes)} байт")
        
        # Отправляем сам пост без лишнего текста
        await message.reply_photo(
            photo=photo.file_id,
            caption=cleaned_caption if cleaned_caption else " ",
            parse_mode="HTML",
            reply_markup=get_post_preview_keyboard()
        )
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Не удалось загрузить фото: {e}")

async def handle_forwarded_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.video:
        return
    
    caption = message.caption or ""
    
    # Очищаем текст от смайликов и ссылок
    cleaned_caption = clean_text(caption)
    
    video = message.video
    
    print(f"📹 Получено видео. ID: {video.file_id}")
    
    context.chat_data["pending_video"] = {
        "type": "video",
        "text": cleaned_caption,
        "file_id": video.file_id,
        "original_text": caption
    }
    
    await message.reply_video(
        video=video.file_id,
        caption=cleaned_caption if cleaned_caption else " ",
        parse_mode="HTML",
        reply_markup=get_video_keyboard()
    )

# ==================== РЕДАКТИРОВАНИЕ ====================
async def edit_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_for_edit"] = "photo"
    
    await query.message.reply_text(
        "✏️ *Отправьте новый текст для поста*\n\n"
        "Текст будет полностью заменён. Отправьте сообщение с новым текстом.\n"
        "Или нажмите /cancel для отмены.",
        parse_mode="Markdown"
    )

async def edit_video_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_for_edit"] = "video"
    
    await query.message.reply_text(
        "✏️ *Отправьте новый текст для видео*\n\n"
        "Текст будет полностью заменён. Отправьте сообщение с новым текстом.\n"
        "Или нажмите /cancel для отмены.",
        parse_mode="Markdown"
    )

async def handle_edited_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit_type = context.user_data.get("waiting_for_edit")
    if not edit_type:
        return
    
    new_text = update.message.text
    
    if edit_type == "photo":
        pending = context.chat_data.get("pending_post", {})
        if pending:
            pending["text"] = new_text
            context.chat_data["pending_post"] = pending
            
            await update.message.reply_text(
                f"✅ *Текст обновлён!*\n\n{new_text[:300]}...\n\nПродолжить оформление?",
                parse_mode="Markdown",
                reply_markup=get_post_preview_keyboard()
            )
    
    elif edit_type == "video":
        pending = context.chat_data.get("pending_video", {})
        if pending:
            pending["text"] = new_text
            context.chat_data["pending_video"] = pending
            
            await update.message.reply_text(
                f"✅ *Текст обновлён!*\n\n{new_text[:300]}...\n\nПродолжить?",
                parse_mode="Markdown",
                reply_markup=get_video_keyboard()
            )
    
    context.user_data["waiting_for_edit"] = None

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_edit"] = None
    await update.message.reply_text("✅ Редактирование отменено.")

# ==================== ОФОРМЛЕНИЕ ПОСТА ====================
async def design_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    
    if not pending or pending.get("type") != "photo":
        await query.message.reply_text("❌ Оформить можно только фото с подписью.")
        return
    
    full_text = pending.get("text", "")
    if not full_text:
        await query.message.reply_text("❌ Нет текста. Отправьте фото с подписью.")
        return
    
    lines = full_text.rstrip('\n').split('\n')
    title = lines[0][:150] if lines else "Пост"
    full_text_for_publish = full_text
    
    if not pending.get("photo_bytes"):
        await query.message.reply_text("❌ Нет фото. Отправьте фото заново.")
        return
    
    try:
        await query.message.reply_text("🎨 Оформляю пост...")
        
        photo_io = process_photo(pending["photo_bytes"], title)
        
        if photo_io.getbuffer().nbytes == 0:
            raise ValueError("Фото пустое после обработки")
        
        context.chat_data["designed_post"] = {
            "title": title,
            "text": full_text_for_publish,
            "photo_bytes": photo_io.getvalue()
        }
        
        preview_text = f"📰 *{title}*\n\n{full_text_for_publish[:300]}...\n\n✅ Пост оформлен! Нажми кнопку для публикации."
        
        await query.message.reply_photo(
            photo=photo_io,
            caption=preview_text,
            parse_mode="Markdown",
            reply_markup=get_publish_button()
        )
        
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        print(f"❌ Ошибка оформления: {e}")
        await query.message.reply_text(f"⚠️ Ошибка: {e}")

# ==================== ПУБЛИКАЦИЯ ====================
async def publish_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    designed = context.chat_data.get("designed_post", {})
    
    if not designed:
        await query.message.reply_text("❌ Нет оформленного поста.")
        return
    
    full_text = designed.get("text", "")
    photo_bytes = designed.get("photo_bytes")
    
    if not photo_bytes:
        await query.message.reply_text("❌ Нет фото для публикации.")
        return
    
    try:
        # Обрезаем текст до 1000 символов
        if len(full_text) > 1000:
            full_text = full_text[:1000] + "..."
        
        lines = full_text.split('\n')
        if lines:
            title_bold = f"<b>{lines[0]}</b>"
            rest_text = '\n'.join(lines[1:]) if len(lines) > 1 else ""
            
            if rest_text:
                caption = f"{title_bold}\n\n{rest_text}"
            else:
                caption = title_bold
        else:
            caption = full_text
        
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_bytes,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_post_publish_keyboard()
        )
        
        print(f"✅ Пост опубликован в канал с кнопками")
        
        await query.message.reply_text("✅ Пост опубликован в канал!")
        
        context.chat_data.pop("pending_post", None)
        context.chat_data.pop("designed_post", None)
        
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        print(f"❌ Ошибка публикации: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_video", {})
    
    if not pending or pending.get("type") != "video":
        await query.message.reply_text("❌ Нет видео для публикации.")
        return
    
    text = pending.get("text", "")
    file_id = pending.get("file_id")
    
    if not file_id:
        await query.message.reply_text("❌ Нет file_id видео.")
        return
    
    try:
        # Обрезаем текст до 1000 символов
        if len(text) > 1000:
            text = text[:1000] + "..."
        
        if text:
            lines = text.split('\n')
            if lines:
                title_bold = f"<b>{lines[0]}</b>"
                rest_text = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                if rest_text:
                    caption = f"{title_bold}\n\n{rest_text}"
                else:
                    caption = title_bold
            else:
                caption = text
        else:
            caption = " "
        
        await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_post_publish_keyboard()
        )
        
        print(f"✅ Видео опубликовано в канал с кнопками")
        
        await query.message.reply_text("✅ Видео опубликовано в канал!")
        
        context.chat_data.pop("pending_video", None)
        
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        print(f"❌ Ошибка публикации видео: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, news_id: str):
    news = pending_news.get(news_id)
    if not news:
        return
    
    try:
        # Обрезаем текст до 1000 символов
        news_text = news['text']
        if len(news_text) > 1000:
            news_text = news_text[:1000] + "..."
        
        title_bold = f"<b>{news['title']}</b>"
        caption = f"{title_bold}\n\n{news_text}\n\n🔗 {news['url']}"
        
        if news.get('photo') and news['photo'].getbuffer().nbytes > 0:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=news['photo'],
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_post_publish_keyboard()
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=get_post_publish_keyboard()
            )
        
        save_published(news['url'], news['title'])
        pending_news.pop(news_id, None)
        
    except Exception as e:
        print(f"❌ Ошибка публикации новости: {e}")
        raise e

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот новостей Гродно*\n\n"
        "*Доступные функции:*\n\n"
        "📰 *Парсинг новостей* — нажми кнопку ниже\n"
        "🖼️ *Фото* — отправьте фото с подписью (оформление)\n"
        "📹 *Видео* — отправьте видео с подписью\n\n"
        "*Редактирование:*\n"
        "✏️ Можно отредактировать текст перед публикацией\n\n"
        "*После публикации:*\n"
        "📢 Кнопки подписки и предложки новостей\n\n"
        "👇 *Нажми кнопку*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "start_parsing":
        await query.edit_message_text("⏳ Парсинг новостей...")
        
        news_items = await fetch_news_from_csv(10)
        if not news_items:
            await query.message.reply_text("❌ Не удалось загрузить новости.", reply_markup=get_main_keyboard())
            return
        
        pending_news.clear()
        
        for i, item in enumerate(news_items):
            if is_already_published(item['url']):
                continue
            
            status_msg = await query.message.reply_text(f"📡 {item['title'][:60]}...")
            
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
                except Exception as e:
                    print(f"❌ Ошибка: {e}")
            
            pending_news[news_id] = {
                'title': item['title'],
                'url': item['url'],
                'text': article_text,
                'photo': processed_photo
            }
            
            caption = f"📰 *{item['title']}*\n\n{article_text}\n\n🔗 [Читать]({item['url']})"
            
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
        await publish_news_callback(update, context, news_id)
        await query.message.reply_text("✅ Опубликовано в канал!", reply_markup=get_post_publish_keyboard())
        try:
            await query.message.delete()
        except:
            pass
    
    elif data.startswith("skip:"):
        news_id = data.split(":")[1]
        pending_news.pop(news_id, None)
        try:
            await query.message.delete()
        except:
            pass
    
    elif data == "design_post":
        await design_post_callback(update, context)
    
    elif data == "publish_video":
        await publish_video_callback(update, context)
    
    elif data == "publish_designed":
        await publish_designed_callback(update, context)
    
    elif data == "edit_text":
        await edit_text_callback(update, context)
    
    elif data == "edit_video_text":
        await edit_video_text_callback(update, context)

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
    application.add_handler(CommandHandler("cancel", cancel_edit))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_forwarded_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_forwarded_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_text))
    
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
