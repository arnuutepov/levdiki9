import os
import io
import tempfile
import sqlite3
from PIL import Image, ImageFilter
import cv2
import numpy as np
import fitz  # pymupdf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import asyncio

# Параметры по умолчанию
DEFAULT_BLUR = 1
DEFAULT_SKEW = 0
DEFAULT_NOISE = 2
DEFAULT_QUALITY = 50
DEFAULT_DPI = 150

# НОВЫЕ ПАРАМЕТРЫ ДЛЯ БОЛЬШИХ ФАЙЛОВ
MAX_FILE_SIZE_MB = 50  # Максимальный размер файла
CHUNK_SIZE = 4096 * 4096 * 40  # 10 МБ чанки для скачивания
DOWNLOAD_TIMEOUT = 600  # 10 минут на скачивание

class Database:
    def __init__(self, db_path='bot_settings.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Создаём таблицы"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица настроек пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                blur INTEGER DEFAULT 2,
                skew INTEGER DEFAULT 5,
                noise INTEGER DEFAULT 10,
                quality INTEGER DEFAULT 50,
                dpi INTEGER DEFAULT 150,
                filename_prefix TEXT DEFAULT 'corrupted_',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица истории обработки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processing_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                original_filename TEXT,
                output_filename TEXT,
                original_size INTEGER,
                output_size INTEGER,
                pages_count INTEGER,
                blur INTEGER,
                skew INTEGER,
                noise INTEGER,
                quality INTEGER,
                dpi INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_settings(self, user_id):
        """Получаем настройки пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            settings = {
                'blur': result[1],
                'skew': result[2],
                'noise': result[3],
                'quality': result[4],
                'dpi': result[5],
                'filename_prefix': result[6]
            }
        else:
            # Создаём дефолтные настройки
            settings = {
                'blur': DEFAULT_BLUR,
                'skew': DEFAULT_SKEW,
                'noise': DEFAULT_NOISE,
                'quality': DEFAULT_QUALITY,
                'dpi': DEFAULT_DPI,
                'filename_prefix': 'corrupted_'
            }
            cursor.execute('''
                INSERT INTO user_settings (user_id, blur, skew, noise, quality, dpi, filename_prefix)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, settings['blur'], settings['skew'], settings['noise'], 
                  settings['quality'], settings['dpi'], settings['filename_prefix']))
            conn.commit()
        
        conn.close()
        return settings
    
    def update_settings(self, user_id, **kwargs):
        """Обновляем настройки"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        updates = []
        values = []
        
        for key, value in kwargs.items():
            if key in ['blur', 'skew', 'noise', 'quality', 'dpi', 'filename_prefix']:
                updates.append(f"{key} = ?")
                values.append(value)
        
        if updates:
            values.append(user_id)
            query = f"UPDATE user_settings SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
            cursor.execute(query, values)
            conn.commit()
        
        conn.close()
    
    def save_history(self, user_id, original_filename, output_filename, original_size, 
                     output_size, pages_count, blur, skew, noise, quality, dpi):
        """Сохраняем историю обработки"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO processing_history 
            (user_id, original_filename, output_filename, original_size, output_size, 
             pages_count, blur, skew, noise, quality, dpi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, original_filename, output_filename, original_size, output_size,
              pages_count, blur, skew, noise, quality, dpi))
        
        conn.commit()
        conn.close()
    
    def get_user_stats(self, user_id):
        """Получаем статистику пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*), SUM(pages_count), AVG(output_size), MAX(created_at)
            FROM processing_history WHERE user_id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        return {
            'total_files': result[0] or 0,
            'total_pages': result[1] or 0,
            'avg_size': int(result[2]) if result[2] else 0,
            'last_processed': result[3]
        }

