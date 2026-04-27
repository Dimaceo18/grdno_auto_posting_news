import asyncio
import sqlite3
import csv
import os
import re
import io
from io import StringIO
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Bot
import httpx
from newspaper import Article
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CSV_URL = "https://rss.app/feeds/eblnvNTLpd5syIbd.csv"
DB_PATH = "news.db"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 300 секунд = 5 минут

# ==================== НАСТРОЙКИ ДЛЯ ШАБЛОНА ЧП ВМ ====================
TARGET_WIDTH = 750
TARGET_HEIGHT = 938
GRADIENT_HEIGHT_PCT = 0.48  # 48% высоты под градиент
FONT_PATH = "Montserrat-Black.ttf"  # Шрифт для заголовка

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
                    'title': row.get('Title', ''),
                    'published_at': row.get('Date', datetime.now().isoformat()),
                })

        print(f"📊 Найдено новых ссылок: {len(new_news)}")
        return new_news
    except Exception as e:
        print(f"❌ Ошибка при чтении CSV: {e}")
        return []

# ==================== ПАРСЕР ПОЛНОЙ СТАТЬИ ====================
async def fetch_full_article(url: str) -> Optional[Dict]:
    """
    С помощью newspaper3k получаем заголовок, полный текст и главное изображение статьи
    """
    try:
        loop = asyncio.get_event_loop()
        article = await loop.run_in_executor(None, lambda: Article(url, language='ru'))

        article.download()
        article.parse()

        title = article.title or "Без заголовка"
        full_text = article.text or "Текст статьи не найден."

        if len(full_text) > 3800:
            full_text = full_text[:3800] + "\n\n...(продолжение на сайте)"

        top_image = article.top_image

        return {
            'title': title,
            'text': full_text,
            'image_url': top_image
        }
    except Exception as e:
        print(f"❌ Ошибка при парсинге статьи {url}: {e}")
        return None

# ==================== ОБРАБОТКА ФОТО (СТИЛЬ ЧП ВМ) ====================
def crop_to_4x5(img: Image.Image) -> Image.Image:
    """Обрезает фото до пропорции 4:5"""
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
    """Накладывает градиент (затемнение) снизу вверх"""
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
    """Разбивает длинный текст на строки"""
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

def process_photo_chp(photo_bytes: bytes, title_text: str) -> io.BytesIO:
    """
    Обрабатывает фото в стиле ЧП ВМ:
    - Обрезает до 4:5
    - Накладывает градиент снизу
    - Добавляет заголовок белым текстом
    """
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    
    # Обрезаем и ресайзим
    img = crop_to_4x5(img)
    img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.Resampling.LANCZOS)
    
    # Немного увеличиваем яркость
    img = ImageEnhance.Brightness(img).enhance(0.9)
    
    # Накладываем градиент снизу
    img = apply_bottom_gradient(img, GRADIENT_HEIGHT_PCT, max_alpha=220)
    
    # Загружаем шрифт
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
    
    # Разбиваем текст на строки
    text_lines = wrap_text(title_text.upper(), font, max_text_width, max_lines=4)
    
    # Рассчитываем высоту текста
    line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    spacing = int(line_height * 0.2)
    total_text_height = len(text_lines) * line_height + (len(text_lines) - 1) * spacing
    
    # Располагаем текст снизу
    y = img.height - margin_bottom - total_text_height
    
    # Рисуем каждую строку
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

# ==================== ОТПРАВКА В TELEGRAM ====================
async def send_full_news(article_data: dict, original_url: str, original_title: str) -> bool:
    """Отправляет новость с обработанным фото (градиент + заголовок)"""
    bot = Bot(token=BOT_TOKEN)
    
    # Заголовок для обработки фото (используем тот, что спарсили со страницы)
    photo_title = article_data['title'] if article_data['title'] != "Без заголовка" else original_title
    
    if not article_data.get('image_url'):
        # Если нет фото, отправляем только текст
        message = f"<b>{article_data['title']}</b>\n\n{article_data['text']}\n\n<a href='{original_url}'>📖 Читать на сайте</a>"
        try:
            await bot.send_message(CHANNEL_ID, message, parse_mode='HTML')
            return True
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            return False
    
    try:
        # Скачиваем фото
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(article_data['image_url'])
            response.raise_for_status()
            photo_bytes = response.content
        
        # Обрабатываем фото
        processed_photo = process_photo_chp(photo_bytes, photo_title)
        
        # Формируем подпись
        caption = (
            f"<b>{article_data['title']}</b>\n\n"
            f"{article_data['text'][:500]}...\n\n"
            f"<a href='{original_url}'>📖 Читать полностью на сайте</a>\n\n"
            f"#Гродно #Новости"
        )
        
        # Отправляем
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=processed_photo,
            caption=caption,
            parse_mode='HTML'
        )
        
        print(f"✅ Отправлено (с фото ЧП ВМ): {article_data['title'][:50]}...")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при обработке фото: {e}")
        # Резервная отправка без обработки
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(article_data['image_url'])
                response.raise_for_status()
            
            caption = f"<b>{article_data['title']}</b>\n\n{article_data['text'][:500]}...\n\n<a href='{original_url}'>📖 Читать на сайте</a>"
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=response.content,
                caption=caption,
                parse_mode='HTML'
            )
            return True
        except Exception as e2:
            print(f"❌ Резервная отправка тоже не удалась: {e2}")
            return False

# ==================== ОСНОВНАЯ ЛОГИКА ====================
async def check_and_send():
    """Основная логика: новые ссылки → парсинг статьи → обработка фото → отправка"""
    print(f"🔍 Проверка новостей: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    new_links = await fetch_new_news_from_csv()
    
    for item in new_links:
        url = item['url']
        csv_title = item.get('title', '')
        
        print(f"📰 Обработка: {url}")
        
        # Получаем полную статью
        article_data = await fetch_full_article(url)
        
        if article_data:
            # Успешно: отправляем с обработанным фото
            success = await send_full_news(article_data, url, csv_title)
            if success:
                save_news(url, article_data['title'], item['published_at'])
        else:
            # Не удалось спарсить статью
            print(f"⚠️ Не удалось спарсить {url}, пропускаем")
        
        await asyncio.sleep(2)  # Пауза между новостями
    
    # Раз в сутки чистим БД
    if datetime.now().hour == 3:
        cleanup_old_news()
    
    print(f"✅ Цикл завершён, следующая проверка через {CHECK_INTERVAL} секунд\n")

async def periodic_checker():
    """Бесконечный цикл проверки новостей"""
    while True:
        await check_and_send()
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== ВЕБ-СЕРВЕР ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск планировщика
    init_db()
    task = asyncio.create_task(periodic_checker())
    print(f"✅ Бот запущен! Проверка новостей каждые {CHECK_INTERVAL} секунд")
    print(f"📐 Шаблон: ЧП ВМ (градиент + заголовок на фото)")
    yield
    task.cancel()
    print("🛑 Бот остановлен")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "Grodno News Bot (CHP style)",
        "interval_seconds": CHECK_INTERVAL,
        "version": "3.0 - with photo processing"
    }

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.get("/stats")
async def stats():
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
    return {"total_news_sent": count}

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
