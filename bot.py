import os
import requests
import logging
import re
import time
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
WP_URL = os.getenv('WP_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_PASSWORD = os.getenv('WP_PASSWORD')

# WordPress API
WP_API_URL = f"{WP_URL}/wp-json/wp/v2"
WP_MEDIA_URL = f"{WP_URL}/wp-json/wp/v2/media"

# Flask приложение
app = Flask(__name__)

# Создаём сессию для постоянного соединения с WordPress
wp_session = requests.Session()

def extract_title_and_content(text):
    """Извлечение заголовка из текста"""
    if not text:
        return "Новый пост из Telegram", ""
    
    lines = text.strip().split('\n')
    title = lines[0].strip() if lines else "Новый пост"
    
    if len(title) > 180:
        title = title[:177] + "..."
    
    content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
    return title, content

def format_content_for_wp(text):
    """Форматирование контента для WordPress"""
    if not text:
        return ""
    
    paragraphs = text.split('\n')
    formatted = []
    
    for para in paragraphs:
        para = para.strip()
        if para:
            # Конвертируем ссылки
            para = re.sub(r'(https?://[^\s]+)', r'<a href="\1">\1</a>', para)
            # Конвертируем **жирный**
            para = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            # Конвертируем *курсив*
            para = re.sub(r'\*(.+?)\*', r'<em>\1</em>', para)
            formatted.append(f'<p>{para}</p>')
    
    return '\n'.join(formatted)

def download_and_upload_photo(file_id):
    """Синхронная загрузка фото из Telegram в WordPress"""
    try:
        # Получаем URL фото через Telegram API
        get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        file_response = requests.get(get_file_url, params={'file_id': file_id}, timeout=30)
        
        if file_response.status_code != 200:
            logger.error(f"Ошибка getFile: {file_response.status_code}")
            return None
        
        result = file_response.json().get('result')
        if not result:
            logger.error("Не получен result от Telegram")
            return None
        
        file_path = result.get('file_path')
        if not file_path:
            logger.error("Не получен file_path")
            return None
        
        # Скачиваем фото
        photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        logger.info(f"Скачивание фото: {photo_url[:50]}...")
        
        # Заголовки для скачивания
        download_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        photo_response = requests.get(photo_url, headers=download_headers, timeout=60)
        
        if photo_response.status_code != 200:
            logger.error(f"Ошибка скачивания фото: {photo_response.status_code}")
            return None
        
        # Определяем тип контента
        content_type = photo_response.headers.get('content-type', 'image/jpeg')
        
        # Заголовки для WordPress
        wp_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="telegram_photo_{int(time.time())}.jpg"'
        }
        
        # Загружаем в WordPress через сессию
        wp_response = wp_session.post(
            WP_MEDIA_URL,
            auth=(WP_USERNAME, WP_PASSWORD),
            headers=wp_headers,
            data=photo_response.content,
            timeout=60
        )
        
        if wp_response.status_code == 201:
            media_id = wp_response.json()['id']
            logger.info(f"✅ Фото загружено, ID: {media_id}")
            return media_id
        else:
            logger.error(f"Ошибка WP при загрузке фото: {wp_response.status_code}")
            logger.error(f"Ответ: {wp_response.text[:200]}")
            if wp_response.status_code == 401:
                logger.error("❌ Ошибка авторизации! Проверь WP_USERNAME и WP_PASSWORD")
            return None
            
    except requests.exceptions.Timeout:
        logger.error("Таймаут при загрузке фото")
        return None
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        return None

def create_wp_draft(title, content, media_id=None):
    """Создание черновика в WordPress"""
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft',
    }
    
    if media_id:
        post_data['featured_media'] = media_id
    
    try:
        # Полные заголовки как у реального браузера
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/json',
            'Origin': WP_URL,
            'Referer': WP_URL,
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        }
        
        logger.info(f"Отправка запроса в WordPress: {WP_API_URL}/posts")
        
        # Используем сессию для сохранения соединения
        response = wp_session.post(
            f"{WP_API_URL}/posts",
            auth=(WP_USERNAME, WP_PASSWORD),
            json=post_data,
            headers=headers,
            timeout=60
        )
        
        if response.status_code == 201:
            post_link = response.json()['link']
            logger.info(f"✅ Черновик создан: {post_link}")
            return True
        else:
            logger.error(f"Ошибка создания поста: {response.status_code}")
            logger.error(f"Ответ сервера: {response.text[:300]}")
            
            if response.status_code == 401:
                logger.error("❌ Ошибка авторизации! Проверь:")
                logger.error("   - WP_USERNAME (логин, не email)")
                logger.error("   - WP_PASSWORD (пароль приложения, не обычный пароль)")
            elif response.status_code == 403:
                logger.error("❌ Доступ запрещён. Проверь права пользователя в WordPress")
            elif response.status_code == 404:
                logger.error("❌ API не найден. Проверь WP_URL и что REST API включен")
            
            return False
            
    except requests.exceptions.Timeout:
        logger.error("❌ Таймаут подключения к WordPress")
        logger.error("   Возможные причины:")
        logger.error("   - Хостинг блокирует IP Render")
        logger.error("   - Медленный ответ сервера")
        logger.error("   - Проблемы с сетью")
        return False
    except requests.exceptions.ConnectionError as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        logger.error("   Проверь:")
        logger.error("   - Доступен ли сайт: " + WP_URL)
        logger.error("   - Не блокирует ли хостинг запросы из Render")
        logger.error("   - Нет ли проблем с SSL сертификатом")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False

