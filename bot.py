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
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
user_sessions: Dict[int, Dict] = {}

# Промпт для DeepSeek - сохраняем объем текста, добавляем абзацы
DEEPSEEK_PROMPT = """Ты редактор новостного сайта. Твоя задача - отформатировать текст новости, практически не меняя его содержание.

Правила:
1. НЕ УКОРАЧИВАЙ текст - сохрани его оригинальный объем
2. Удали только смайлики, если они есть
3. ОБЯЗАТЕЛЬНО расставь абзацы - разбей текст на логические части (3-5 предложений в абзаце)
4. Заголовок сделай чуть более интересным, но не меняй смысл
5. Сохрани все важные детали, даты, цифры, имена

Верни только готовую новость в формате:
Заголовок: (заголовок новости)

Текст: (текст новости с абзацами, оригинального объема)"""

# ==================== ФУНКЦИЯ START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот для публикации новостей*\n\n"
        "📸 *Отправьте мне фото с подписью* - я помогу оформить и опубликовать\n"
        "📹 *Отправьте видео с подписью* - опубликую в канал\n"
        "📰 *Нажмите кнопку \"Начать парсинг\"* - получу свежие новости\n\n"
        "*Доступные функции:*\n"
        "• 🎨 Оформление постов с текстом на фото\n"
        "• ✏️ Редактирование текста\n"
        "• 🤖 Обработка текста через ИИ (DeepSeek)\n"
        "• 🌍 Публикация в несколько каналов\n"
        "• ⏰ Отложенная публикация\n\n"
        "👇 *Нажмите кнопку*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
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
    title = remove_emojis(title) if title else ""
    body = remove_emojis(body) if body else ""
    
    if not title and not body:
        return ""
    
    if title and not body:
        return f"<b>{title}</b>"
    
    if not title and body:
        return body
    
    return f"<b>{title}</b>\n{body}"

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
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
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
                video_bytes BLOB,
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

