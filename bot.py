# -*- coding: utf-8 -*-

import os
import io
import threading
import logging
import re
import asyncio
import httpx

from flask import Flask
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TimedOut

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN")

web_app = Flask(__name__)

@web_app.get("/")
def health():
    return "OK", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port)


W, H = 1080, 1920

PURPLE = (111, 55, 245)
BLACK = (20, 22, 32)
WHITE = (255, 255, 255)

FONT_BOLD = "Montserrat-Black.ttf"
FONT_REGULAR = "Montserrat-Bold.ttf"
DIVIDER_PATH = "divider.png"

# Высота плашки-разделителя в пикселях
DIVIDER_HEIGHT = 50

# ИСПРАВЛЕННЫЙ ПРОМПТ ДЛЯ DEEPSEEK
DEEPSEEK_PROMPT = """Ты профессиональный редактор новостного сайта. Твоя задача - ПЕРЕПИСАТЬ длинную новость в короткий новостной формат, сохраняя все важные факты.

СТРОГИЕ ТРЕБОВАНИЯ:
1. Длина текста ДОЛЖНА БЫТЬ от 600 до 650 символов (считая пробелы)
2. НЕ ОБРЕЗАЙ текст - ПЕРЕПИШИ его, сохраняя смысл
3. Удали только явную воду: "подпишись", "читайте также", "переходите по ссылке", смайлики
4. Сохрани ВСЕ важные факты: кто, что, где, когда, почему, сколько
5. Сделай заголовок коротким и интересным (40-60 символов)
6. Обязательно разбей текст на 2-3 логических абзаца (абзацы отделяются пустой строкой)

Важно: Итоговый текст должен быть информативным и читаемым. Не удаляй важные детали, цифры, даты, имена.

Верни строго в формате:
ЗАГОЛОВОК: (заголовок новости)

ТЕКСТ: (текст новости на 600-650 символов, с абзацами)"""


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except:
        return ImageFont.load_default()


def crop_cover(img, size):
    target_w, target_h = size
    img_w, img_h = img.size
    scale = max(target_w / img_w, target_h / img_h)

    img = img.resize((int(img_w * scale), int(img_h * scale)), Image.LANCZOS)

    left = (img.width - target_w) // 2
    top = (img.height - target_h) // 2

    return img.crop((left, top, left + target_w, top + target_h))


def wrap_text(draw, text, fnt, max_width):
    words = text.split()
    lines = []
    line = ""

    for word in words:
        test = line + " " + word if line else word
        box = draw.textbbox((0, 0), test, font=fnt)

        if box[2] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word

    if line:
        lines.append(line)

    return lines


def fit_text(draw, text, font_path, max_width, max_height, start_size, min_size, gap):
    size = start_size

    while size >= min_size:
        fnt = font(font_path, size)
        lines = wrap_text(draw, text, fnt, max_width)
        total_h = len(lines) * (size + gap)

        if total_h <= max_height:
            return fnt, lines

        size -= 2

    fnt = font(font_path, min_size)
    lines = wrap_text(draw, text, fnt, max_width)
    return fnt, lines


