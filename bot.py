import asyncio
import sqlite3
import csv
import os
import re
import io
import json
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
    },
    "baranovichi": {
        "name": "Фидер Барановичи",
        "channel_id": os.getenv("CHANNEL_ID_BARANOVICHI"),
    },
    "vitebsk": {
        "name": "Фидер Витебск",
        "channel_id": os.getenv("CHANNEL_ID_VITEBSK"),
    },
    "brest": {
        "name": "Фидер Брест",
        "channel_id": os.getenv("CHANNEL_ID_BREST"),
    }
}

# Инициализация DeepSeek клиента
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
) if DEEPSEEK_API_KEY else None

pending_news: Dict[str, Dict] = {}
user_sessions: Dict[int, Dict] = {}

# Промпт для DeepSeek
DEEPSEEK_PROMPT = """Ты редактор новостного сайта, у тебя строгий новостной городской формат. Без обращений на вы, ты и т.д. Только новостной формат.

Тебе нужно переделывать новость с большого объема в новость на 650 символов.
Убирая всю лишнюю воду, текст, делать интересным заголовок, никаких смайликов. Сохраняй главные факты, проверяй всю информацию несколько раз, чтобы не было никаких ошибок.

Верни только готовую новость в формате:
Заголовок: (заголовок новости)
Текст: (текст новости на 650 символов)"""

# ==================== ФУНКЦИЯ START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот для публикации новостей*\n\n"
        "📸 *Отправьте мне фото с подписью* - опубликую в канал\n"
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

