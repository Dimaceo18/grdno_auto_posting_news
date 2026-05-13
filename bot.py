import asyncio
import sqlite3
import csv
import os
import re
import io
import json
import uuid
from io import StringIO
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from fastapi import FastAPI
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from openai import AsyncOpenAI

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/grodno_news")
SUGGEST_LINK = os.getenv("SUGGEST_LINK", "https://t.me/grodno_news_bot?start=suggest")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"

# Каналы для публикации
CHANNELS = {
    "grodno": {
        "name": "Фидер Гродно",
        "channel_id": os.getenv("CHANNEL_ID_GRODNO", CHANNEL_ID),
        "link": os.getenv("CHANNEL_LINK_GRODNO", "https://t.me/grodno_news")
    },
    "baranovichi": {
        "name": "Фидер Барановичи",
        "channel_id": os.getenv("CHANNEL_ID_BARANOVICHI"),
        "link": os.getenv("CHANNEL_LINK_BARANOVICHI", "https://t.me/baranovichi_news")
    },
    "vitebsk": {
        "name": "Фидер Витебск",
        "channel_id": os.getenv("CHANNEL_ID_VITEBSK"),
        "link": os.getenv("CHANNEL_LINK_VITEBSK", "https://t.me/vitebsk_news")
    },
    "brest": {
        "name": "Фидер Брест",
        "channel_id": os.getenv("CHANNEL_ID_BREST"),
        "link": os.getenv("CHANNEL_LINK_BREST", "https://t.me/brest_news")
    }
}

# Инициализация DeepSeek клиента
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
) if DEEPSEEK_API_KEY else None

pending_news: Dict[str, Dict] = {}
multi_channel_posts: Dict[str, Dict] = {}