def create_story(photo_bytes, title, body):
    # Проверка длины текста
    if len(body) > 900:
        raise ValueError("Текст слишком длинный, сделайте его короче (максимум 900 символов)")

    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")

    canvas = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(canvas)

    # Фиолетовая линия сверху (15 пикселей)
    top_line_height = 15
    draw.rectangle((0, 0, W, top_line_height), fill=PURPLE)

    # Высота фото - 40% от высоты сторис
    photo_h = int(H * 0.4)
    photo = crop_cover(img, (W, photo_h))
    photo = ImageEnhance.Brightness(photo).enhance(0.92)
    canvas.paste(photo, (0, top_line_height))

    # СНАЧАЛА создаём белый фон с текстом
    white_bg_start = photo_h + top_line_height
    draw.rectangle((0, white_bg_start, W, H), fill=WHITE)

    # Заголовок
    title = title.strip()

    title_font, title_lines = fit_text(
        draw,
        title,
        FONT_BOLD,
        max_width=900,
        max_height=300,
        start_size=58,
        min_size=38,
        gap=8,
    )

    y = white_bg_start + 80

    for line in title_lines[:5]:
        draw.text((80, y), line, font=title_font, fill=BLACK)
        y += title_font.size + 10

    # Три большие точки после заголовка
    y += 30
    dot_radius = 15
    dot_spacing = 20
    
    start_x = 80 + 25
    
    for i in range(3):
        x = start_x + i * (dot_radius * 2 + dot_spacing)
        y_dot = y + 10
        draw.ellipse((x - dot_radius, y_dot - dot_radius, x + dot_radius, y_dot + dot_radius), fill=PURPLE)
    
    y += 85

    # Основной текст
    body = body.strip()
    available_height = H - y - 150

    body_font, body_lines = fit_text(
        draw,
        body,
        FONT_REGULAR,
        max_width=900,
        max_height=available_height,
        start_size=33,
        min_size=16,
        gap=8,
    )

    for line in body_lines:
        draw.text((80, y), line, font=body_font, fill=BLACK)
        y += body_font.size + 8

    # Плашка-разделитель
    divider_y = photo_h + top_line_height
    
    if not os.path.exists(DIVIDER_PATH):
        draw.rectangle((0, divider_y, W, divider_y + DIVIDER_HEIGHT), fill=PURPLE)
    else:
        divider = Image.open(DIVIDER_PATH).convert("RGBA")
        divider = divider.resize((W, DIVIDER_HEIGHT), Image.LANCZOS)
        
        temp_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        temp_layer.paste(divider, (0, divider_y), divider)
        
        canvas = canvas.convert("RGBA")
        canvas = Image.alpha_composite(canvas, temp_layer)
        canvas = canvas.convert("RGB")
        draw = ImageDraw.Draw(canvas)

    # Логотип внизу по центру
    logo_font = font(FONT_BOLD, 38)
    logo_text = "fider.by"
    
    try:
        bbox = draw.textbbox((0, 0), logo_text, font=logo_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    except:
        text_width = len(logo_text) * 20
        text_height = 40
    
    logo_x = (W - text_width) // 2
    logo_y = H - 80
    
    padding = 20
    logo_bg_x1 = logo_x - padding
    logo_bg_y1 = logo_y - 12
    logo_bg_x2 = logo_x + text_width + padding
    logo_bg_y2 = logo_y + text_height + 12
    
    draw.rounded_rectangle(
        (logo_bg_x1, logo_bg_y1, logo_bg_x2, logo_bg_y2),
        radius=18,
        fill=PURPLE
    )
    
    draw.text(
        (logo_x, logo_y),
        logo_text,
        font=logo_font,
        fill=WHITE
    )

    # Фиолетовая полоса внизу (15 пикселей)
    footer_height = 15
    draw.rectangle((0, H - footer_height, W, H), fill=PURPLE)

    output = io.BytesIO()
    canvas.save(output, format="PNG", quality=95)
    output.seek(0)
    return output


# ==================== ФУНКЦИЯ ЗАПРОСА К DeepSeek ====================
async def call_deepseek(prompt, text):
    """Вызов DeepSeek API"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Перепиши эту новость в формате на 600-650 символов. Сохрани ВСЕ важные факты, цифры, даты, имена. НЕ ОБРЕЗАЙ текст, а ПЕРЕПИШИ его.\n\n{text}"}
                ],
                "temperature": 0.7,
                "max_tokens": 1200
            }
        )
        return response.json()


async def call_deepseek_with_fix(prompt, text, original_text=None):
    """Вызов DeepSeek с автоматической корректировкой длины"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Первый запрос
        response = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Перепиши эту новость в формате на 600-650 символов. Сохрани ВСЕ важные факты, цифры, даты, имена. НЕ ОБРЕЗАЙ текст, а ПЕРЕПИШИ его.\n\n{text}"}
                ],
                "temperature": 0.7,
                "max_tokens": 1200
            }
        )
        result = response.json()
        
        if "error" in result:
            return result
        
        content = result["choices"][0]["message"]["content"]
        
        # Извлекаем текст
        body_match = re.search(r'(?:ТЕКСТ:|Текст:)\s*(.+?)$', content, re.IGNORECASE | re.DOTALL)
        if body_match:
            body = body_match.group(1).strip()
        else:
            lines = content.strip().split('\n')
            body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else content.strip()
        
        char_count = len(body)
        
        # Если длина неправильная, делаем второй запрос с корректировкой
        if char_count < 550 or char_count > 700:
            correction = "СДЕЛАЙ ТЕКСТ ДЛИННЕЕ (600-650 символов). Добавь больше деталей." if char_count < 550 else "СДЕЛАЙ ТЕКСТ КОРОЧЕ (600-650 символов). Убери лишние слова, но сохрани все важные факты."
            
            response2 = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"{correction}\n\nПерепиши эту новость заново, соблюдая длину 600-650 символов. Сохрани все важные факты, цифры, даты:\n\n{original_text if original_text else text}"}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1200
                }
            )
            result = response2.json()
        
        return result