def save_scheduled_video(text: str, video_bytes: bytes, schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_videos (text, video_bytes, schedule_time, created_at) VALUES (?, ?, ?, ?)",
            (text, video_bytes, schedule_time, datetime.now())
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
            "SELECT id, text, video_bytes, schedule_time FROM scheduled_videos WHERE schedule_time <= ?",
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

async def fetch_article_image(url: str) -> Optional[bytes]:
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
            async with httpx.AsyncClient() as client:
                resp = await client.get(image_url)
                if resp.status_code == 200:
                    return resp.content
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

# ==================== КНОПКИ ====================
def get_main_keyboard():
    keyboard = [[InlineKeyboardButton("📰 Начать парсинг (10 новостей)", callback_data="start_parsing")]]
    return InlineKeyboardMarkup(keyboard)

def get_news_keyboard(news_id: str):
    keyboard = [[
        InlineKeyboardButton("✅ Опубликовать в канал", callback_data=f"publish_news:{news_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_news:{news_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_post_preview_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_text")],
        [InlineKeyboardButton("🤖 Обработать текст (ИИ)", callback_data="ai_process")],
        [InlineKeyboardButton("📤 Опубликовать без оформления", callback_data="publish_raw")],
        [InlineKeyboardButton("🌍 Опубликовать во все каналы", callback_data="publish_to_all_channels")],
        [InlineKeyboardButton("🎯 Выбрать канал", callback_data="select_channel_menu")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_video_keyboard():
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_video_text")],
        [InlineKeyboardButton("🤖 Обработать текст (ИИ)", callback_data="ai_process_video")],
        [InlineKeyboardButton("📹 Опубликовать видео", callback_data="publish_video")],
        [InlineKeyboardButton("🌍 Опубликовать во все каналы", callback_data="publish_video_to_all_channels")],
        [InlineKeyboardButton("🎯 Выбрать канал", callback_data="select_channel_menu_video")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_video_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_designed_post_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать в основной канал", callback_data="publish_designed")],
        [InlineKeyboardButton("🌍 Опубликовать во все каналы", callback_data="publish_to_all_channels")],
        [InlineKeyboardButton("🎯 Выбрать канал", callback_data="select_channel_menu_designed")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_designed_text")],
        [InlineKeyboardButton("⏰ Отложить публикацию", callback_data="schedule_designed")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_ai_result_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать в основной канал", callback_data="publish_raw")],
        [InlineKeyboardButton("🌍 Опубликовать во все каналы", callback_data="publish_to_all_channels")],
        [InlineKeyboardButton("🎯 Выбрать канал", callback_data="select_channel_menu_ai")],
        [InlineKeyboardButton("🎨 Оформить пост", callback_data="design_post")],
        [InlineKeyboardButton("🔄 Переделать текст", callback_data="ai_reprocess")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_preview")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_video_ai_result_keyboard():
    keyboard = [
        [InlineKeyboardButton("📹 Опубликовать видео", callback_data="publish_video")],
        [InlineKeyboardButton("🌍 Опубликовать во все каналы", callback_data="publish_video_to_all_channels")],
        [InlineKeyboardButton("🎯 Выбрать канал", callback_data="select_channel_menu_video")],
        [InlineKeyboardButton("✏️ Редактировать вручную", callback_data="edit_video_text")],
        [InlineKeyboardButton("🔄 Переделать текст", callback_data="ai_reprocess_video")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_video_preview")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_schedule_keyboard():
    schedule_times = [
        ("Через 30 мин", "30min"),
        ("9:05", "9:05"), ("10:05", "10:05"), ("11:05", "11:05"),
        ("12:05", "12:05"), ("13:05", "13:05"), ("14:05", "14:05"),
        ("15:05", "15:05"), ("16:05", "16:05"), ("17:05", "17:05"),
        ("18:05", "18:05"), ("19:05", "19:05"), ("20:05", "20:05"),
        ("21:05", "21:05"), ("22:05", "22:05")
    ]
    keyboard = []
    row = []
    for i, (label, value) in enumerate(schedule_times):
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_time:{value}"))
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
        ("9:05", "9:05"), ("10:05", "10:05"), ("11:05", "11:05"),
        ("12:05", "12:05"), ("13:05", "13:05"), ("14:05", "14:05"),
        ("15:05", "15:05"), ("16:05", "16:05"), ("17:05", "17:05"),
        ("18:05", "18:05"), ("19:05", "19:05"), ("20:05", "20:05"),
        ("21:05", "21:05"), ("22:05", "22:05")
    ]
    keyboard = []
    row = []
    for i, (label, value) in enumerate(schedule_times):
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_video_time:{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_video_preview")])
    return InlineKeyboardMarkup(keyboard)

def get_post_publish_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Прислать нам новость", url=SUGGEST_LINK)]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ ПОЛУЧЕНИЯ МЕДИА ====================
# ==================== ОБРАБОТЧИКИ ПОЛУЧЕНИЯ МЕДИА (ИСПРАВЛЕННЫЕ) ====================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    
    if not message.photo:
        return
    
    caption = message.caption or ""
    photo = message.photo[-1]
    
    print(f"📸 Получено фото от {user_id}")
    
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        
        user_sessions[user_id] = {
            "type": "photo",
            "text": remove_emojis(caption),
            "photo_bytes": photo_bytes,
            "video_bytes": None
        }
        
        # Отправляем фото без подписи (или с короткой подписью)
        await message.reply_photo(
            photo=photo.file_id,
            caption="✅ Пост получен!" if len(caption) > 900 else (f"✅ Пост получен!\n\n{caption}" if caption else "✅ Пост получен!"),
            reply_markup=get_post_preview_keyboard()
        )
        
        # Если текст был длинным, отправляем его отдельным сообщением
        if len(caption) > 900:
            await message.reply_text(
                f"📝 *Текст поста:*\n\n{caption}",
                parse_mode="Markdown",
                reply_markup=get_post_preview_keyboard()
            )
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Ошибка загрузки фото: {e}")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    
    if not message.video:
        return
    
    caption = message.caption or ""
    video = message.video
    
    print(f"📹 Получено видео от {user_id}")
    
    try:
        file = await context.bot.get_file(video.file_id)
        video_bytes = await file.download_as_bytearray()
        
        user_sessions[user_id] = {
            "type": "video",
            "text": remove_emojis(caption),
            "photo_bytes": None,
            "video_bytes": video_bytes
        }
        
        # Отправляем видео без подписи (или с короткой подписью)
        await message.reply_video(
            video=video.file_id,
            caption="✅ Видео получено!" if len(caption) > 900 else (f"✅ Видео получено!\n\n{caption}" if caption else "✅ Видео получено!"),
            reply_markup=get_video_keyboard()
        )
        
        # Если текст был длинным, отправляем его отдельным сообщением
        if len(caption) > 900:
            await message.reply_text(
                f"📝 *Текст видео:*\n\n{caption}",
                parse_mode="Markdown",
                reply_markup=get_video_keyboard()
            )
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Ошибка загрузки видео: {e}")

# ==================== РЕДАКТИРОВАНИЕ ТЕКСТА ====================
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
    user_id = update.message.from_user.id
    edit_type = context.user_data.get("waiting_for_edit")
    
    if not edit_type:
        return
    
    new_text = update.message.text
    session = user_sessions.get(user_id)
    
    if session:
        session["text"] = new_text
        
        if edit_type == "photo":
            await update.message.reply_text(
                f"✅ Текст обновлён!\n\n{new_text}",
                reply_markup=get_post_preview_keyboard()
            )
            # Если есть фото, показываем его отдельно
            if session.get("photo_bytes"):
                await update.message.reply_photo(
                    photo=InputFile(io.BytesIO(session["photo_bytes"]), filename="post.jpg"),
                    caption="🖼️ *Фото к посту*",
                    parse_mode="Markdown"
                )
        elif edit_type == "video":
            await update.message.reply_text(
                f"✅ Текст обновлён!\n\n{new_text}",
                reply_markup=get_video_keyboard()
            )
            if session.get("video_bytes"):
                await update.message.reply_video(
                    video=InputFile(io.BytesIO(session["video_bytes"]), filename="video.mp4"),
                    caption="🎬 *Видео к посту*",
                    parse_mode="Markdown"
                )
        elif edit_type == "designed":
            if session.get("photo_bytes"):
                await update.message.reply_photo(
                    photo=InputFile(io.BytesIO(session["photo_bytes"]), filename="post.jpg"),
                    caption=f"{new_text}\n\n✅ Текст обновлён!",
                    reply_markup=get_designed_post_keyboard()
                )
            else:
                await update.message.reply_text(
                    f"{new_text}\n\n✅ Текст обновлён!",
                    reply_markup=get_designed_post_keyboard()
                )
    
    context.user_data["waiting_for_edit"] = None

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_edit"] = None
    await update.message.reply_text("✅ Редактирование отменено.")

# ==================== ОФОРМЛЕНИЕ ПОСТА ====================
async def design_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "photo":
        await query.message.reply_text("❌ Оформить можно только фото")
        return
    
    full_text = session.get("text", "")
    if not full_text:
        await query.message.reply_text("❌ Нет текста")
        return
    
    lines = full_text.split('\n')
    title_for_photo = lines[0][:150] if lines else "Пост"
    
    if not session.get("photo_bytes"):
        await query.message.reply_text("❌ Нет фото")
        return
    
    try:
        await query.message.reply_text("🎨 Оформляю пост...")
        
        photo_io = process_photo(session["photo_bytes"], title_for_photo)
        processed_bytes = photo_io.getvalue()
        
        session["photo_bytes"] = processed_bytes
        session["designed"] = True
        
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(processed_bytes), filename="post.jpg"),
            caption=f"{full_text}\n\n✅ Пост оформлен!",
            reply_markup=get_designed_post_keyboard()
        )
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"⚠️ Ошибка оформления: {e}")

# ==================== ОБРАБОТКА ИИ ====================
async def ai_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not deepseek_client:
        await query.message.reply_text("❌ API DeepSeek не настроен.")
        return
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    text = session.get("text", "")
    if not text:
        await query.message.reply_text("❌ Нет текста для обработки")
        return
    
    await query.message.reply_text("🤖 Обрабатываю текст через DeepSeek...")
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": DEEPSEEK_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        processed_text = response.choices[0].message.content
        
        title = ""
        body = ""
        for line in processed_text.split('\n'):
            if line.startswith("Заголовок:"):
                title = line.replace("Заголовок:", "").strip()
            elif line.startswith("Текст:"):
                body = line.replace("Текст:", "").strip()
            elif line.startswith("**Заголовок:**"):
                title = line.replace("**Заголовок:**", "").strip()
            elif line.startswith("**Текст:**"):
                body = line.replace("**Текст:**", "").strip()
        
        if not title and not body:
            # Если формат не соблюден, пробуем извлечь
            parts = processed_text.split('\n\n', 1)
            if len(parts) == 2:
                title = parts[0].strip()
                body = parts[1].strip()
            else:
                body = processed_text
        
        new_text = f"{title}\n\n{body}" if title and body else (body if body else processed_text)
        session["text"] = new_text
        
        # Показываем весь текст полностью
        await query.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{body}\n\n"
            f"Выберите действие:",
            parse_mode="Markdown",
            reply_markup=get_ai_result_keyboard()
        )
        
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def ai_process_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not deepseek_client:
        await query.message.reply_text("❌ API DeepSeek не настроен.")
        return
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    text = session.get("text", "")
    if not text:
        await query.message.reply_text("❌ Нет текста для обработки")
        return
    
    await query.message.reply_text("🤖 Обрабатываю текст через DeepSeek...")
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": DEEPSEEK_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        processed_text = response.choices[0].message.content
        
        title = ""
        body = ""
        for line in processed_text.split('\n'):
            if line.startswith("Заголовок:"):
                title = line.replace("Заголовок:", "").strip()
            elif line.startswith("Текст:"):
                body = line.replace("Текст:", "").strip()
            elif line.startswith("**Заголовок:**"):
                title = line.replace("**Заголовок:**", "").strip()
            elif line.startswith("**Текст:**"):
                body = line.replace("**Текст:**", "").strip()
        
        if not title and not body:
            parts = processed_text.split('\n\n', 1)
            if len(parts) == 2:
                title = parts[0].strip()
                body = parts[1].strip()
            else:
                body = processed_text
        
        new_text = f"{title}\n\n{body}" if title and body else (body if body else processed_text)
        session["text"] = new_text
        
        await query.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{body}\n\n"
            f"Выберите действие:",
            parse_mode="Markdown",
            reply_markup=get_video_ai_result_keyboard()
        )
        
        try:
            await query.message.delete()
        except:
            pass
        
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

async def handle_ai_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for_ai_request"):
        request = update.message.text
        context.user_data["custom_ai_request"] = request
        context.user_data["waiting_for_ai_request"] = False
        await update.message.reply_text(f"✅ Запрос: *{request}*\n🤖 Обрабатываю...", parse_mode="Markdown")
        await ai_process_callback(update, context)
        return
    
    if context.user_data.get("waiting_for_ai_request_video"):
        request = update.message.text
        context.user_data["custom_ai_request_video"] = request
        context.user_data["waiting_for_ai_request_video"] = False
        await update.message.reply_text(f"✅ Запрос: *{request}*\n🤖 Обрабатываю...", parse_mode="Markdown")
        await ai_process_video_callback(update, context)
        return

# ==================== ПУБЛИКАЦИЯ ====================
async def publish_to_channel(bot, channel_id, text, photo_bytes, video_bytes=None, is_video=False):
    if len(text) > 1000:
        text = text[:1000] + "..."
    
    lines = text.split('\n')
    title = lines[0] if lines else ""
    body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
    caption = format_caption(title, body)
    
    if is_video and video_bytes:
        if caption:
            return await bot.send_video(
                chat_id=channel_id,
                video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                caption=caption,
                parse_mode="HTML"
            )
        else:
            return await bot.send_video(
                chat_id=channel_id,
                video=InputFile(io.BytesIO(video_bytes), filename="video.mp4")
            )
    elif photo_bytes:
        if caption:
            return await bot.send_photo(
                chat_id=channel_id,
                photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                caption=caption,
                parse_mode="HTML"
            )
        else:
            return await bot.send_photo(
                chat_id=channel_id,
                photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg")
            )
    else:
        return await bot.send_message(
            chat_id=channel_id,
            text=caption if caption else "."
        )

async def publish_raw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "photo":
        await query.message.reply_text("❌ Нет поста для публикации")
        return
    
    await query.message.reply_text("⏳ Публикую в основной канал...")
    
    try:
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await publish_to_channel(
            context.bot,
            CHANNEL_ID,
            text,
            photo_bytes,
            None,
            False
        )
        
        await query.message.reply_text("✅ Пост опубликован в основной канал!")
        user_sessions.pop(user_id, None)
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def publish_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет оформленного поста")
        return
    
    await query.message.reply_text("⏳ Публикую в основной канал...")
    
    try:
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await publish_to_channel(
            context.bot,
            CHANNEL_ID,
            text,
            photo_bytes,
            None,
            False
        )
        
        await query.message.reply_text("✅ Пост опубликован в основной канал!")
        user_sessions.pop(user_id, None)
        
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
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "video":
        await query.message.reply_text("❌ Нет видео")
        return
    
    await query.message.reply_text("⏳ Публикую видео...")
    
    try:
        text = session.get("text", "")
        video_bytes = session.get("video_bytes")
        
        await publish_to_channel(
            context.bot,
            CHANNEL_ID,
            text,
            None,
            video_bytes,
            True
        )
        
        await query.message.reply_text("✅ Видео опубликовано!")
        user_sessions.pop(user_id, None)
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

# ==================== ПУБЛИКАЦИЯ ВО ВСЕ КАНАЛЫ ====================
async def publish_to_all_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    active_channels = [(k, v) for k, v in CHANNELS.items() if v["channel_id"]]
    
    if not active_channels:
        await query.message.reply_text("❌ Нет настроенных каналов")
        return
    
    await query.message.reply_text(f"⏳ Публикую во все {len(active_channels)} каналов...")
    
    text = session.get("text", "")
    photo_bytes = session.get("photo_bytes")
    video_bytes = session.get("video_bytes")
    is_video = session.get("type") == "video"
    
    success = 0
    errors = []
    
    for channel_key, channel_info in active_channels:
        try:
            await publish_to_channel(
                context.bot,
                channel_info["channel_id"],
                text,
                photo_bytes,
                video_bytes,
                is_video
            )
            success += 1
            print(f"✅ Опубликовано в {channel_info['name']}")
        except Exception as e:
            errors.append(f"{channel_info['name']}: {str(e)[:30]}")
            print(f"❌ Ошибка в {channel_info['name']}: {e}")
    
    report = f"✅ *Результат публикации*\n\n📊 Успешно: {success}/{len(active_channels)}"
    if errors:
        report += f"\n\n❌ Ошибки:\n" + "\n".join(f"• {e}" for e in errors)
    
    await query.message.edit_text(report, parse_mode="Markdown")
    
    if success == len(active_channels):
        user_sessions.pop(user_id, None)

async def publish_video_to_all_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await publish_to_all_channels_callback(update, context)

# ==================== ВЫБОР КАНАЛА (ДЛЯ ВСЕХ РЕЖИМОВ) ====================
async def select_channel_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных для публикации")
        return
    
    context.user_data["temp_session"] = session
    context.user_data["temp_source"] = "post"
    
    keyboard = []
    for key, channel in CHANNELS.items():
        if channel["channel_id"]:
            keyboard.append([InlineKeyboardButton(f"📢 {channel['name']}", callback_data=f"publish_to_channel:{key}:post")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_post")])
    
    await query.message.reply_text(
        "🌍 *Выберите канал для публикации*\n\n"
        "Нажмите на нужный канал, и пост будет опубликован туда.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def select_channel_menu_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных для публикации")
        return
    
    context.user_data["temp_session"] = session
    context.user_data["temp_source"] = "video"
    
    keyboard = []
    for key, channel in CHANNELS.items():
        if channel["channel_id"]:
            keyboard.append([InlineKeyboardButton(f"📢 {channel['name']}", callback_data=f"publish_to_channel:{key}:video")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_video_preview")])
    
    await query.message.reply_text(
        "🌍 *Выберите канал для публикации видео*\n\n"
        "Нажмите на нужный канал, и видео будет опубликовано туда.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def select_channel_menu_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных для публикации")
        return
    
    context.user_data["temp_session"] = session
    context.user_data["temp_source"] = "designed"
    
    keyboard = []
    for key, channel in CHANNELS.items():
        if channel["channel_id"]:
            keyboard.append([InlineKeyboardButton(f"📢 {channel['name']}", callback_data=f"publish_to_channel:{key}:designed")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_designed")])
    
    await query.message.reply_text(
        "🌍 *Выберите канал для публикации*\n\n"
        "Нажмите на нужный канал, и пост будет опубликован туда.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def select_channel_menu_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных для публикации")
        return
    
    context.user_data["temp_session"] = session
    context.user_data["temp_source"] = "ai"
    
    keyboard = []
    for key, channel in CHANNELS.items():
        if channel["channel_id"]:
            keyboard.append([InlineKeyboardButton(f"📢 {channel['name']}", callback_data=f"publish_to_channel:{key}:ai")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_ai")])
    
    await query.message.reply_text(
        "🌍 *Выберите канал для публикации*\n\n"
        "Нажмите на нужный канал, и пост будет опубликован туда.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def publish_to_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        _, channel_key, source = query.data.split(":")
    except ValueError:
        await query.message.reply_text("❌ Ошибка формата данных")
        return
    
    session = context.user_data.get("temp_session")
    
    if not session:
        await query.message.reply_text("❌ Нет данных для публикации. Отправьте пост заново.")
        return
    
    channel_info = CHANNELS.get(channel_key)
    if not channel_info or not channel_info["channel_id"]:
        await query.message.reply_text("❌ Канал не настроен")
        return
    
    await query.message.edit_text(f"⏳ Публикую в {channel_info['name']}...")
    
    try:
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        video_bytes = session.get("video_bytes")
        is_video = session.get("type") == "video"
        
        await publish_to_channel(
            context.bot,
            channel_info["channel_id"],
            text,
            photo_bytes,
            video_bytes,
            is_video
        )
        
        await query.message.edit_text(f"✅ Опубликовано в {channel_info['name']}!")
        
        user_id = query.from_user.id
        user_sessions.pop(user_id, None)
        context.user_data.pop("temp_session", None)
        context.user_data.pop("temp_source", None)
        
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

# ==================== ОТЛОЖЕННАЯ ПУБЛИКАЦИЯ ====================
async def schedule_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=get_schedule_keyboard())

async def schedule_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
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
        time_str = publish_time.strftime("%H:%M (%d.%m)")
    
    save_scheduled_post(
        session.get("text", ""),
        session.get("photo_bytes"),
        publish_time
    )
    
    await query.message.reply_text(
        f"✅ Пост запланирован на {time_str}\n\n"
        f"Он будет автоматически опубликован в основной канал."
    )
    
    user_sessions.pop(user_id, None)
    
    try:
        await query.message.delete()
    except:
        pass

async def schedule_video_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=get_video_schedule_keyboard())

async def schedule_video_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
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
        time_str = publish_time.strftime("%H:%M (%d.%m)")
    
    save_scheduled_video(
        session.get("text", ""),
        session.get("video_bytes"),
        publish_time
    )
    
    await query.message.reply_text(
        f"✅ Видео запланировано на {time_str}\n\n"
        f"Оно будет автоматически опубликовано в основной канал."
    )
    
    user_sessions.pop(user_id, None)
    
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
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
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
        time_str = publish_time.strftime("%H:%M (%d.%m)")
    
    save_scheduled_post(
        session.get("text", ""),
        session.get("photo_bytes"),
        publish_time
    )
    
    await query.message.reply_text(
        f"✅ Оформленный пост запланирован на {time_str}\n\n"
        f"Он будет автоматически опубликован в основной канал."
    )
    
    user_sessions.pop(user_id, None)
    context.user_data["scheduling_designed"] = False
    
    try:
        await query.message.delete()
    except:
        pass

# ==================== НАВИГАЦИЯ ====================
async def back_to_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if session and session.get("photo_bytes"):
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
            caption=text if text else "Пост",
            reply_markup=get_post_preview_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass

async def back_to_video_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if session and session.get("video_bytes"):
        text = session.get("text", "")
        video_bytes = session.get("video_bytes")
        
        await query.message.reply_video(
            video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
            caption=text if text else "Видео",
            reply_markup=get_video_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass

async def back_to_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = context.user_data.get("temp_session")
    
    if session and session.get("photo_bytes"):
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
            caption=text if text else "Пост",
            reply_markup=get_post_preview_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    
    context.user_data.pop("temp_session", None)
    context.user_data.pop("temp_source", None)

async def back_to_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = context.user_data.get("temp_session")
    
    if session and session.get("video_bytes"):
        text = session.get("text", "")
        video_bytes = session.get("video_bytes")
        
        await query.message.reply_video(
            video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
            caption=text if text else "Видео",
            reply_markup=get_video_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    
    context.user_data.pop("temp_session", None)
    context.user_data.pop("temp_source", None)

async def back_to_designed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = context.user_data.get("temp_session")
    
    if session and session.get("photo_bytes"):
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
            caption=f"{text}\n\n✅ Пост оформлен!" if text else "Пост оформлен!",
            reply_markup=get_designed_post_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    
    context.user_data.pop("temp_session", None)
    context.user_data.pop("temp_source", None)

async def back_to_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = context.user_data.get("temp_session")
    
    if session and session.get("photo_bytes"):
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
            caption=f"✅ *Текст обработан!*\n\n{text}",
            parse_mode="Markdown",
            reply_markup=get_ai_result_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    
    context.user_data.pop("temp_session", None)
    context.user_data.pop("temp_source", None)

# ==================== ПАРСИНГ НОВОСТЕЙ ====================
async def start_parsing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("⏳ Парсинг новостей...")
    
    news_items = await fetch_news_from_csv(10)
    if not news_items:
        await query.message.reply_text("❌ Не удалось загрузить новости", reply_markup=get_main_keyboard())
        return
    
    pending_news.clear()
    
    for i, item in enumerate(news_items):
        if is_already_published(item['url']):
            continue
        
        image_bytes = await fetch_article_image(item['url'])
        article_text = await fetch_article_text(item['url'])
        
        news_id = f"{i}_{abs(hash(item['url']))}"
        
        caption = f"📰 *{item['title']}*\n\n{article_text[:500]}...\n\n🔗 [Читать]({item['url']})"
        
        if image_bytes:
            await query.message.reply_photo(
                photo=InputFile(io.BytesIO(image_bytes), filename="news.jpg"),
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

async def publish_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    news_id = query.data.split(":")[1]
    news = pending_news.get(news_id)
    
    if not news:
        await query.message.reply_text("❌ Новость не найдена")
        return
    
    try:
        news_text = news['text']
        if len(news_text) > 1000:
            news_text = news_text[:1000] + "..."
        
        caption = format_caption(news['title'], f"{news_text}\n\n🔗 {news['url']}")
        
        if news.get('photo'):
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=InputFile(io.BytesIO(news['photo']), filename="news.jpg"),
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
        await query.message.reply_text("✅ Опубликовано!")
        
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def skip_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    news_id = query.data.split(":")[1]
    pending_news.pop(news_id, None)
    
    try:
        await query.message.delete()
    except:
        pass

# ==================== ПЛАНИРОВЩИК ====================
async def check_scheduled_posts(app: Application):
    while True:
        try:
            posts = get_pending_scheduled_posts()
            for post in posts:
                text = post["text"]
                photo_bytes = post["photo_bytes"]
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                if caption:
                    await app.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
                else:
                    await app.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                        reply_markup=get_post_publish_keyboard()
                    )
                
                delete_scheduled_post(post["id"])
                print(f"✅ Опубликован отложенный пост")
            
            videos = get_pending_scheduled_videos()
            for video in videos:
                text = video["text"]
                video_bytes = video["video_bytes"]
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                if caption:
                    await app.bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_post_publish_keyboard()
                    )
                else:
                    await app.bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                        reply_markup=get_post_publish_keyboard()
                    )
                
                delete_scheduled_video(video["id"])
                print(f"✅ Опубликовано отложенное видео")
                
        except Exception as e:
            print(f"❌ Ошибка в планировщике: {e}")
        
        await asyncio.sleep(60)

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
    
    bot = Bot(token=BOT_TOKEN)
    
    # Принудительно удаляем вебхук
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook удалён")
    except Exception as e:
        print(f"⚠️ Ошибка при удалении webhook: {e}")
    
    await asyncio.sleep(1)
    
    if deepseek_client:
        print("✅ DeepSeek API подключен")
    else:
        print("⚠️ DeepSeek API не настроен")
    
    active = sum(1 for ch in CHANNELS.values() if ch["channel_id"])
    print(f"✅ Активных каналов: {active}")
    for key, ch in CHANNELS.items():
        if ch["channel_id"]:
            print(f"   • {ch['name']}: {ch['channel_id']}")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_edit))
    
    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_request))
    
    # Колбэки парсинга
    application.add_handler(CallbackQueryHandler(start_parsing_callback, pattern="start_parsing"))
    application.add_handler(CallbackQueryHandler(publish_news_callback, pattern="publish_news:"))
    application.add_handler(CallbackQueryHandler(skip_news_callback, pattern="skip_news:"))
    
    # Оформление и редактирование
    application.add_handler(CallbackQueryHandler(design_post_callback, pattern="design_post"))
    application.add_handler(CallbackQueryHandler(edit_text_callback, pattern="edit_text"))
    application.add_handler(CallbackQueryHandler(edit_video_text_callback, pattern="edit_video_text"))
    application.add_handler(CallbackQueryHandler(edit_designed_text_callback, pattern="edit_designed_text"))
    
    # Обработка ИИ
    application.add_handler(CallbackQueryHandler(ai_process_callback, pattern="ai_process"))
    application.add_handler(CallbackQueryHandler(ai_process_video_callback, pattern="ai_process_video"))
    application.add_handler(CallbackQueryHandler(ai_reprocess_callback, pattern="ai_reprocess"))
    application.add_handler(CallbackQueryHandler(ai_reprocess_video_callback, pattern="ai_reprocess_video"))
    
    # Публикация
    application.add_handler(CallbackQueryHandler(publish_raw_callback, pattern="publish_raw"))
    application.add_handler(CallbackQueryHandler(publish_designed_callback, pattern="publish_designed"))
    application.add_handler(CallbackQueryHandler(publish_video_callback, pattern="publish_video"))
    
    # Публикация во все каналы
    application.add_handler(CallbackQueryHandler(publish_to_all_channels_callback, pattern="publish_to_all_channels"))
    application.add_handler(CallbackQueryHandler(publish_video_to_all_channels_callback, pattern="publish_video_to_all_channels"))
    
    # Выбор канала (для всех режимов)
    application.add_handler(CallbackQueryHandler(select_channel_menu_callback, pattern="select_channel_menu$"))
    application.add_handler(CallbackQueryHandler(select_channel_menu_video_callback, pattern="select_channel_menu_video"))
    application.add_handler(CallbackQueryHandler(select_channel_menu_designed_callback, pattern="select_channel_menu_designed"))
    application.add_handler(CallbackQueryHandler(select_channel_menu_ai_callback, pattern="select_channel_menu_ai"))
    application.add_handler(CallbackQueryHandler(publish_to_channel_callback, pattern="publish_to_channel:"))
    
    # Отложенная публикация
    application.add_handler(CallbackQueryHandler(schedule_menu_callback, pattern="schedule_menu"))
    application.add_handler(CallbackQueryHandler(schedule_video_menu_callback, pattern="schedule_video_menu"))
    application.add_handler(CallbackQueryHandler(schedule_designed_callback, pattern="schedule_designed"))
    application.add_handler(CallbackQueryHandler(schedule_time_callback, pattern="schedule_time:"))
    application.add_handler(CallbackQueryHandler(schedule_video_time_callback, pattern="schedule_video_time:"))
    application.add_handler(CallbackQueryHandler(schedule_designed_time_callback, pattern="schedule_designed_time:"))
    
    # Навигация
    application.add_handler(CallbackQueryHandler(back_to_preview_callback, pattern="back_to_preview"))
    application.add_handler(CallbackQueryHandler(back_to_video_preview_callback, pattern="back_to_video_preview"))
    application.add_handler(CallbackQueryHandler(back_to_post_callback, pattern="back_to_post"))
    application.add_handler(CallbackQueryHandler(back_to_video_callback, pattern="back_to_video"))
    application.add_handler(CallbackQueryHandler(back_to_designed_callback, pattern="back_to_designed"))
    application.add_handler(CallbackQueryHandler(back_to_ai_callback, pattern="back_to_ai"))
    
    await application.initialize()
    await application.start()
    
    asyncio.create_task(check_scheduled_posts(application))
    
    # Запускаем polling
    await application.updater.start_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    
    print("✅ Бот запущен!")

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