# Промпт для DeepSeek
DEEPSEEK_PROMPT = """Ты редактор новостного сайта, у тебя строгий новостной городской формат. Без обращений на вы, ты и т.д. Только новостной формат.

Тебе нужно переделывать новость с большого объема в новость на 650 символов.
Убирая всю лишнюю воду, текст, делать интересным заголовок, никаких смайликов. Сохраняй главные факты, проверяй всю информацию несколько раз, чтобы не было никаких ошибок.

Верни только готовую новость в формате:
Заголовок: (заголовок новости)
Текст: (текст новости на 650 символов)"""

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                photo_bytes BLOB,
                schedule_time TIMESTAMP,
                created_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                file_id TEXT,
                schedule_time TIMESTAMP,
                created_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_multi_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                photo_bytes BLOB,
                channels TEXT,
                schedule_time TIMESTAMP,
                created_at TIMESTAMP
            )
        """)
    print("✅ База данных готова")

def save_scheduled_post(text: str, photo_bytes: bytes, schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_posts (text, photo_bytes, schedule_time, created_at) VALUES (?, ?, ?, ?)",
            (text, photo_bytes, schedule_time, datetime.now())
        )

def save_scheduled_video(text: str, file_id: str, schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_videos (text, file_id, schedule_time, created_at) VALUES (?, ?, ?, ?)",
            (text, file_id, schedule_time, datetime.now())
        )

def save_scheduled_multi_post(text: str, photo_bytes: bytes, channels: List[str], schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_multi_posts (text, photo_bytes, channels, schedule_time, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, photo_bytes, json.dumps(channels), schedule_time, datetime.now())
        )

def get_pending_scheduled_posts() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT id, text, photo_bytes, schedule_time FROM scheduled_posts WHERE schedule_time <= ?",
            (datetime.now(),)
        ).fetchall()
        return [dict(row) for row in result]

def get_pending_scheduled_videos() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT id, text, file_id, schedule_time FROM scheduled_videos WHERE schedule_time <= ?",
            (datetime.now(),)
        ).fetchall()
        return [dict(row) for row in result]

def get_pending_scheduled_multi_posts() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT id, text, photo_bytes, channels, schedule_time FROM scheduled_multi_posts WHERE schedule_time <= ?",
            (datetime.now(),)
        ).fetchall()
        posts = []
        for row in result:
            post = dict(row)
            post['channels'] = json.loads(post['channels'])
            posts.append(post)
        return posts

def delete_scheduled_post(post_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))

def delete_scheduled_video(video_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scheduled_videos WHERE id = ?", (video_id,))

def delete_scheduled_multi_post(post_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scheduled_multi_posts WHERE id = ?", (post_id,))

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
def remove_emojis(text: str) -> str:
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub(r'', text)

def format_caption(title: str, body: str) -> str:
    if body and body.strip():
        return f"<b>{title}</b>\n{body}"
    else:
        return f"<b>{title}</b>"

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
    
    font_paths = [
        "Montserrat-Black.ttf",
        "fonts/Montserrat-Black.ttf",
        "/app/Montserrat-Black.ttf",
        "Montserrat-Bold.ttf",
    ]
    
    for font_path in font_paths:
        try:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, font_size)
                print(f"✅ Загружен шрифт: {font_path}")
                break
        except:
            continue
    
    if font is None:
        font = ImageFont.load_default()
        print("⚠️ Шрифт не найден, использую стандартный")
    
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
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_video_text")],
        [InlineKeyboardButton("🤖 Обработать текст (ИИ)", callback_data="ai_process_video")],
        [InlineKeyboardButton("📹 Опубликовать видео", callback_data="publish_video")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_video_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_preview_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")],
        [InlineKeyboardButton("🤖 Обработать текст (ИИ)", callback_data="ai_process")],
        [InlineKeyboardButton("📤 Опубликовать без оформления", callback_data="publish_raw")],
        [InlineKeyboardButton("🌍 Опубликовать в несколько каналов", callback_data="start_multi_channel")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_designed_post_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать в канал", callback_data="publish_designed")],
        [InlineKeyboardButton("🌍 Опубликовать в несколько каналов", callback_data="start_multi_channel_designed")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_designed_text")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_designed")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_ai_result_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать в канал", callback_data="publish_raw")],
        [InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")],
        [InlineKeyboardButton("🔄 Переделать текст (другой запрос)", callback_data="ai_reprocess")],
        [InlineKeyboardButton("✏️ Редактировать вручную", callback_data="edit_text")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_preview")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_video_ai_result_keyboard():
    keyboard = [
        [InlineKeyboardButton("📹 Опубликовать видео", callback_data="publish_video")],
        [InlineKeyboardButton("✏️ Редактировать вручную", callback_data="edit_video_text")],
        [InlineKeyboardButton("🔄 Переделать текст (другой запрос)", callback_data="ai_reprocess_video")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_video_preview")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_schedule_keyboard():
    schedule_times = [
        ("Через 30 мин", "30min"),
        ("9:05", "9:05"), ("10:05", "10:05"), ("10:06", "10:06"), ("11:07", "11:07"),
        ("12:08", "12:08"), ("13:09", "13:09"), ("14:10", "14:10"), ("15:11", "15:11"),
        ("16:12", "16:12"), ("17:13", "17:13"), ("18:14", "18:14"), ("19:07", "19:07"),
        ("20:08", "20:08"), ("21:09", "21:09"), ("22:11", "22:11"), ("22:45", "22:45")
    ]
    keyboard = []
    row = []
    for i, (label, value) in enumerate(schedule_times):
        row.append(InlineKeyboardButton(label, callback_data=f"schedule:{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_preview")])
    return InlineKeyboardMarkup(keyboard)

def get_video_schedule_keyboard():
    schedule_times = [
        ("Через 30 мин", "30min"),
        ("9:05", "9:05"), ("10:05", "10:05"), ("10:06", "10:06"), ("11:07", "11:07"),
        ("12:08", "12:08"), ("13:09", "13:09"), ("14:10", "14:10"), ("15:11", "15:11"),
        ("16:12", "16:12"), ("17:13", "17:13"), ("18:14", "18:14"), ("19:07", "19:07"),
        ("20:08", "20:08"), ("21:09", "21:09"), ("22:11", "22:11"), ("22:45", "22:45")
    ]
    keyboard = []
    row = []
    for i, (label, value) in enumerate(schedule_times):
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_video:{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_video_preview")])
    return InlineKeyboardMarkup(keyboard)

def get_channel_selection_keyboard(post_id: str):
    keyboard = []
    for channel_key, channel_info in CHANNELS.items():
        if channel_info["channel_id"]:
            keyboard.append([
                InlineKeyboardButton(
                    f"📢 {channel_info['name']}", 
                    callback_data=f"toggle_channel:{post_id}:{channel_key}"
                )
            ])
    keyboard.append([
        InlineKeyboardButton("✅ Опубликовать в выбранные", callback_data=f"publish_multi_now:{post_id}"),
        InlineKeyboardButton("⏰ Отложить в выбранные", callback_data=f"schedule_multi:{post_id}")
    ])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_post_preview")])
    return InlineKeyboardMarkup(keyboard)

def get_multi_schedule_keyboard(post_id: str):
    schedule_times = [
        ("Через 30 мин", "30min"),
        ("9:05", "9:05"), ("10:05", "10:05"), ("10:06", "10:06"), ("11:07", "11:07"),
        ("12:08", "12:08"), ("13:09", "13:09"), ("14:10", "14:10"), ("15:11", "15:11"),
        ("16:12", "16:12"), ("17:13", "17:13"), ("18:14", "18:14"), ("19:07", "19:07"),
        ("20:08", "20:08"), ("21:09", "21:09"), ("22:11", "22:11"), ("22:45", "22:45")
    ]
    keyboard = []
    row = []
    for i, (label, value) in enumerate(schedule_times):
        row.append(InlineKeyboardButton(label, callback_data=f"multi_schedule_time:{post_id}:{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"back_to_channel_select:{post_id}")])
    return InlineKeyboardMarkup(keyboard)

def get_post_publish_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Прислать нам новость", url=SUGGEST_LINK)]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ РЕПОСТОВ ====================
async def handle_forwarded_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.photo:
        return
    
    caption = message.caption or ""
    photo = message.photo[-1]
    
    cleaned_caption = remove_emojis(caption)
    
    print(f"📸 Получено фото")
    
    context.chat_data["pending_post"] = {
        "type": "photo",
        "text": cleaned_caption,
        "file_id": photo.file_id,
        "photo_bytes": None
    }
    
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        context.chat_data["pending_post"]["photo_bytes"] = photo_bytes
        
        await message.reply_photo(
            photo=photo.file_id,
            caption=cleaned_caption if cleaned_caption else " ",
            parse_mode="HTML",
            reply_markup=get_post_preview_keyboard()
        )
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Не удалось загрузить фото")

async def handle_forwarded_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.video:
        return
    
    caption = message.caption or ""
    video = message.video
    
    cleaned_caption = remove_emojis(caption)
    
    print(f"📹 Получено видео")
    
    context.chat_data["pending_video"] = {
        "type": "video",
        "text": cleaned_caption,
        "file_id": video.file_id
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
    await query.message.reply_text("✏️ Отправьте новый текст для поста. Или /cancel для отмены.")

async def edit_video_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["waiting_for_edit"] = "video"
    await query.message.reply_text("✏️ Отправьте новый текст для видео. Или /cancel для отмены.")

async def edit_designed_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["waiting_for_edit"] = "designed"
    await query.message.reply_text("✏️ Отправьте новый текст для поста. Или /cancel для отмены.")

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
            await update.message.reply_text("✅ Текст обновлён!", reply_markup=get_post_preview_keyboard())
    
    elif edit_type == "video":
        pending = context.chat_data.get("pending_video", {})
        if pending:
            pending["text"] = new_text
            context.chat_data["pending_video"] = pending
            await update.message.reply_text("✅ Текст обновлён!", reply_markup=get_video_keyboard())
    
    elif edit_type == "designed":
        designed = context.chat_data.get("designed_post", {})
        if designed:
            designed["text"] = new_text
            context.chat_data["designed_post"] = designed
            photo_bytes = designed.get("photo_bytes")
            if photo_bytes:
                await update.message.reply_photo(
                    photo=photo_bytes,
                    caption=f"{new_text}\n\n✅ Текст обновлён!",
                    parse_mode="HTML",
                    reply_markup=get_designed_post_keyboard()
                )
    
    context.user_data["waiting_for_edit"] = None

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_edit"] = None
    await update.message.reply_text("✅ Редактирование отменено.")

# ==================== ОБРАБОТКА ИИ ДЛЯ ФОТО ====================
async def ai_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not deepseek_client:
        await query.message.reply_text("❌ API DeepSeek не настроен.")
        return
    
    custom_request = context.user_data.get("custom_ai_request", "")
    if custom_request:
        prompt = f"""{DEEPSEEK_PROMPT}
        
        Дополнительные требования пользователя: {custom_request}
        
        Переделай новость согласно этим требованиям."""
        context.user_data["custom_ai_request"] = None
    else:
        prompt = DEEPSEEK_PROMPT
    
    pending = context.chat_data.get("pending_post", {})
    text = pending.get("text", "")
    
    if not text:
        await query.message.reply_text("❌ Нет текста для обработки")
        return
    
    await query.message.reply_text("🤖 Обрабатываю текст через DeepSeek...")
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
            max_tokens=1000
        )
        
        processed_text = response.choices[0].message.content
        
        title = ""
        body = ""
        for line in processed_text.split('\n'):
            if line.startswith("Заголовок:"):
                title = line.replace("Заголовок:", "").strip()
            elif line.startswith("Текст:"):
                body = line.replace("Текст:", "").strip()
        
        if not title and not body:
            body = processed_text
        
        if title and body:
            new_text = f"{title}\n\n{body}"
        else:
            new_text = body if body else processed_text
        
        pending["text"] = new_text
        context.chat_data["pending_post"] = pending
        
        await query.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{body[:500]}...\n\n"
            f"Выберите действие:",
            parse_mode="Markdown",
            reply_markup=get_ai_result_keyboard()
        )
        
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def ai_reprocess_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.message.reply_text(
        "📝 *Введите ваш запрос для переделки текста*\n\n"
        "Примеры:\n"
        "• Сделай заголовок броским\n"
        "• Сократи до 400 символов\n"
        "• Сделай более официальным\n\n"
        "Или /cancel для отмены.",
        parse_mode="Markdown"
    )
    
    context.user_data["waiting_for_ai_request"] = True

# ==================== ОБРАБОТКА ИИ ДЛЯ ВИДЕО ====================
async def ai_process_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not deepseek_client:
        await query.message.reply_text("❌ API DeepSeek не настроен.")
        return
    
    custom_request = context.user_data.get("custom_ai_request_video", "")
    if custom_request:
        prompt = f"""{DEEPSEEK_PROMPT}
        
        Дополнительные требования пользователя: {custom_request}
        
        Переделай новость согласно этим требованиям."""
        context.user_data["custom_ai_request_video"] = None
    else:
        prompt = DEEPSEEK_PROMPT
    
    pending = context.chat_data.get("pending_video", {})
    text = pending.get("text", "")
    
    if not text:
        await query.message.reply_text("❌ Нет текста для обработки")
        return
    
    await query.message.reply_text("🤖 Обрабатываю текст через DeepSeek...")
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
            max_tokens=1000
        )
        
        processed_text = response.choices[0].message.content
        
        title = ""
        body = ""
        for line in processed_text.split('\n'):
            if line.startswith("Заголовок:"):
                title = line.replace("Заголовок:", "").strip()
            elif line.startswith("Текст:"):
                body = line.replace("Текст:", "").strip()
        
        if not title and not body:
            body = processed_text
        
        if title and body:
            new_text = f"{title}\n\n{body}"
        else:
            new_text = body if body else processed_text
        
        pending["text"] = new_text
        context.chat_data["pending_video"] = pending
        
        await query.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{body[:500]}...\n\n"
            f"Выберите действие:",
            parse_mode="Markdown",
            reply_markup=get_video_ai_result_keyboard()
        )
        
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def ai_reprocess_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.message.reply_text(
        "📝 *Введите ваш запрос для переделки текста видео*\n\n"
        "Примеры:\n"
        "• Сделай заголовок броским\n"
        "• Сократи до 400 символов\n"
        "• Сделай более официальным\n\n"
        "Или /cancel для отмены.",
        parse_mode="Markdown"
    )
    
    context.user_data["waiting_for_ai_request_video"] = True

# ==================== ОБРАБОТЧИКИ ЗАПРОСОВ ИИ ====================
async def handle_ai_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_ai_request"):
        request = update.message.text
        context.user_data["custom_ai_request"] = request
        context.user_data["waiting_for_ai_request"] = False
        await update.message.reply_text(f"✅ Запрос: *{request}*\n🤖 Обрабатываю...", parse_mode="Markdown")
        class FakeQuery:
            def __init__(self, message):
                self.message = message
            async def answer(self):
                pass
        fake_query = FakeQuery(update.message)
        await ai_process_callback(update, context)
        return
    
    if context.user_data.get("waiting_for_ai_request_video"):
        request = update.message.text
        context.user_data["custom_ai_request_video"] = request
        context.user_data["waiting_for_ai_request_video"] = False
        await update.message.reply_text(f"✅ Запрос: *{request}*\n🤖 Обрабатываю...", parse_mode="Markdown")
        class FakeQuery:
            def __init__(self, message):
                self.message = message
            async def answer(self):
                pass
        fake_query = FakeQuery(update.message)
        await ai_process_video_callback(update, context)
        return

# ==================== ОФОРМЛЕНИЕ ПОСТА ====================
async def design_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    
    if not pending or pending.get("type") != "photo":
        await query.message.reply_text("❌ Оформить можно только фото")
        return
    
    full_text = pending.get("text", "")
    if not full_text:
        await query.message.reply_text("❌ Нет текста")
        return
    
    lines = full_text.split('\n')
    title_for_photo = lines[0][:150] if lines else "Пост"
    
    if not pending.get("photo_bytes"):
        await query.message.reply_text("❌ Нет фото")
        return
    
    try:
        await query.message.reply_text("🎨 Оформляю пост...")
        
        photo_io = process_photo(pending["photo_bytes"], title_for_photo)
        
        context.chat_data["designed_post"] = {
            "text": full_text,
            "photo_bytes": photo_io.getvalue()
        }
        
        await query.message.reply_photo(
            photo=photo_io,
            caption=f"{full_text}\n\n✅ Пост оформлен!",
            parse_mode="HTML",
            reply_markup=get_designed_post_keyboard()
        )
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"⚠️ Ошибка: {e}")

# ==================== МУЛЬТИКАНАЛЬНАЯ ПУБЛИКАЦИЯ ====================
async def start_multi_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    if not pending:
        await query.message.reply_text("❌ Нет поста для публикации")
        return
    
    post_id = str(uuid.uuid4())
    
    multi_channel_posts[post_id] = {
        "type": "photo",
        "text": pending.get("text", ""),
        "photo_bytes": pending.get("photo_bytes"),
        "file_id": pending.get("file_id"),
        "selected_channels": []
    }
    
    await query.message.reply_text(
        "🌍 *Выберите каналы для публикации*\n\n"
        "Нажмите на канал чтобы выбрать/отменить. Можно выбрать несколько каналов.\n\n"
        "После выбора нажмите кнопку публикации.",
        parse_mode="Markdown",
        reply_markup=get_channel_selection_keyboard(post_id)
    )

async def start_multi_channel_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    designed = context.chat_data.get("designed_post", {})
    if not designed:
        await query.message.reply_text("❌ Нет оформленного поста")
        return
    
    post_id = str(uuid.uuid4())
    
    multi_channel_posts[post_id] = {
        "type": "designed",
        "text": designed.get("text", ""),
        "photo_bytes": designed.get("photo_bytes"),
        "selected_channels": []
    }
    
    await query.message.reply_text(
        "🌍 *Выберите каналы для публикации*\n\n"
        "Нажмите на канал чтобы выбрать/отменить. Можно выбрать несколько каналов.\n\n"
        "После выбора нажмите кнопку публикации.",
        parse_mode="Markdown",
        reply_markup=get_channel_selection_keyboard(post_id)
    )

async def toggle_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, post_id, channel_key = query.data.split(":")
    
    post_data = multi_channel_posts.get(post_id)
    if not post_data:
        await query.message.reply_text("❌ Пост не найден")
        return
    
    selected = post_data["selected_channels"]
    
    if channel_key in selected:
        selected.remove(channel_key)
        status = "❌ отменен"
    else:
        if CHANNELS.get(channel_key, {}).get("channel_id"):
            selected.append(channel_key)
            status = "✅ выбран"
        else:
            await query.answer("⚠️ Этот канал не настроен!", show_alert=True)
            return
    
    channels_text = "\n".join([f"• {CHANNELS[ch]['name']}" for ch in selected]) if selected else "❌ Ничего не выбрано"
    
    await query.message.edit_text(
        f"🌍 *Выберите каналы для публикации*\n\n"
        f"*Выбрано:*\n{channels_text}\n\n"
        f"Нажмите на канал чтобы выбрать/отменить.\n\n"
        f"📢 {CHANNELS[channel_key]['name']} — {status}",
        parse_mode="Markdown",
        reply_markup=get_channel_selection_keyboard(post_id)
    )

async def publish_multi_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    post_id = query.data.split(":")[1]
    
    post_data = multi_channel_posts.get(post_id)
    if not post_data:
        await query.message.reply_text("❌ Пост не найден")
        return
    
    selected_channels = post_data.get("selected_channels", [])
    if not selected_channels:
        await query.message.reply_text("❌ Выберите хотя бы один канал")
        return
    
    await query.message.edit_text("⏳ Публикую в выбранные каналы...")
    
    success_count = 0
    errors = []
    
    for channel_key in selected_channels:
        channel_info = CHANNELS.get(channel_key)
        if not channel_info or not channel_info["channel_id"]:
            errors.append(f"{channel_info['name'] if channel_info else channel_key}: канал не настроен")
            continue
        
        try:
            text = post_data.get("text", "")
            if len(text) > 1000:
                text = text[:1000] + "..."
            
            lines = text.split('\n')
            title = lines[0] if lines else ""
            body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
            caption = format_caption(title, body)
            
            if post_data.get("type") == "designed" or post_data.get("photo_bytes"):
                photo_bytes = post_data.get("photo_bytes")
                if photo_bytes:
                    await context.bot.send_photo(
                        chat_id=channel_info["channel_id"],
                        photo=photo_bytes,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
                else:
                    await context.bot.send_message(
                        chat_id=channel_info["channel_id"],
                        text=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
            else:
                file_id = post_data.get("file_id")
                if file_id:
                    await context.bot.send_photo(
                        chat_id=channel_info["channel_id"],
                        photo=file_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
                else:
                    await context.bot.send_message(
                        chat_id=channel_info["channel_id"],
                        text=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
            
            success_count += 1
            print(f"✅ Опубликовано в {channel_info['name']}")
            
        except Exception as e:
            error_msg = f"{channel_info['name']}: {str(e)[:50]}"
            errors.append(error_msg)
            print(f"❌ Ошибка при публикации в {channel_info['name']}: {e}")
    
    report = f"✅ *Результат публикации*\n\n📊 Успешно: {success_count}/{len(selected_channels)}"
    if errors:
        report += f"\n\n❌ Ошибки:\n" + "\n".join(f"• {err}" for err in errors)
    
    await query.message.edit_text(report, parse_mode="Markdown")
    multi_channel_posts.pop(post_id, None)
    
    await asyncio.sleep(5)
    try:
        await query.message.delete()
    except:
        pass

async def schedule_multi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    post_id = query.data.split(":")[1]
    
    post_data = multi_channel_posts.get(post_id)
    if not post_data:
        await query.message.reply_text("❌ Пост не найден")
        return
    
    selected_channels = post_data.get("selected_channels", [])
    if not selected_channels:
        await query.message.reply_text("❌ Выберите хотя бы один канал")
        return
    
    channels_text = "\n".join([f"• {CHANNELS[ch]['name']}" for ch in selected_channels])
    
    await query.message.edit_text(
        f"⏰ *Отложенная публикация*\n\n"
        f"Будет опубликовано в:\n{channels_text}\n\n"
        f"Выберите время:",
        parse_mode="Markdown",
        reply_markup=get_multi_schedule_keyboard(post_id)
    )

async def multi_schedule_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, post_id, time_value = query.data.split(":")
    
    post_data = multi_channel_posts.get(post_id)
    if not post_data:
        await query.message.reply_text("❌ Пост не найден")
        return
    
    selected_channels = post_data.get("selected_channels", [])
    if not selected_channels:
        await query.message.reply_text("❌ Выберите хотя бы один канал")
        return
    
    now = datetime.now()
    if time_value == "30min":
        publish_time = now + timedelta(minutes=30)
        time_str = "через 30 минут"
    else:
        hour, minute = map(int, time_value.split(":"))
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)
        time_str = f"{publish_time.strftime('%H:%M')} ({publish_time.strftime('%d.%m')})"
    
    text = post_data.get("text", "")
    photo_bytes = post_data.get("photo_bytes")
    
    if not photo_bytes:
        file_id = post_data.get("file_id")
        if file_id:
            try:
                file = await context.bot.get_file(file_id)
                photo_bytes = await file.download_as_bytearray()
            except Exception as e:
                await query.message.reply_text(f"❌ Не удалось загрузить фото: {e}")
                return
    
    save_scheduled_multi_post(text, photo_bytes, selected_channels, publish_time)
    
    channels_text = "\n".join([f"• {CHANNELS[ch]['name']}" for ch in selected_channels])
    
    await query.message.edit_text(
        f"✅ *Пост запланирован!*\n\n"
        f"📅 Время: {time_str}\n"
        f"📢 Каналы:\n{channels_text}\n\n"
        f"Пост будет автоматически опубликован в указанные каналы.",
        parse_mode="Markdown"
    )
    
    multi_channel_posts.pop(post_id, None)
    
    await asyncio.sleep(5)
    try:
        await query.message.delete()
    except:
        pass

async def back_to_channel_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, post_id = query.data.split(":")
    
    post_data = multi_channel_posts.get(post_id)
    if not post_data:
        await query.message.reply_text("❌ Пост не найден")
        return
    
    selected = post_data.get("selected_channels", [])
    channels_text = "\n".join([f"• {CHANNELS[ch]['name']}" for ch in selected]) if selected else "❌ Ничего не выбрано"
    
    await query.message.edit_text(
        f"🌍 *Выберите каналы для публикации*\n\n"
        f"*Выбрано:*\n{channels_text}\n\n"
        f"Нажмите на канал чтобы выбрать/отменить. Можно выбрать несколько каналов.\n\n"
        f"После выбора нажмите кнопку публикации.",
        parse_mode="Markdown",
        reply_markup=get_channel_selection_keyboard(post_id)
    )

async def back_to_post_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    text = pending.get("text", "")
    
    await query.message.edit_caption(
        caption=text if text else " ",
        parse_mode="HTML",
        reply_markup=get_post_preview_keyboard()
    )

# ==================== ОТЛОЖЕННАЯ ПУБЛИКАЦИЯ ====================
async def schedule_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=get_schedule_keyboard())

async def back_to_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    text = pending.get("text", "")
    
    await query.message.edit_caption(
        caption=text if text else " ",
        parse_mode="HTML",
        reply_markup=get_post_preview_keyboard()
    )

async def back_to_video_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_video", {})
    text = pending.get("text", "")
    
    await query.message.edit_caption(
        caption=text if text else " ",
        parse_mode="HTML",
        reply_markup=get_video_keyboard()
    )

async def schedule_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    time_value = query.data.split(":")[1]
    
    now = datetime.now()
    if time_value == "30min":
        publish_time = now + timedelta(minutes=30)
        time_str = "через 30 минут"
    else:
        hour, minute = map(int, time_value.split(":"))
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)
        time_str = f"{publish_time.strftime('%H:%M')} ({publish_time.strftime('%d.%m')})"
    
    pending = context.chat_data.get("pending_post", {})
    full_text = pending.get("text", "")
    photo_bytes = pending.get("photo_bytes")
    
    if not photo_bytes:
        await query.message.reply_text("❌ Нет данных для отложенной публикации")
        return
    
    save_scheduled_post(full_text, photo_bytes, publish_time)
    
    await query.message.reply_text(
        f"✅ Пост запланирован на {time_str}\n\n"
        f"Он будет автоматически опубликован в канал в указанное время."
    )
    
    context.chat_data.pop("pending_post", None)
    
    try:
        await query.message.delete()
    except:
        pass

async def schedule_video_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=get_video_schedule_keyboard())

async def schedule_video_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    time_value = query.data.split(":")[1]
    
    now = datetime.now()
    if time_value == "30min":
        publish_time = now + timedelta(minutes=30)
        time_str = "через 30 минут"
    else:
        hour, minute = map(int, time_value.split(":"))
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)
        time_str = f"{publish_time.strftime('%H:%M')} ({publish_time.strftime('%d.%m')})"
    
    pending = context.chat_data.get("pending_video", {})
    text = pending.get("text", "")
    file_id = pending.get("file_id")
    
    if not file_id:
        await query.message.reply_text("❌ Нет данных для отложенной публикации")
        return
    
    save_scheduled_video(text, file_id, publish_time)
    
    await query.message.reply_text(
        f"✅ Видео запланировано на {time_str}\n\n"
        f"Оно будет автоматически опубликовано в канал в указанное время."
    )
    
    context.chat_data.pop("pending_video", None)
    
    try:
        await query.message.delete()
    except:
        pass

async def schedule_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=get_schedule_keyboard())
    context.user_data["scheduling_designed"] = True

async def schedule_designed_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    time_value = query.data.split(":")[1]
    
    now = datetime.now()
    if time_value == "30min":
        publish_time = now + timedelta(minutes=30)
        time_str = "через 30 минут"
    else:
        hour, minute = map(int, time_value.split(":"))
        publish_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_time <= now:
            publish_time += timedelta(days=1)
        time_str = f"{publish_time.strftime('%H:%M')} ({publish_time.strftime('%d.%m')})"
    
    designed = context.chat_data.get("designed_post", {})
    full_text = designed.get("text", "")
    photo_bytes = designed.get("photo_bytes")
    
    if not photo_bytes:
        await query.message.reply_text("❌ Нет данных для отложенной публикации")
        return
    
    save_scheduled_post(full_text, photo_bytes, publish_time)
    
    await query.message.reply_text(
        f"✅ Оформленный пост запланирован на {time_str}\n\n"
        f"Он будет автоматически опубликован в канал в указанное время."
    )
    
    context.chat_data.pop("designed_post", None)
    context.user_data["scheduling_designed"] = False
    
    try:
        await query.message.delete()
    except:
        pass

# ==================== ПУБЛИКАЦИЯ ====================
async def publish_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    designed = context.chat_data.get("designed_post", {})
    
    if not designed:
        await query.message.reply_text("❌ Нет оформленного поста")
        return
    
    full_text = designed.get("text", "")
    photo_bytes = designed.get("photo_bytes")
    
    if not photo_bytes:
        await query.message.reply_text("❌ Нет фото")
        return
    
    try:
        if len(full_text) > 1000:
            full_text = full_text[:1000] + "..."
        
        lines = full_text.split('\n')
        title = lines[0] if lines else ""
        body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
        caption = format_caption(title, body)
        
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_bytes,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_post_publish_keyboard()
        )
        
        await query.message.reply_text("✅ Пост опубликован в канал!")
        
        context.chat_data.pop("pending_post", None)
        context.chat_data.pop("designed_post", None)
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_raw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_post", {})
    
    if not pending or pending.get("type") != "photo":
        await query.message.reply_text("❌ Нет поста для публикации")
        return
    
    full_text = pending.get("text", "")
    file_id = pending.get("file_id")
    
    if not file_id:
        await query.message.reply_text("❌ Нет file_id фото")
        return
    
    try:
        if len(full_text) > 1000:
            full_text = full_text[:1000] + "..."
        
        lines = full_text.split('\n')
        title = lines[0] if lines else ""
        body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
        
        if body and body.strip():
            caption = f"<b>{title}</b>\n{body}"
        else:
            caption = f"<b>{title}</b>"
        
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_post_publish_keyboard()
        )
        
        await query.message.reply_text("✅ Пост опубликован в канал!")
        
        context.chat_data.pop("pending_post", None)
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pending = context.chat_data.get("pending_video", {})
    
    if not pending or pending.get("type") != "video":
        await query.message.reply_text("❌ Нет видео")
        return
    
    text = pending.get("text", "")
    file_id = pending.get("file_id")
    
    if not file_id:
        await query.message.reply_text("❌ Нет file_id")
        return
    
    try:
        if len(text) > 1000:
            text = text[:1000] + "..."
        
        if text:
            lines = text.split('\n')
            title = lines[0] if lines else ""
            body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
            caption = format_caption(title, body)
        else:
            caption = " "
        
        await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_post_publish_keyboard()
        )
        
        await query.message.reply_text("✅ Видео опубликовано!")
        context.chat_data.pop("pending_video", None)
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, news_id: str):
    news = pending_news.get(news_id)
    if not news:
        return
    
    try:
        news_text = news['text']
        if len(news_text) > 1000:
            news_text = news_text[:1000] + "..."
        
        caption = format_caption(news['title'], f"{news_text}\n\n🔗 {news['url']}")
        
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
        print(f"❌ Ошибка: {e}")

# ==================== ПЛАНИРОВЩИК ====================
async def check_scheduled_posts(app: Application):
    while True:
        try:
            posts = get_pending_scheduled_posts()
            for post in posts:
                photo_bytes = post["photo_bytes"]
                text = post["text"]
                
                if len(text) > 1000:
                    text = text[:1000] + "..."
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                await app.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=photo_bytes,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=get_post_publish_keyboard()
                )
                
                delete_scheduled_post(post["id"])
                print(f"✅ Опубликован отложенный фото-пост")
            
            videos = get_pending_scheduled_videos()
            for video in videos:
                text = video["text"]
                file_id = video["file_id"]
                
                if len(text) > 1000:
                    text = text[:1000] + "..."
                
                if text:
                    lines = text.split('\n')
                    title = lines[0] if lines else ""
                    body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                    caption = format_caption(title, body)
                else:
                    caption = " "
                
                await app.bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=get_post_publish_keyboard()
                )
                
                delete_scheduled_video(video["id"])
                print(f"✅ Опубликовано отложенное видео")
            
            multi_posts = get_pending_scheduled_multi_posts()
            for post in multi_posts:
                photo_bytes = post["photo_bytes"]
                text = post["text"]
                channels = post["channels"]
                
                if len(text) > 1000:
                    text = text[:1000] + "..."
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                success_count = 0
                for channel_key in channels:
                    channel_info = CHANNELS.get(channel_key)
                    if not channel_info or not channel_info["channel_id"]:
                        print(f"❌ Канал {channel_key} не настроен")
                        continue
                    
                    try:
                        await app.bot.send_photo(
                            chat_id=channel_info["channel_id"],
                            photo=photo_bytes,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=get_post_publish_keyboard()
                        )
                        success_count += 1
                        print(f"✅ Опубликован отложенный пост в {channel_info['name']}")
                    except Exception as e:
                        print(f"❌ Ошибка публикации в {channel_info['name']}: {e}")
                
                print(f"📊 Мультиканальный пост: успешно {success_count}/{len(channels)}")
                delete_scheduled_multi_post(post["id"])
                
        except Exception as e:
            print(f"❌ Ошибка в планировщике: {e}")
        
        await asyncio.sleep(60)

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот новостей Гродно*\n\n"
        "📰 *Парсинг новостей* — нажми кнопку\n"
        "🖼️ *Фото* — отправьте фото с подписью\n"
        "📹 *Видео* — отправьте видео с подписью\n\n"
        "*Доступные действия:*\n"
        "• 🎨 Оформить пост\n"
        "• ✏️ Редактировать текст\n"
        "• 🤖 Обработать текст (ИИ)\n"
        "• 📤 Опубликовать без оформления\n"
        "• 🌍 Опубликовать в несколько каналов\n"
        "• ⏰ Отложить публикацию\n\n"
        "👇 Нажми кнопку",
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
            await query.message.reply_text("❌ Не удалось загрузить новости", reply_markup=get_main_keyboard())
            return
        
        pending_news.clear()
        
        for i, item in enumerate(news_items):
            if is_already_published(item['url']):
                continue
            
            image_url = await fetch_article_image(item['url'])
            article_text = await fetch_article_text(item['url'])
            
            news_id = f"{i}_{abs(hash(item['url']))}"
            
            processed_photo = None
            if image_url:
                try:
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
            
            await asyncio.sleep(0.3)
        
        await query.message.reply_text("✅ Готово!", reply_markup=get_main_keyboard())
    
    elif data.startswith("publish:"):
        news_id = data.split(":")[1]
        await publish_news_callback(update, context, news_id)
        await query.message.reply_text("✅ Опубликовано!", reply_markup=get_post_publish_keyboard())
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
    
    elif data == "publish_raw":
        await publish_raw_callback(update, context)
    
    elif data == "edit_text":
        await edit_text_callback(update, context)
    
    elif data == "edit_video_text":
        await edit_video_text_callback(update, context)
    
    elif data == "edit_designed_text":
        await edit_designed_text_callback(update, context)
    
    elif data == "schedule_menu":
        await schedule_menu_callback(update, context)
    
    elif data == "schedule_video_menu":
        await schedule_video_menu_callback(update, context)
    
    elif data == "back_to_preview":
        await back_to_preview_callback(update, context)
    
    elif data == "back_to_video_preview":
        await back_to_video_preview_callback(update, context)
    
    elif data == "ai_process":
        await ai_process_callback(update, context)
    
    elif data == "ai_process_video":
        await ai_process_video_callback(update, context)
    
    elif data == "ai_reprocess":
        await ai_reprocess_callback(update, context)
    
    elif data == "ai_reprocess_video":
        await ai_reprocess_video_callback(update, context)
    
    elif data == "start_multi_channel":
        await start_multi_channel_callback(update, context)
    
    elif data == "start_multi_channel_designed":
        await start_multi_channel_designed_callback(update, context)
    
    elif data.startswith("toggle_channel:"):
        await toggle_channel_callback(update, context)
    
    elif data.startswith("publish_multi_now:"):
        await publish_multi_now_callback(update, context)
    
    elif data.startswith("schedule_multi:"):
        await schedule_multi_callback(update, context)
    
    elif data.startswith("multi_schedule_time:"):
        await multi_schedule_time_callback(update, context)
    
    elif data.startswith("back_to_channel_select:"):
        await back_to_channel_select_callback(update, context)
    
    elif data == "back_to_post_preview":
        await back_to_post_preview_callback(update, context)
    
    elif data.startswith("schedule:"):
        if context.user_data.get("scheduling_designed"):
            await schedule_designed_time_callback(update, context)
        else:
            await schedule_post_callback(update, context)
    
    elif data.startswith("schedule_video:"):
        await schedule_video_post_callback(update, context)
    
    elif data == "schedule_designed":
        await schedule_designed_callback(update, context)

# ==================== ВЕБ-СЕРВЕР ====================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Grodno News Bot with Multi-Channel Support"}

@app.get("/health")
async def health():
    return {"status": "alive"}

# ==================== ЗАПУСК ====================
async def run_bot():
    init_db()
    
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook()
    print("✅ Webhook удалён")
    
    if deepseek_client:
        print("✅ DeepSeek API подключен")
    else:
        print("⚠️ DeepSeek API не настроен")
    
    # Проверяем настроенные каналы
    active_channels = [k for k, v in CHANNELS.items() if v["channel_id"]]
    print(f"✅ Активных каналов: {len(active_channels)}")
    for channel_key in active_channels:
        print(f"   • {CHANNELS[channel_key]['name']}")
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_edit))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_forwarded_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_forwarded_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_request))
    
    await application.initialize()
    await application.start()
    
    asyncio.create_task(check_scheduled_posts(application))
    
    await application.updater.start_polling()
    
    print("✅ Бот запущен с поддержкой мультиканальной публикации и ИИ!")

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