# ==================== ОБРАБОТЧИКИ ИИ ====================
async def ai_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста через DeepSeek AI"""
    query = update.callback_query
    await query.answer()
    
    if not DEEPSEEK_API_KEY:
        await query.message.reply_text("❌ API DeepSeek не настроен. Добавьте DEEPSEEK_API_KEY в переменные окружения.")
        return
    
    body = context.user_data.get("temp_body", "")
    original_body = context.user_data.get("original_body", body)
    
    if not body:
        await query.message.reply_text("❌ Нет текста для обработки")
        return
    
    await query.message.reply_text("🤖 Перерабатываю текст через DeepSeek AI (600-650 символов)...")
    
    try:
        result = await call_deepseek_with_fix(DEEPSEEK_PROMPT, body, original_body)
        
        if "error" in result:
            await query.message.reply_text(f"❌ Ошибка DeepSeek: {result['error'].get('message', 'Unknown error')}")
            return
        
        content = result["choices"][0]["message"]["content"]
        
        # Парсим ответ
        title = ""
        new_body = ""
        
        if "ЗАГОЛОВОК:" in content.upper() and "ТЕКСТ:" in content.upper():
            title_match = re.search(r'(?:ЗАГОЛОВОК:|Заголовок:)\s*(.+?)(?=(?:ТЕКСТ:|$))', content, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()
            
            body_match = re.search(r'(?:ТЕКСТ:|Текст:)\s*(.+?)$', content, re.IGNORECASE | re.DOTALL)
            if body_match:
                new_body = body_match.group(1).strip()
        else:
            lines = content.strip().split('\n')
            if len(lines) > 0 and len(lines[0]) < 100:
                title = lines[0].replace('Заголовок:', '').replace('ЗАГОЛОВОК:', '').strip()
                new_body = '\n'.join(lines[1:]).strip()
            else:
                new_body = content.strip()
        
        if not new_body:
            new_body = content.strip()
        
        if not title and new_body:
            title = new_body[:50] + "..."
        
        char_count = len(new_body)
        
        context.user_data["temp_title"] = title
        context.user_data["temp_body"] = new_body
        
        # Определяем статус длины
        if 600 <= char_count <= 650:
            status = "✅ Отлично!"
        elif 550 <= char_count < 600:
            status = "⚠️ Немного коротковат"
        elif 650 < char_count <= 700:
            status = "⚠️ Немного длинноват"
        else:
            status = "❌ Не соответствует"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Использовать", callback_data="use_ai_result")],
            [InlineKeyboardButton("🔄 Переделать", callback_data="ai_reprocess")],
            [InlineKeyboardButton("✏️ Редактировать вручную", callback_data="edit_manually")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_input")]
        ])
        
        await query.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{new_body}\n\n"
            f"📊 *Длина текста:* {char_count} символов {status}\n\n"
            f"Что делаем дальше?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        await query.message.reply_text(f"❌ Ошибка при обработке: {e}")


async def ai_reprocess_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторная обработка с новым запросом"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_custom_request"] = True
    
    await query.message.reply_text(
        "📝 *Напишите ваш запрос для переделки текста*\n\n"
        "Примеры:\n"
        "• Сделай заголовок броским\n"
        "• Сделай текст 650 символов\n"
        "• Сделай более официальным\n"
        "• Добавь больше фактов и цифр\n"
        "• Сохрани все даты и имена\n\n"
        "Или отправьте /cancel для отмены.",
        parse_mode="Markdown"
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def handle_custom_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кастомного запроса к DeepSeek"""
    if not context.user_data.get("waiting_custom_request"):
        return
    
    custom_request = update.message.text
    context.user_data["waiting_custom_request"] = False
    
    body = context.user_data.get("temp_body", "")
    original_body = context.user_data.get("original_body", body)
    
    if not body:
        await update.message.reply_text("❌ Нет текста для обработки")
        return
    
    prompt = f"{DEEPSEEK_PROMPT}\n\nДополнительные требования пользователя: {custom_request}\n\nПеределай новость согласно этим требованиям, сохраняя длину 600-650 символов."
    
    await update.message.reply_text("🤖 Обрабатываю с новым запросом...")
    
    try:
        result = await call_deepseek_with_fix(prompt, body, original_body)
        
        if "error" in result:
            await update.message.reply_text(f"❌ Ошибка DeepSeek: {result['error'].get('message', 'Unknown error')}")
            return
        
        content = result["choices"][0]["message"]["content"]
        
        title = ""
        new_body = ""
        
        if "ЗАГОЛОВОК:" in content.upper() and "ТЕКСТ:" in content.upper():
            title_match = re.search(r'(?:ЗАГОЛОВОК:|Заголовок:)\s*(.+?)(?=(?:ТЕКСТ:|$))', content, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()
            
            body_match = re.search(r'(?:ТЕКСТ:|Текст:)\s*(.+?)$', content, re.IGNORECASE | re.DOTALL)
            if body_match:
                new_body = body_match.group(1).strip()
        else:
            lines = content.strip().split('\n')
            if len(lines) > 0 and len(lines[0]) < 100:
                title = lines[0].strip()
                new_body = '\n'.join(lines[1:]).strip()
            else:
                new_body = content.strip()
        
        if not new_body:
            new_body = content.strip()
        
        if not title and new_body:
            title = new_body[:50] + "..."
        
        char_count = len(new_body)
        
        context.user_data["temp_title"] = title
        context.user_data["temp_body"] = new_body
        
        if 600 <= char_count <= 650:
            status = "✅ Отлично!"
        elif 550 <= char_count < 600:
            status = "⚠️ Немного коротковат"
        elif 650 < char_count <= 700:
            status = "⚠️ Немного длинноват"
        else:
            status = "❌ Не соответствует"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Использовать", callback_data="use_ai_result")],
            [InlineKeyboardButton("🔄 Переделать", callback_data="ai_reprocess")],
            [InlineKeyboardButton("✏️ Редактировать вручную", callback_data="edit_manually")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_input")]
        ])
        
        await update.message.reply_text(
            f"✅ *Текст обработан!*\n\n"
            f"📰 *Заголовок:* {title}\n\n"
            f"📝 *Текст:*\n{new_body}\n\n"
            f"📊 *Длина текста:* {char_count} символов {status}\n\n"
            f"Что делаем дальше?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def use_ai_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Использовать результат обработки ИИ"""
    query = update.callback_query
    await query.answer()
    
    title = context.user_data.get("temp_title", "")
    body = context.user_data.get("temp_body", "")
    
    if not title or not body:
        await query.message.reply_text("❌ Нет данных. Начните заново.")
        return
    
    context.user_data["title"] = title
    context.user_data["body"] = body
    context.user_data["state"] = "ready_to_create"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Создать сторис", callback_data="create_story_final")],
        [InlineKeyboardButton("✏️ Редактировать заголовок", callback_data="edit_title_manual")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_body_manual")],
        [InlineKeyboardButton("🤖 Обработать снова", callback_data="ai_process")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_input")]
    ])
    
    await query.message.reply_text(
        f"✅ *Готово!*\n\n"
        f"📰 *Заголовок:* {title}\n\n"
        f"📝 *Текст:*\n{body}\n\n"
        f"Нажмите 'Создать сторис' для генерации изображения.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def edit_manually_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование вручную"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_edit_body"] = True
    await query.message.reply_text(
        "✏️ Отправьте новый текст (макс. 900 символов)\n\n"
        "Или /cancel для отмены."
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def edit_title_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование заголовка вручную"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_edit_title"] = True
    await query.message.reply_text(
        "✏️ Отправьте новый заголовок\n\n"
        "Или /cancel для отмены."
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def edit_body_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование текста вручную"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["waiting_edit_body"] = True
    await query.message.reply_text(
        "✏️ Отправьте новый текст (макс. 900 символов)\n\n"
        "Или /cancel для отмены."
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def handle_manual_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ручного редактирования"""
    if context.user_data.get("waiting_edit_title"):
        new_title = update.message.text.strip()
        if new_title and len(new_title) >= 3:
            context.user_data["title"] = new_title
            context.user_data["waiting_edit_title"] = False
            await update.message.reply_text(f"✅ Заголовок обновлён:\n\n{new_title}")
            
            title = context.user_data.get("title", "")
            body = context.user_data.get("body", "")
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎨 Создать сторис", callback_data="create_story_final")],
                [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_body_manual")],
                [InlineKeyboardButton("🤖 Обработать ИИ", callback_data="ai_process")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_input")]
            ])
            
            await update.message.reply_text(
                f"📰 *Заголовок:* {title}\n\n"
                f"📝 *Текст:*\n{body}\n\n"
                f"Что дальше?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("❌ Заголовок слишком короткий (минимум 3 символа)")
        return
    
    if context.user_data.get("waiting_edit_body"):
        new_body = update.message.text.strip()
        if new_body:
            if len(new_body) > 900:
                await update.message.reply_text("❌ Текст слишком длинный (макс. 900 символов)")
                return
            context.user_data["body"] = new_body
            context.user_data["waiting_edit_body"] = False
            await update.message.reply_text(f"✅ Текст обновлён!\n\n{new_body[:200]}...")
            
            title = context.user_data.get("title", "")
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎨 Создать сторис", callback_data="create_story_final")],
                [InlineKeyboardButton("✏️ Редактировать заголовок", callback_data="edit_title_manual")],
                [InlineKeyboardButton("🤖 Обработать ИИ", callback_data="ai_process")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_input")]
            ])
            
            await update.message.reply_text(
                f"📰 *Заголовок:* {title}\n\n"
                f"📝 *Текст:*\n{new_body}\n\n"
                f"Что дальше?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text("❌ Текст не может быть пустым")
        return


async def create_story_final_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание сторис из подготовленных данных"""
    query = update.callback_query
    await query.answer()
    
    photo = context.user_data.get("photo")
    title = context.user_data.get("title")
    body = context.user_data.get("body")
    
    if not photo or not title or not body:
        await query.message.reply_text("❌ Не хватает данных. Нажмите /start.")
        return
    
    msg = await query.message.reply_text("🎨 Создаю сторис...")
    
    try:
        result = create_story(photo, title, body)
        result.name = "fider_story.png"
        
        await query.message.reply_photo(
            photo=result,
            caption="✨ Готово!\n\nfider.by"
        )
        
        context.user_data.clear()
        context.user_data["state"] = "waiting_photo"
        
    except ValueError as e:
        await query.message.reply_text(f"❌ {str(e)}\n\nОтправьте новый текст.")
        return
    except Exception as e:
        logger.exception(e)
        await query.message.reply_text(f"❌ Ошибка: {str(e)}")
        context.user_data.clear()
        context.user_data["state"] = "waiting_photo"
    
    try:
        await msg.delete()
    except:
        pass


async def back_to_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к вводу текста"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["state"] = "waiting_body"
    await query.message.reply_text(
        "✏️ Отправьте основной текст (макс. 900 символов)\n\n"
        "Или нажмите /cancel для отмены."
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def continue_without_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продолжить без обработки ИИ"""
    query = update.callback_query
    await query.answer()
    
    body = context.user_data.get("original_body", "")
    
    context.user_data["body"] = body
    context.user_data["state"] = "ready_to_create"
    
    title = context.user_data.get("title", "")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Создать сторис", callback_data="create_story_final")],
        [InlineKeyboardButton("✏️ Редактировать заголовок", callback_data="edit_title_manual")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_body_manual")],
        [InlineKeyboardButton("🤖 Обработать ИИ", callback_data="ai_process")]
    ])
    
    await query.message.reply_text(
        f"📰 *Заголовок:* {title}\n\n"
        f"📝 *Текст:*\n{body}\n\n"
        f"Текст {'можно сократить через ИИ до 600-650 символов' if len(body) > 650 else 'хорошей длины'}.\n\n"
        f"Что делаем?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    
    try:
        await query.message.delete()
    except:
        pass


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    context.user_data.clear()
    await update.message.reply_text("✅ Отменено. Нажмите /start для начала.")


# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = "waiting_photo"

    ai_status = "✅ Доступен" if DEEPSEEK_API_KEY else "❌ Не настроен"
    
    await update.message.reply_text(
        f"🟣 Бот сторис Fider.by\n\n"
        f"✨ ДОСТУПНЫЕ ФУНКЦИИ:\n"
        f"1. Отправь фото\n"
        f"2. Отправь заголовок\n"
        f"3. Отправь основной текст (максимум 900 символов)\n\n"
        f"🤖 DeepSeek AI: {ai_status}\n"
        f"После отправки текста нажми кнопку 'Обработать через ИИ'\n"
        f"ИИ перепишет текст в новостной формат 600-650 символов\n\n"
        f"Я соберу готовую сторис 9:16."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "waiting_photo":
        await update.message.reply_text("Нажми /start и отправь фото заново.")
        return

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        context.user_data["photo"] = bytes(photo_bytes)
        context.user_data["state"] = "waiting_title"

        await update.message.reply_text("✅ Фото получил. Теперь отправь заголовок.")
    except TimedOut:
        await update.message.reply_text("⏱️ Превышено время ожидания. Попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text("❌ Ошибка при загрузке фото.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "waiting_photo":
        await update.message.reply_text("Нажми /start и отправь фото заново.")
        return

    doc = update.message.document

    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text("Нужен файл изображения.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        photo_bytes = await file.download_as_bytearray()

        context.user_data["photo"] = bytes(photo_bytes)
        context.user_data["state"] = "waiting_title"

        await update.message.reply_text("✅ Фото получил в хорошем качестве. Теперь отправь заголовок.")
    except TimedOut:
        await update.message.reply_text("⏱️ Превышено время ожидания. Попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text("❌ Ошибка при загрузке файла.")


async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка заголовка"""
    if context.user_data.get("state") != "waiting_title":
        return
    
    title = update.message.text.strip()

    if len(title) < 3:
        await update.message.reply_text("❌ Заголовок слишком короткий (минимум 3 символа).")
        return

    context.user_data["title"] = title
    context.user_data["state"] = "waiting_body"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Продолжить без обработки", callback_data="continue_without_ai")],
        [InlineKeyboardButton("🤖 Обработать через ИИ", callback_data="ai_process")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_title")]
    ])
    
    await update.message.reply_text(
        f"✅ Заголовок сохранён:\n\n{title}\n\n"
        f"📝 Теперь отправь основной текст (макс. 900 символов)\n\n"
        f"Или выбери действие:",
        reply_markup=keyboard
    )


async def back_to_title_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться к вводу заголовка"""
    query = update.callback_query
    await query.answer()
    
    context.user_data["state"] = "waiting_title"
    await query.message.reply_text("✏️ Отправьте новый заголовок.")
    
    try:
        await query.message.delete()
    except:
        pass


async def handle_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка основного текста"""
    if context.user_data.get("state") != "waiting_body":
        return
    
    body = update.message.text.strip()
    
    if len(body) < 20:
        await update.message.reply_text("❌ Текст слишком короткий (минимум 20 символов).")
        return
    
    if len(body) > 900:
        await update.message.reply_text("❌ Текст слишком длинный (макс. 900 символов).")
        return
    
    context.user_data["original_body"] = body
    context.user_data["temp_body"] = body
    context.user_data["state"] = "ready_to_create"
    
    title = context.user_data.get("title", "")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Создать сторис", callback_data="create_story_final")],
        [InlineKeyboardButton("🤖 Обработать через ИИ", callback_data="ai_process")],
        [InlineKeyboardButton("✏️ Редактировать заголовок", callback_data="edit_title_manual")],
        [InlineKeyboardButton("✏️ Редактировать текст", callback_data="edit_body_manual")]
    ])
    
    await update.message.reply_text(
        f"✅ *Всё готово!*\n\n"
        f"📰 *Заголовок:* {title}\n\n"
        f"📝 *Текст:*\n{body}\n\n"
        f"Текст {'можно сократить через ИИ до 600-650 символов' if len(body) > 650 else 'хорошей длины'}.\n\n"
        f"Что делаем?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)


def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(60)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(120)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_body))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_edit))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_request))
    
    # Callback кнопки
    app.add_handler(CallbackQueryHandler(continue_without_ai_callback, pattern="continue_without_ai"))
    app.add_handler(CallbackQueryHandler(back_to_title_callback, pattern="back_to_title"))
    app.add_handler(CallbackQueryHandler(back_to_input_callback, pattern="back_to_input"))
    app.add_handler(CallbackQueryHandler(ai_process_callback, pattern="ai_process"))
    app.add_handler(CallbackQueryHandler(ai_reprocess_callback, pattern="ai_reprocess"))
    app.add_handler(CallbackQueryHandler(use_ai_result_callback, pattern="use_ai_result"))
    app.add_handler(CallbackQueryHandler(edit_manually_callback, pattern="edit_manually"))
    app.add_handler(CallbackQueryHandler(edit_title_manual_callback, pattern="edit_title_manual"))
    app.add_handler(CallbackQueryHandler(edit_body_manual_callback, pattern="edit_body_manual"))
    app.add_handler(CallbackQueryHandler(create_story_final_callback, pattern="create_story_final"))

    print("✅ FIDER STORY BOT STARTED")
    if DEEPSEEK_API_KEY:
        print("🤖 DeepSeek AI подключен и готов к работе")
        print("📏 Текст будет перерабатываться в формат 600-650 символов")
    else:
        print("⚠️ DeepSeek AI не настроен (добавьте DEEPSEEK_API_KEY)")

    app.run_polling(
        drop_pending_updates=True,
        poll_interval=2.0,
        timeout=60,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