class PDFCorruptor:
    def __init__(self, blur=2, skew=5, noise=10, quality=50, dpi=150, progress_callback=None):
        self.blur_amount = blur
        self.skew_amount = skew
        self.noise_amount = noise
        self.quality = quality
        self.dpi = dpi
        self.progress_callback = progress_callback
    
    def add_blur(self, image):
        """Добавляет размытие к изображению"""
        if self.blur_amount == 0:
            return image
        blur_radius = max(1, self.blur_amount)
        return image.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    
    def add_skew(self, image):
        """Добавляет перекос к изображению"""
        if self.skew_amount == 0:
            return image
        
        img_array = np.array(image)
        h, w = img_array.shape[:2]
        
        angle = self.skew_amount
        center = (w // 2, h // 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        
        skewed = cv2.warpAffine(img_array, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)
        return Image.fromarray(skewed)
    
    def add_noise(self, image):
        """Добавляет шум к изображению"""
        if self.noise_amount == 0:
            return image
        
        img_array = np.array(image, dtype=np.float32)
        noise = np.random.normal(0, self.noise_amount, img_array.shape)
        noisy = np.clip(img_array + noise, 0, 255).astype(np.uint8)
        
        return Image.fromarray(noisy)
    
    def process_page(self, image):
        """Обрабатывает одну страницу PDF"""
        image = self.add_blur(image)
        image = self.add_skew(image)
        image = self.add_noise(image)
        return image
    
    async def process_pdf(self, pdf_path, output_path):
        """Обрабатывает весь PDF файл со сжатием + прогресс"""
        try:
            pdf_doc = fitz.open(pdf_path)
            output_doc = fitz.open()
            
            total_pages = len(pdf_doc)
            
            # Матрица масштабирования на основе DPI
            zoom = self.dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            
            for page_num in range(total_pages):
                page = pdf_doc[page_num]
                
                # Рендерим страницу с заданным DPI
                pix = page.get_pixmap(matrix=mat)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                
                # Применяем эффекты
                processed_image = self.process_page(image)
                
                # КРИТИЧНО: освобождаем память от оригинала
                del image
                
                # Сжимаем изображение через JPEG
                img_bytes = io.BytesIO()
                processed_image.save(img_bytes, format='JPEG', quality=self.quality, optimize=True)
                img_bytes.seek(0)
                
                # КРИТИЧНО: освобождаем память от processed_image
                del processed_image
                
                # Добавляем в новый документ
                new_page = output_doc.new_page(width=pix.width, height=pix.height)
                new_page.insert_image(fitz.Rect(0, 0, pix.width, pix.height), stream=img_bytes.getvalue())
                
                # КРИТИЧНО: освобождаем память
                img_bytes.close()
                del pix
                
                # Обновляем прогресс (без await внутри синхронного процесса)
                if self.progress_callback:
                    progress = int((page_num + 1) / total_pages * 100)
                    # Запускаем в event loop
                    try:
                        await self.progress_callback(progress, page_num + 1, total_pages)
                    except:
                        pass  # Игнорируем ошибки прогресса
                
                # Даем event loop возможность обработать другие задачи
                await asyncio.sleep(0)
            
            # Сохраняем с дополнительным сжатием
            output_doc.save(output_path, garbage=4, deflate=True, clean=True)
            
            pages_count = len(pdf_doc)
            pdf_doc.close()
            output_doc.close()
            
            return True, pages_count
            
        except Exception as e:
            print(f"Ошибка при обработке PDF: {e}")
            import traceback
            traceback.print_exc()
            return False, 0

# Глобальная база данных
db = Database()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало работы с ботом"""
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)
    stats = db.get_user_stats(user_id)
    
    await update.message.reply_text(
        f"👋 Привет! Я бот для 'шакаления' PDF файлов.\n\n"
        f"📊 Твоя статистика:\n"
        f"• Обработано файлов: {stats['total_files']}\n"
        f"• Всего страниц: {stats['total_pages']}\n\n"
        f"⚙️ Текущие настройки:\n"
        f"🔲 Блюр: {settings['blur']}\n"
        f"↗️ Перекос: {settings['skew']}°\n"
        f"⚪ Шум: {settings['noise']}\n"
        f"📦 Качество: {settings['quality']}%\n"
        f"📐 DPI: {settings['dpi']}\n"
        f"📝 Префикс: {settings['filename_prefix']}\n\n"
        f"📤 Отправь мне PDF файл для обработки!\n"
        f"💪 Поддержка файлов до {MAX_FILE_SIZE_MB} МБ"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем PDF файл от пользователя"""
    user_id = update.effective_user.id
    file = await update.message.document.get_file()
    
    if not update.message.document.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("❌ Пожалуйста, отправь PDF файл!")
        return
    
    # Проверяем размер файла
    file_size_mb = update.message.document.file_size / 1024 / 1024
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(
            f"❌ Файл слишком большой!\n"
            f"📦 Твой файл: {file_size_mb:.1f} МБ\n"
            f"📏 Максимум: {MAX_FILE_SIZE_MB} МБ"
        )
        return
    
    # Сохраняем информацию о файле
    context.user_data['filename'] = update.message.document.file_name
    context.user_data['file_size'] = update.message.document.file_size
    
    # Показываем статус скачивания для больших файлов
    if file_size_mb > 10:
        status_msg = await update.message.reply_text(
            f"⬇️ Скачиваю файл ({file_size_mb:.1f} МБ)...\n"
            f"Это может занять несколько минут ⏳"
        )
    else:
        status_msg = None
    
    try:
        # Скачиваем файл с увеличенным таймаутом
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as f:
            pdf_bytes = await asyncio.wait_for(
                file.download_as_bytearray(),
                timeout=DOWNLOAD_TIMEOUT
            )
            f.write(pdf_bytes)
            context.user_data['pdf_path'] = f.name
        
        if status_msg:
            await status_msg.delete()
        
        # Загружаем настройки пользователя
        settings = db.get_settings(user_id)
        
        # Показываем меню настроек
        await show_settings_menu(update, context, settings)
        
    except asyncio.TimeoutError:
        if status_msg:
            await status_msg.delete()
        await update.message.reply_text(
            "❌ Превышено время ожидания при скачивании файла!\n"
            "Попробуй файл поменьше или повтори позже."
        )
    except Exception as e:
        if status_msg:
            await status_msg.delete()
        await update.message.reply_text(f"❌ Ошибка при скачивании: {str(e)}")

async def show_settings_menu(update, context, settings):
    """Показываем меню настроек"""
    keyboard = [
        [
            InlineKeyboardButton(f"🔲 Блюр: {settings['blur']}", callback_data='blur'),
            InlineKeyboardButton(f"↗️ Перекос: {settings['skew']}", callback_data='skew'),
        ],
        [
            InlineKeyboardButton(f"⚪ Шум: {settings['noise']}", callback_data='noise'),
            InlineKeyboardButton(f"📦 Качество: {settings['quality']}%", callback_data='quality'),
        ],
        [
            InlineKeyboardButton(f"📐 DPI: {settings['dpi']}", callback_data='dpi'),
            InlineKeyboardButton(f"📝 Имя файла", callback_data='filename'),
        ],
        [
            InlineKeyboardButton("✅ Обработать!", callback_data='process'),
            InlineKeyboardButton("❌ Отмена", callback_data='cancel'),
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    file_size_mb = context.user_data.get('file_size', 0) / 1024 / 1024
    
    text = (
        f"⚙️ Настрой параметры обработки\n\n"
        f"📄 Файл: {context.user_data.get('filename', 'document.pdf')}\n"
        f"📦 Размер: {file_size_mb:.2f} МБ\n\n"
        f"Текущие значения:\n"
        f"🔲 Блюр: {settings['blur']}\n"
        f"↗️ Перекос: {settings['skew']}°\n"
        f"⚪ Шум: {settings['noise']}\n"
        f"📦 Качество JPEG: {settings['quality']}% (ниже = меньше размер)\n"
        f"📐 DPI: {settings['dpi']} (ниже = меньше размер)\n"
        f"📝 Префикс файла: {settings['filename_prefix']}\n"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def adjust_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регулируем параметр эффекта"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    param = query.data
    
    if param == 'blur':
        await query.edit_message_text(
            "🔲 Укажи значение блюра (0-20):\n"
            "0 = без блюра, 20 = максимальное размытие"
        )
        context.user_data['adjusting'] = 'blur'
        
    elif param == 'skew':
        await query.edit_message_text(
            "↗️ Укажи угол перекоса (-45 до 45 градусов):\n"
            "0 = без перекоса"
        )
        context.user_data['adjusting'] = 'skew'
        
    elif param == 'noise':
        await query.edit_message_text(
            "⚪ Укажи интенсивность шума (0-50):\n"
            "0 = без шума, 50 = максимальный шум"
        )
        context.user_data['adjusting'] = 'noise'
        
    elif param == 'quality':
        await query.edit_message_text(
            "📦 Укажи качество JPEG (10-100):\n"
            "10 = минимальный размер файла\n"
            "100 = максимальное качество\n"
            "Рекомендуется: 40-60"
        )
        context.user_data['adjusting'] = 'quality'
        
    elif param == 'dpi':
        await query.edit_message_text(
            "📐 Укажи DPI (72-300):\n"
            "72 = минимальный размер\n"
            "150 = оптимально\n"
            "300 = высокое качество"
        )
        context.user_data['adjusting'] = 'dpi'
        
    elif param == 'filename':
        await query.edit_message_text(
            "📝 Укажи префикс для имени файла:\n"
            "Например: 'bad_', 'corrupted_', 'low_quality_'\n"
            "Или отправь '0' чтобы убрать префикс"
        )
        context.user_data['adjusting'] = 'filename'
        
    elif param == 'process':
        await query.edit_message_text("⏳ Обрабатываю PDF... Пожалуйста, подожди...")
        context.user_data['progress_message'] = query.message
        await process_pdf_file(update, context)
        
    elif param == 'cancel':
        await query.edit_message_text("❌ Отменено. Отправь новый PDF файл или используй /start")
        if 'pdf_path' in context.user_data:
            try:
                os.unlink(context.user_data['pdf_path'])
            except:
                pass

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем текстовый ввод для параметров"""
    user_id = update.effective_user.id
    adjusting = context.user_data.get('adjusting')
    
    if not adjusting:
        return
    
    try:
        if adjusting == 'filename':
            prefix = update.message.text.strip()
            if prefix == '0':
                prefix = ''
            db.update_settings(user_id, filename_prefix=prefix)
            await update.message.reply_text(f"✅ Префикс установлен: '{prefix}'")
            
        else:
            value = int(update.message.text)
            
            if adjusting == 'blur':
                value = max(0, min(20, value))
                db.update_settings(user_id, blur=value)
                await update.message.reply_text(f"✅ Блюр установлен на {value}")
                
            elif adjusting == 'skew':
                value = max(-45, min(45, value))
                db.update_settings(user_id, skew=value)
                await update.message.reply_text(f"✅ Перекос установлен на {value}°")
                
            elif adjusting == 'noise':
                value = max(0, min(50, value))
                db.update_settings(user_id, noise=value)
                await update.message.reply_text(f"✅ Шум установлен на {value}")
                
            elif adjusting == 'quality':
                value = max(10, min(100, value))
                db.update_settings(user_id, quality=value)
                await update.message.reply_text(f"✅ Качество установлено на {value}%")
                
            elif adjusting == 'dpi':
                value = max(72, min(300, value))
                db.update_settings(user_id, dpi=value)
                await update.message.reply_text(f"✅ DPI установлен на {value}")
        
        context.user_data['adjusting'] = None
        
        settings = db.get_settings(user_id)
        await show_settings_menu(update, context, settings)
        
    except ValueError:
        await update.message.reply_text("❌ Укажи правильное значение!")

async def process_pdf_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем PDF файл с прогресс-баром"""
    user_id = update.effective_user.id
    pdf_path = context.user_data.get('pdf_path')
    original_filename = context.user_data.get('filename', 'document.pdf')
    original_size = context.user_data.get('file_size', 0)
    progress_msg = context.user_data.get('progress_message')
    
    last_update_time = [0]  # Для ограничения частоты обновлений
    
    async def progress_callback(progress, current_page, total_pages):
        """Обновляем прогресс обработки"""
        import time
        current_time = time.time()
        
        # Обновляем не чаще раза в 2 секунды
        if current_time - last_update_time[0] < 2 and progress < 100:
            return
        
        last_update_time[0] = current_time
        
        bar_length = 20
        filled = int(bar_length * progress / 100)
        bar = '█' * filled + '░' * (bar_length - filled)
        
        try:
            await progress_msg.edit_text(
                f"⏳ Обрабатываю PDF...\n\n"
                f"[{bar}] {progress}%\n"
                f"📄 Страница {current_page} из {total_pages}"
            )
        except:
            pass  # Игнорируем ошибки редактирования
    
    try:
        settings = db.get_settings(user_id)
        
        corrupted = PDFCorruptor(
            blur=settings['blur'],
            skew=settings['skew'],
            noise=settings['noise'],
            quality=settings['quality'],
            dpi=settings['dpi'],
            progress_callback=progress_callback
        )
        
        output_fd, output_path = tempfile.mkstemp(suffix='.pdf')
        os.close(output_fd)
        
        success, pages_count = await corrupted.process_pdf(pdf_path, output_path)
        
        if success:
            output_size = os.path.getsize(output_path)
            output_filename = settings['filename_prefix'] + original_filename
            
            # Для больших файлов показываем статус загрузки
            if output_size > 10 * 1024 * 1024:
                await progress_msg.edit_text("⬆️ Загружаю обработанный файл...")
            
            with open(output_path, 'rb') as f:
                compression_ratio = (1 - output_size / original_size) * 100 if original_size > 0 else 0
                
                caption = (
                    f"✅ Готово!\n\n"
                    f"📄 Оригинал: {original_size / 1024 / 1024:.2f} МБ\n"
                    f"📦 Результат: {output_size / 1024 / 1024:.2f} МБ\n"
                    f"📉 {'Сжатие' if compression_ratio > 0 else 'Увеличение'}: {abs(compression_ratio):.1f}%\n"
                    f"📑 Страниц: {pages_count}\n\n"
                    f"⚙️ Параметры:\n"
                    f"🔲 Блюр: {settings['blur']}\n"
                    f"↗️ Перекос: {settings['skew']}°\n"
                    f"⚪ Шум: {settings['noise']}\n"
                    f"📦 Качество: {settings['quality']}%\n"
                    f"📐 DPI: {settings['dpi']}"
                )
                
                await progress_msg.reply_document(
                    document=f,
                    filename=output_filename,
                    caption=caption,
                    write_timeout=300,
                    read_timeout=300,
                    connect_timeout=300
                )
            
            await progress_msg.delete()
            
            db.save_history(
                user_id, original_filename, output_filename,
                original_size, output_size, pages_count,
                settings['blur'], settings['skew'], settings['noise'],
                settings['quality'], settings['dpi']
            )
            
        else:
            await progress_msg.edit_text("❌ Ошибка при обработке PDF")
        
        if os.path.exists(output_path):
            os.unlink(output_path)
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
            
    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()
        await progress_msg.edit_text(f"❌ Ошибка: {str(e)}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем статистику пользователя"""
    user_id = update.effective_user.id
    stats = db.get_user_stats(user_id)
    
    await update.message.reply_text(
        f"📊 Твоя статистика:\n\n"
        f"📁 Обработано файлов: {stats['total_files']}\n"
        f"📑 Всего страниц: {stats['total_pages']}\n"
        f"📦 Средний размер: {stats['avg_size'] / 1024 / 1024:.2f} МБ\n"
        f"🕒 Последняя обработка: {stats['last_processed'] or 'Никогда'}"
    )

def main():
    """Запуск бота"""
    TOKEN = '8248836441:AAGH5-LsNsbJ03Cr7B1frIz1TI0SF5ZMiwU'
    
    # Создаем приложение с УВЕЛИЧЕННЫМИ таймаутами для больших файлов
    builder = Application.builder().token(TOKEN)
    builder = builder.connect_timeout(300.0).read_timeout(300.0).write_timeout(300.0)
    builder = builder.pool_timeout(300.0).get_updates_connect_timeout(300.0).get_updates_read_timeout(300.0)
    
    # Опционально: если используешь локальный Bot API сервер
    # LOCAL_API_URL = os.getenv('LOCAL_API_URL', None)
    # if LOCAL_API_URL:
    #     builder = builder.base_url(LOCAL_API_URL)
    #     builder = builder.base_file_url(LOCAL_API_URL)
    #     print(f"🌐 Используется локальный Bot API: {LOCAL_API_URL}")
    
    application = builder.build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(adjust_parameter))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    
    # Запускаем бота
    print("🤖 Бот запущен! Нажми Ctrl+C для выхода.")
    print(f"📦 Максимальный размер файла: {MAX_FILE_SIZE_MB} МБ")
    print("📊 База данных: bot_settings.db")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == '__main__':
    main()


# ============================================================
# 📖 ИНСТРУКЦИЯ ПО НАСТРОЙКЕ ДЛЯ БОЛЬШИХ ФАЙЛОВ (500 МБ)
# ============================================================

"""
🔧 ВАРИАНТ 1: Локальный Bot API Server (для 500 МБ)

1. Установка через Docker:
   docker pull aiogram/telegram-bot-api
   docker run -d -p 8081:8081 \
     --name telegram-bot-api \
     -e TELEGRAM_API_ID=your_api_id \
     -e TELEGRAM_API_HASH=your_api_hash \
     -v telegram-bot-api-data:/var/lib/telegram-bot-api \
     aiogram/telegram-bot-api

2. Получи API_ID и API_HASH:
   https://my.telegram.org/apps

3. Настрой переменные окружения:
   export TELEGRAM_BOT_TOKEN='твой_токен'
   export LOCAL_API_URL='http://localhost:8081'

4. Раскомментируй строки 381-385 в коде (LOCAL_API_URL)

5. Запусти бота:
   python bot.py

📌 С локальным сервером лимит 2000 МБ!


🔧 ВАРИАНТ 2: Обычный Bot API (до 50 МБ)

1. Просто измени MAX_FILE_SIZE_MB = 50 (строка 16)
2. Установи переменную окружения:
   export TELEGRAM_BOT_TOKEN='твой_токен'
3. Запусти:
   python bot.py

📌 Работает без дополнительных настроек!


📦 УСТАНОВКА ЗАВИСИМОСТЕЙ:

pip install python-telegram-bot==20.7 \
    PyMuPDF==1.23.8 \
    Pillow==10.1.0 \
    opencv-python==4.8.1.78 \
    numpy==1.26.2


🚀 ОПТИМИЗАЦИЯ ДЛЯ БОЛЬШИХ ФАЙЛОВ:

- Для файлов 100+ МБ снижай DPI до 100-120
- Качество JPEG ставь 30-40 для максимального сжатия
- Шум и блюр работают быстрее на низком DPI
- Используй SSD для временных файлов


💡 СОВЕТЫ:

- Для 500 МБ файлов обработка может занять 5-15 минут
- Убедись что достаточно RAM (минимум 4 ГБ свободно)
- Временные файлы занимают ~2x размера оригинала
- Прогресс-бар обновляется каждые 2 секунды
"""