async def handle_channel_post(update: Update, context):
    """Обработка постов из канала"""
    try:
        logger.info("=" * 60)
        logger.info("🔍 НОВЫЙ ПОСТ ПОЛУЧЕН")
        
        channel_post = update.channel_post
        if not channel_post:
            logger.warning("⚠️ Нет channel_post в update")
            return
        
        real_chat_id = channel_post.chat_id
        logger.info(f"📨 ID сообщения: {channel_post.message_id}")
        logger.info(f"🔍 ID канала: {real_chat_id}")
        
        text = channel_post.caption or channel_post.text or ""
        title, content_text = extract_title_and_content(text)
        logger.info(f"📌 Заголовок: {title[:60]}...")
        logger.info(f"📏 Длина заголовка: {len(title)} символов")
        
        # Обработка фото
        media_id = None
        if channel_post.photo:
            try:
                photo = channel_post.photo[-1]
                logger.info(f"📸 Найдено фото, обработка...")
                media_id = download_and_upload_photo(photo.file_id)
                if media_id:
                    logger.info(f"✅ Фото успешно загружено (ID: {media_id})")
                else:
                    logger.warning("⚠️ Фото не загружено, продолжаем без фото")
            except Exception as e:
                logger.error(f"Ошибка при обработке фото: {e}")
        
        # Форматируем контент
        formatted_content = format_content_for_wp(content_text)
        
        # Добавляем информацию об источнике
        if channel_post.date:
            source_info = f'<p><small>📱 Источник: Telegram | {channel_post.date.strftime("%d.%m.%Y %H:%M")}</small></p>'
            formatted_content += source_info
        
        # Создаем черновик
        logger.info("📤 Отправка в WordPress...")
        success = create_wp_draft(title, formatted_content, media_id)
        
        if success:
            logger.info(f"✨ ГОТОВО! Пост сохранен как черновик")
        else:
            logger.error("❌ НЕ УДАЛОСЬ создать черновик")
            
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка обработки: {e}")
        logger.exception("Детали ошибки:")

# Создаем приложение Telegram
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Добавляем обработчик (без фильтрации по каналу для отладки)
application.add_handler(MessageHandler(
    (filters.TEXT | filters.PHOTO | filters.CAPTION),
    handle_channel_post
))
logger.info("✅ Обработчик сообщений добавлен")

# Настраиваем сессию для WordPress
wp_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint для вебхуков Telegram"""
    try:
        json_data = request.get_json(force=True)
        logger.info("🔔 Получен вебхук от Telegram")
        
        update = Update.de_json(json_data, application.bot)
        
        # Обрабатываем update
        import asyncio
        asyncio.run(application.process_update(update))
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check для Render"""
    return jsonify({'status': 'healthy', 'service': 'Telegram to WordPress Bot'})

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'status': 'Bot is running',
        'message': 'Telegram to WordPress Bot',
        'endpoints': {
            'webhook': '/webhook (POST)',
            'health': '/health (GET)'
        }
    })

if __name__ == '__main__':
    render_url = os.getenv('RENDER_EXTERNAL_URL')
    
    if not render_url:
        logger.error("❌ RENDER_EXTERNAL_URL не задан!")
        render_url = f"http://localhost:{os.getenv('PORT', 8000)}"
    
    webhook_url = f"{render_url}/webhook"
    
    logger.info(f"🚀 ЗАПУСК БОТА...")
    logger.info(f"🔗 Вебхук URL: {webhook_url}")
    logger.info(f"🌐 WordPress URL: {WP_URL}")
    logger.info(f"👤 WordPress Username: {WP_USERNAME}")
    
    # Настройка вебхука
    async def setup():
        await application.initialize()
        await application.bot.delete_webhook()
        await application.bot.set_webhook(url=webhook_url)
        logger.info("✅ Вебхук установлен успешно")
    
    import asyncio
    asyncio.run(setup())
    
    port = int(os.getenv('PORT', 8000))
    logger.info(f"🎯 Сервер запущен на порту {port}")
    app.run(host='0.0.0.0', port=port)