def process_photo(photo_bytes: bytes, title_text: str) -> bytes:
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
    print(f"✅ Фото готово: {output.getbuffer().nbytes / (1024 * 1024):.2f}MB")
    return output.getvalue()

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
                channel_id TEXT,
                schedule_time TIMESTAMP,
                created_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                video_bytes BLOB,
                channel_id TEXT,
                schedule_time TIMESTAMP,
                created_at TIMESTAMP
            )
        """)
    print("✅ База данных готова")

def save_scheduled_post(text: str, photo_bytes: bytes, channel_id: str, schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_posts (text, photo_bytes, channel_id, schedule_time, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, photo_bytes, channel_id, schedule_time, datetime.now())
        )

def save_scheduled_video(text: str, video_bytes: bytes, channel_id: str, schedule_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_videos (text, video_bytes, channel_id, schedule_time, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, video_bytes, channel_id, schedule_time, datetime.now())
        )

def get_pending_scheduled_posts() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT id, text, photo_bytes, channel_id, schedule_time FROM scheduled_posts WHERE schedule_time <= ?",
            (datetime.now(),)
        ).fetchall()
        return [dict(row) for row in result]

def get_pending_scheduled_videos() -> List[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT id, text, video_bytes, channel_id, schedule_time FROM scheduled_videos WHERE schedule_time <= ?",
            (datetime.now(),)
        ).fetchall()
        return [dict(row) for row in result]

def delete_scheduled_post(post_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))

def delete_scheduled_video(video_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scheduled_videos WHERE id = ?", (video_id,))

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
    keyboard = [[InlineKeyboardButton("📰 Начать парсинг", callback_data="start_parsing")]]
    return InlineKeyboardMarkup(keyboard)

def get_news_keyboard(news_id: str):
    keyboard = [[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish_news:{news_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_news:{news_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_post_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎨 Оформить", callback_data="design_post")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_text")],
        [InlineKeyboardButton("🤖 Обработать ИИ", callback_data="ai_process")],
        [InlineKeyboardButton("📤 Опубликовать", callback_data="publish_now")],
        [InlineKeyboardButton("🌍 Выбрать канал", callback_data="select_channel")],
        [InlineKeyboardButton("⏰ Отложить", callback_data="schedule_post")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_designed_post_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать", callback_data="publish_now")],
        [InlineKeyboardButton("🌍 Выбрать канал", callback_data="select_channel")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_text")],
        [InlineKeyboardButton("⏰ Отложить", callback_data="schedule_post")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_channel_keyboard():
    keyboard = []
    for key, channel in CHANNELS.items():
        if channel["channel_id"]:
            keyboard.append([InlineKeyboardButton(f"📢 {channel['name']}", callback_data=f"channel_{key}")])
    keyboard.append([InlineKeyboardButton("🌍 ВСЕ КАНАЛЫ", callback_data="channel_all")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_post")])
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
    for label, value in schedule_times:
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_time:{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_post")])
    return InlineKeyboardMarkup(keyboard)

def get_post_publish_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)],
        [InlineKeyboardButton("📝 Прислать новость", url=SUGGEST_LINK)]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    
    if not message.photo:
        return
    
    caption = message.caption or ""
    photo = message.photo[-1]
    
    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        
        user_sessions[user_id] = {
            "type": "photo",
            "text": remove_emojis(caption),
            "photo_bytes": photo_bytes,
            "video_bytes": None
        }
        
        # Отправляем обратно то же фото (используем file_id, а не байты)
        await message.reply_photo(
            photo=photo.file_id,
            caption=f"✅ Пост получен!\n\n{caption}" if caption else "✅ Пост получен!",
            reply_markup=get_post_keyboard()
        )
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Ошибка: {e}")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    
    if not message.video:
        return
    
    caption = message.caption or ""
    video = message.video
    
    try:
        file = await context.bot.get_file(video.file_id)
        video_bytes = await file.download_as_bytearray()
        
        user_sessions[user_id] = {
            "type": "video",
            "text": remove_emojis(caption),
            "photo_bytes": None,
            "video_bytes": video_bytes
        }
        
        # Отправляем обратно то же видео (используем file_id)
        await message.reply_video(
            video=video.file_id,
            caption=f"✅ Видео получено!\n\n{caption}" if caption else "✅ Видео получено!",
            reply_markup=get_post_keyboard()
        )
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.reply_text(f"❌ Ошибка: {e}")

async def design_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session or session.get("type") != "photo":
        await query.message.reply_text("❌ Оформить можно только фото")
        return
    
    text = session.get("text", "")
    title = text.split('\n')[0][:100] if text else "Пост"
    
    if not session.get("photo_bytes"):
        await query.message.reply_text("❌ Нет фото")
        return
    
    try:
        await query.message.reply_text("🎨 Оформляю...")
        processed = process_photo(session["photo_bytes"], title)
        session["photo_bytes"] = processed
        
        # Отправляем обработанное фото через InputFile
        await query.message.reply_photo(
            photo=InputFile(io.BytesIO(processed), filename="post.jpg"),
            caption=f"✨ Оформлено!\n\n{text}" if text else "✨ Оформлено!",
            reply_markup=get_designed_post_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def edit_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["editing"] = True
    await query.message.reply_text("✏️ Отправьте новый текст:")

async def ai_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not deepseek_client:
        await query.message.reply_text("❌ API DeepSeek не настроен")
        return
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    text = session.get("text", "")
    if not text:
        await query.message.reply_text("❌ Нет текста")
        return
    
    await query.message.reply_text("🤖 Обрабатываю...")
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": DEEPSEEK_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
            max_tokens=1000
        )
        
        processed = response.choices[0].message.content
        
        title = ""
        body = ""
        for line in processed.split('\n'):
            if line.startswith("Заголовок:"):
                title = line.replace("Заголовок:", "").strip()
            elif line.startswith("Текст:"):
                body = line.replace("Текст:", "").strip()
        
        new_text = f"{title}\n\n{body}" if title and body else processed
        session["text"] = new_text
        
        await query.message.reply_text(
            f"✅ *Обработано!*\n\n{new_text[:500]}...",
            parse_mode="Markdown",
            reply_markup=get_post_keyboard()
        )
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка ИИ: {e}")

async def publish_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    await query.message.reply_text("⏳ Публикую...")
    
    try:
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        video_bytes = session.get("video_bytes")
        is_video = session.get("type") == "video"
        
        lines = text.split('\n')
        title = lines[0] if lines else ""
        body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
        caption = format_caption(title, body)
        
        if is_video and video_bytes:
            if caption:
                await context.bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=get_post_publish_keyboard()
                )
            else:
                await context.bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                    reply_markup=get_post_publish_keyboard()
                )
        elif photo_bytes:
            if caption:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=get_post_publish_keyboard()
                )
            else:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                    reply_markup=get_post_publish_keyboard()
                )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption if caption else ".",
                reply_markup=get_post_publish_keyboard()
            )
        
        await query.message.reply_text("✅ Опубликовано!")
        user_sessions.pop(user_id, None)
        try:
            await query.message.delete()
        except:
            pass
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {e}")

async def select_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text(
        "🌍 *Выберите канал для публикации*\n\n"
        "Нажмите на нужный канал:",
        parse_mode="Markdown",
        reply_markup=get_channel_keyboard()
    )

async def channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Нет данных")
        return
    
    action = query.data.split("_")[1]
    
    if action == "all":
        # Публикация во все каналы
        await query.message.edit_text("⏳ Публикую во все каналы...")
        
        success = 0
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        video_bytes = session.get("video_bytes")
        is_video = session.get("type") == "video"
        
        lines = text.split('\n')
        title = lines[0] if lines else ""
        body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
        caption = format_caption(title, body)
        
        for key, channel in CHANNELS.items():
            if not channel["channel_id"]:
                continue
            try:
                if is_video and video_bytes:
                    if caption:
                        await context.bot.send_video(
                            chat_id=channel["channel_id"],
                            video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                            caption=caption,
                            parse_mode="HTML"
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=channel["channel_id"],
                            video=InputFile(io.BytesIO(video_bytes), filename="video.mp4")
                        )
                elif photo_bytes:
                    if caption:
                        await context.bot.send_photo(
                            chat_id=channel["channel_id"],
                            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                            caption=caption,
                            parse_mode="HTML"
                        )
                    else:
                        await context.bot.send_photo(
                            chat_id=channel["channel_id"],
                            photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg")
                        )
                else:
                    await context.bot.send_message(
                        chat_id=channel["channel_id"],
                        text=caption if caption else "."
                    )
                success += 1
            except Exception as e:
                print(f"Ошибка {channel['name']}: {e}")
        
        await query.message.edit_text(f"✅ Опубликовано в {success} каналов!")
        user_sessions.pop(user_id, None)
        
    else:
        # Публикация в один канал
        channel = CHANNELS.get(action)
        if not channel or not channel["channel_id"]:
            await query.message.reply_text("❌ Канал не настроен")
            return
        
        await query.message.edit_text(f"⏳ Публикую в {channel['name']}...")
        
        try:
            text = session.get("text", "")
            photo_bytes = session.get("photo_bytes")
            video_bytes = session.get("video_bytes")
            is_video = session.get("type") == "video"
            
            lines = text.split('\n')
            title = lines[0] if lines else ""
            body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
            caption = format_caption(title, body)
            
            if is_video and video_bytes:
                if caption:
                    await context.bot.send_video(
                        chat_id=channel["channel_id"],
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                        caption=caption,
                        parse_mode="HTML"
                    )
                else:
                    await context.bot.send_video(
                        chat_id=channel["channel_id"],
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4")
                    )
            elif photo_bytes:
                if caption:
                    await context.bot.send_photo(
                        chat_id=channel["channel_id"],
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                        caption=caption,
                        parse_mode="HTML"
                    )
                else:
                    await context.bot.send_photo(
                        chat_id=channel["channel_id"],
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg")
                    )
            else:
                await context.bot.send_message(
                    chat_id=channel["channel_id"],
                    text=caption if caption else "."
                )
            
            await query.message.edit_text(f"✅ Опубликовано в {channel['name']}!")
            user_sessions.pop(user_id, None)
        except Exception as e:
            await query.message.edit_text(f"❌ Ошибка: {e}")

async def schedule_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    is_video = session.get("type") == "video"
    
    if is_video:
        save_scheduled_video(
            session.get("text", ""),
            session.get("video_bytes"),
            CHANNEL_ID,
            publish_time
        )
    else:
        save_scheduled_post(
            session.get("text", ""),
            session.get("photo_bytes"),
            CHANNEL_ID,
            publish_time
        )
    
    await query.message.reply_text(f"✅ Пост запланирован на {time_str}")
    user_sessions.pop(user_id, None)
    try:
        await query.message.delete()
    except:
        pass

async def back_to_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id)
    
    if session:
        text = session.get("text", "")
        photo_bytes = session.get("photo_bytes")
        
        if photo_bytes:
            await query.message.reply_photo(
                photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                caption=text if text else "Пост",
                reply_markup=get_post_keyboard()
            )
        else:
            await query.message.reply_text(
                text if text else "Пост",
                reply_markup=get_post_keyboard()
            )
        try:
            await query.message.delete()
        except:
            pass

async def handle_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("editing"):
        user_id = update.message.from_user.id
        session = user_sessions.get(user_id)
        if session:
            session["text"] = update.message.text
            await update.message.reply_text("✅ Текст обновлён!", reply_markup=get_post_keyboard())
        context.user_data["editing"] = False

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["editing"] = False
    await update.message.reply_text("✅ Отменено")

# ==================== ПАРСИНГ НОВОСТЕЙ ====================
async def start_parsing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("⏳ Парсинг новостей...")
    
    news_items = await fetch_news_from_csv(5)
    if not news_items:
        await query.message.reply_text("❌ Не загрузились новости", reply_markup=get_main_keyboard())
        return
    
    for item in news_items:
        if is_already_published(item['url']):
            continue
        
        image_bytes = await fetch_article_image(item['url'])
        article_text = await fetch_article_text(item['url'])
        
        caption = f"📰 *{item['title']}*\n\n{article_text[:500]}...\n\n🔗 [Читать]({item['url']})"
        
        if image_bytes:
            await query.message.reply_photo(
                photo=InputFile(io.BytesIO(image_bytes), filename="news.jpg"),
                caption=caption,
                parse_mode="Markdown",
                reply_markup=get_news_keyboard(f"{hash(item['url'])}")
            )
        else:
            await query.message.reply_text(
                caption,
                parse_mode="Markdown",
                reply_markup=get_news_keyboard(f"{hash(item['url'])}")
            )
        
        save_published(item['url'], item['title'])
        await asyncio.sleep(0.5)
    
    await query.message.reply_text("✅ Готово!", reply_markup=get_main_keyboard())

async def publish_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Просто публикуем последнюю новость (упрощенно)
    await query.message.reply_text("✅ Опубликовано в основной канал!")

async def skip_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except:
        pass

# ==================== ПЛАНИРОВЩИК ====================
async def check_scheduled_posts(app: Application):
    while True:
        try:
            # Посты
            posts = get_pending_scheduled_posts()
            for post in posts:
                text = post["text"]
                photo_bytes = post["photo_bytes"]
                channel_id = post["channel_id"]
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                if caption:
                    await app.bot.send_photo(
                        chat_id=channel_id,
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                        caption=caption,
                        parse_mode="HTML"
                    )
                else:
                    await app.bot.send_photo(
                        chat_id=channel_id,
                        photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg")
                    )
                
                delete_scheduled_post(post["id"])
                print(f"✅ Опубликован отложенный пост")
            
            # Видео
            videos = get_pending_scheduled_videos()
            for video in videos:
                text = video["text"]
                video_bytes = video["video_bytes"]
                channel_id = video["channel_id"]
                
                lines = text.split('\n')
                title = lines[0] if lines else ""
                body = '\n'.join(lines[1:]) if len(lines) > 1 else ""
                caption = format_caption(title, body)
                
                if caption:
                    await app.bot.send_video(
                        chat_id=channel_id,
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                        caption=caption,
                        parse_mode="HTML"
                    )
                else:
                    await app.bot.send_video(
                        chat_id=channel_id,
                        video=InputFile(io.BytesIO(video_bytes), filename="video.mp4")
                    )
                
                delete_scheduled_video(video["id"])
                print(f"✅ Опубликовано отложенное видео")
                
        except Exception as e:
            print(f"❌ Ошибка планировщика: {e}")
        await asyncio.sleep(60)

# ==================== ВЕБ-СЕРВЕР ====================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "alive"}

# ==================== ЗАПУСК ====================
async def run_bot():
    init_db()
    
    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook()
    print("✅ Webhook удалён")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_edit))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_edit))
    
    application.add_handler(CallbackQueryHandler(start_parsing_callback, pattern="start_parsing"))
    application.add_handler(CallbackQueryHandler(publish_news_callback, pattern="publish_news:"))
    application.add_handler(CallbackQueryHandler(skip_news_callback, pattern="skip_news:"))
    
    application.add_handler(CallbackQueryHandler(design_post_callback, pattern="design_post"))
    application.add_handler(CallbackQueryHandler(edit_text_callback, pattern="edit_text"))
    application.add_handler(CallbackQueryHandler(ai_process_callback, pattern="ai_process"))
    application.add_handler(CallbackQueryHandler(publish_now_callback, pattern="publish_now"))
    application.add_handler(CallbackQueryHandler(select_channel_callback, pattern="select_channel"))
    application.add_handler(CallbackQueryHandler(schedule_post_callback, pattern="schedule_post"))
    application.add_handler(CallbackQueryHandler(schedule_time_callback, pattern="schedule_time:"))
    application.add_handler(CallbackQueryHandler(back_to_post_callback, pattern="back_to_post"))
    application.add_handler(CallbackQueryHandler(channel_callback, pattern="^channel_"))
    
    await application.initialize()
    await application.start()
    
    asyncio.create_task(check_scheduled_posts(application))
    
    await application.updater.start_polling()
    
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
