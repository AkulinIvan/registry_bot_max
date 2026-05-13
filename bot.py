"""Основной модуль бота для регистрации на мероприятия"""
import asyncio
import logging
import sys
import re
import traceback
from typing import Any, Dict, Optional
from dataclasses import dataclass
from functools import wraps
from logging.handlers import RotatingFileHandler
import os

import httpx
from maxapi import Bot, Dispatcher
from maxapi.types import MessageCreated, Command
from maxapi.types.updates import BotAdded, BotStarted, DialogRemoved, MessageCallback
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import RequestContactButton, CallbackButton
from maxapi.exceptions.max import MaxApiError

from config import AppConfig
from dadata_client import DadataClient
from database import Database
from bitrix_client import BitrixClient
from admin_service import AdminService, format_broadcast_result
from checking_service import checkin_service, CheckInResult

logger = logging.getLogger(__name__)
import maxapi.types.attachments as attachments_module
logger.info(f"Available in attachments: {dir(attachments_module)}")



# Посмотрите что доступно в image модуле
try:
    import maxapi.types.attachments.image as image_module
    logger.info(f"Available in image: {dir(image_module)}")
except:
    pass

# Настройка логирования
config = AppConfig()

# Создаем директорию для логов если её нет
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
    print(f"Created log directory: {log_dir}")

# Настройка ротации логов
log_file = os.path.join(log_dir, "bot.log")
max_log_size = 10 * 1024 * 1024  # 10 MB
backup_count = 5  # Хранить 5 файлов бэкапов

# Создаем форматтер для логов
log_format = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Настраиваем корневой логгер
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, config.bot.log_level))

# Очищаем существующие хендлеры
root_logger.handlers.clear()

# Консольный хендлер
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, config.bot.log_level))
console_handler.setFormatter(log_format)
root_logger.addHandler(console_handler)

# Файловый хендлер с ротацией
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=max_log_size,
    backupCount=backup_count,
    encoding='utf-8'
)
file_handler.setLevel(getattr(logging, config.bot.log_level))
file_handler.setFormatter(log_format)
root_logger.addHandler(file_handler)

# Хендлер для ошибок (отдельный файл)
error_log_file = os.path.join(log_dir, "bot_error.log")
error_handler = RotatingFileHandler(
    error_log_file,
    maxBytes=max_log_size,
    backupCount=backup_count,
    encoding='utf-8'
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(log_format)
root_logger.addHandler(error_handler)

logger = logging.getLogger(__name__)

# Логируем информацию о настройке логирования
logger.info("=" * 60)
logger.info("Logging configuration:")
logger.info(f"  Log directory: {log_dir}")
logger.info(f"  Main log file: {log_file}")
logger.info(f"  Error log file: {error_log_file}")
logger.info(f"  Max log size: {max_log_size / (1024*1024):.0f} MB")
logger.info(f"  Backup count: {backup_count}")
logger.info(f"  Log level: {config.bot.log_level}")
logger.info("=" * 60)

# Инициализация
bot = Bot(token=config.bot.token)
dp = Dispatcher()
db = Database()
bitrix_client = BitrixClient(
    register_url=config.bitrix.register_url,
    list_url=config.bitrix.list_url,
    timeout=config.bitrix.timeout
)
admin_service = AdminService(bot=bot, db=db, config=config)

# Хранилище состояний
user_states = {}


def log_function_call(func):
    """Декоратор для логирования вызовов функций"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        func_name = func.__name__
        logger.debug(f"→ Calling {func_name}")
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"← {func_name} completed")
            return result
        except Exception as e:
            logger.error(f"✗ {func_name} failed: {e}\n{traceback.format_exc()}")
            raise
    return wrapper


def safe_execute(func):
    """Декоратор для безопасного выполнения функций с обработкой ошибок"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except MaxApiError as e:
            logger.error(f"MAX API Error in {func.__name__}: {e.code} - {e}")
            if "chat.not.found" in str(e):
                logger.warning(f"Chat not found, skipping...")
                return None
            raise
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}\n{traceback.format_exc()}")
            raise
    return wrapper


@dataclass
class ValidatedPhone:
    number: str
    formatted: str


@dataclass
class ValidatedINN:
    number: str
    type: str


def validate_phone(phone: str) -> Optional[ValidatedPhone]:
    """Валидация телефона"""
    logger.debug(f"Validating phone: {phone[:4] if phone else 'empty'}****")
    
    if not phone:
        logger.warning("Phone validation failed: empty phone")
        return None
    
    clean = re.sub(r'[^\d]', '', phone.strip())
    
    if len(clean) not in (10, 11):
        logger.warning(f"Phone validation failed: invalid length {len(clean)}")
        return None
    
    if len(clean) == 11:
        if clean.startswith('8'):
            clean = '7' + clean[1:]
        elif not clean.startswith('7'):
            logger.warning(f"Phone validation failed: invalid prefix {clean[0]}")
            return None
    elif len(clean) == 10:
        clean = '7' + clean
    
    formatted = f"+7 ({clean[1:4]}) {clean[4:7]}-{clean[7:9]}-{clean[9:11]}"
    logger.info(f"Phone validated: {formatted}")
    return ValidatedPhone(number=clean, formatted=formatted)


async def validate_inn(inn: str) -> Optional[Dict[str, Any]]:
    """Валидация ИНН с проверкой типа (юрлицо, ИП, самозанятый, физлицо)"""
    logger.debug(f"Validating INN: {inn[:4] if inn else 'empty'}****")
    
    if not inn:
        logger.warning("INN validation failed: empty INN")
        return None
    
    clean = re.sub(r'[\s\-]', '', inn.strip())
    
    if len(clean) not in (10, 12) or not clean.isdigit():
        logger.warning(f"INN validation failed: invalid format (len={len(clean)}, isdigit={clean.isdigit()})")
        return None
    
    try:
        async with DadataClient() as dadata:
            # Сначала пробуем найти в DaData
            company = await dadata.find_company_by_inn(clean)
            
            if company:
                logger.info(f"✅ Company found via DaData: {company['name']['short']}")
                
                # Определяем тип
                raw_type = company.get('type', '').upper()
                
                if raw_type == 'INDIVIDUAL':
                    company_type = 'INDIVIDUAL'
                elif raw_type == 'LEGAL':
                    company_type = 'LEGAL'
                else:
                    company_type = 'FIZ'
                
                return {
                    "valid": True,
                    "number": clean,
                    "type": company_type,
                    "company": company,
                    "is_active": company.get('is_active', True),
                    "state_status": company.get('state', {}).get('status', 'ACTIVE')
                }
            
            # Если не нашли в DaData - проверяем самозанятых (только для 12-значных ИНН)
            if len(clean) == 12:
                logger.info(f"INN {clean[:4]}**** not found in DaData, checking NPD status...")
                npd_result = await dadata.check_npd_status(clean)
                
                if npd_result and npd_result.get("is_npd"):
                    logger.info(f"✅ INN {clean[:4]}**** is NPD (самозанятый)")
                    return {
                        "valid": True,
                        "number": clean,
                        "type": "NPD",
                        "company": {
                            "type": "NPD",
                            "name": {"short": "Самозанятый", "full": "Самозанятый"},
                            "is_active": True,
                            "state": {"status": "ACTIVE"}
                        },
                        "is_active": True,
                        "npd_message": npd_result.get("message", "")
                    }
            
            # Если ничего не нашли - возвращаем FIZ
            logger.info(f"INN {clean[:4]}**** not found anywhere, returning FIZ type")
            return {
                "valid": True,
                "number": clean,
                "type": "FIZ",
                "company": {
                    "type": "FIZ",
                    "name": {"short": "Физическое лицо", "full": "Физическое лицо"},
                    "is_active": True,
                    "state": {"status": "ACTIVE"}
                },
                "is_active": True
            }
                
    except Exception as e:
        logger.error(f"Error during INN validation {clean[:4]}****: {e}")
        # В случае ошибки - возвращаем FIZ для 12-значных, LEGAL для 10-значных
        fallback_type = 'FIZ' if len(clean) == 12 else 'LEGAL'
        logger.info(f"Using fallback type {fallback_type} for INN {clean[:4]}****")
        return {
            "valid": True,
            "number": clean,
            "type": fallback_type,
            "company": {
                "type": fallback_type,
                "name": {"short": "Неизвестно", "full": "Неизвестно"},
                "is_active": True,
                "state": {"status": "ACTIVE"}
            },
            "is_active": True,
            "fallback": True
        }


def get_user_id(event) -> int:
    """Получение user_id из события"""
    try:
        # Для MessageCreated
        if hasattr(event, 'message') and event.message:
            if event.message.sender:
                return event.message.sender.user_id
        
        # Для MessageCallback
        if hasattr(event, 'user') and event.user:
            return event.user.user_id
            
        # Прямой атрибут
        if hasattr(event, 'user_id'):
            return event.user_id
            
        logger.warning("No sender/user in event")
        return 0
    except Exception as e:
        logger.error(f"Failed to get user_id: {e}")
        return 0


def get_user_name(event: MessageCreated) -> str:
    """Получение имени пользователя из события"""
    try:
        sender = event.message.sender
        if sender:
            name_parts = []
            if sender.first_name:
                name_parts.append(sender.first_name)
            if sender.last_name:
                name_parts.append(sender.last_name)
            if name_parts:
                return " ".join(name_parts)
            elif sender.username:
                return sender.username
        return "Пользователь"
    except Exception as e:
        logger.error(f"Failed to get user_name: {e}")
        return "Пользователь"

async def send_callback_response(event, text: str, attachments=None):
    """
    Универсальная отправка ответа на callback в MAX API
    
    Args:
        event: объект MessageCallback
        text: текст сообщения
        attachments: список вложений (клавиатура и т.д.)
    """
    try:
        # Получаем chat_id из callback
        chat_id = get_chat_id(event)
        
        if chat_id:
            # Отправляем новое сообщение в чат
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                attachments=attachments or []
            )
            logger.info(f"Sent callback response to chat {chat_id}")
            return True
        else:
            # Если chat_id не найден, просто подтверждаем callback
            logger.warning("No chat_id found in callback")
            try:
                await event.answer(show_alert=False)
            except:
                pass
            return False
            
    except MaxApiError as e:
        if "chat.not.found" in str(e) or "Unknown recipient" in str(e):
            logger.warning(f"Chat not available: {e}")
            try:
                await event.answer(show_alert=False)
            except:
                pass
            return False
        raise
    except Exception as e:
        logger.error(f"Failed to send callback response: {e}")
        try:
            await event.answer(show_alert=False)
        except:
            pass
        return False

def get_chat_id(event: MessageCreated) -> Optional[int]:
    """Получение chat_id из события"""
    try:
        if event.message.recipient:
            return event.message.recipient.chat_id
        return None
    except Exception as e:
        logger.error(f"Failed to get chat_id: {e}")
        return None


def get_message_text(event: MessageCreated) -> Optional[str]:
    """Получение текста сообщения"""
    try:
        if event.message.body:
            return event.message.body.text
        return None
    except Exception as e:
        logger.error(f"Failed to get message text: {e}")
        return None


def extract_phone_from_message(message) -> Optional[str]:
    """Извлечение номера телефона из сообщения"""
    logger.debug("Attempting to extract phone from message")
    
    try:
        if hasattr(message, 'body') and message.body:
            body = message.body
            
            # Используем model_dump вместо устаревшего dict
            if hasattr(body, 'model_dump'):
                body_dict = body.model_dump()
            elif hasattr(body, 'dict'):
                body_dict = body.dict()
            else:
                logger.warning("Body has neither model_dump nor dict method")
                return None
            
            # Проверяем attachments
            if 'attachments' in body_dict:
                logger.debug(f"Found {len(body_dict['attachments'])} attachments")
                
                for idx, attachment in enumerate(body_dict['attachments']):
                    if isinstance(attachment, dict) and attachment.get('type') == 'contact':
                        logger.info(f"Found contact attachment at index {idx}")
                        payload = attachment.get('payload', {})
                        vcf_info = payload.get('vcf_info', '')
                        
                        # Извлекаем телефон из VCF
                        if vcf_info:
                            # Ищем строку с TEL
                            for line in vcf_info.split('\n'):
                                if line.startswith('TEL'):
                                    # Извлекаем номер после двоеточия
                                    parts = line.split(':')
                                    if len(parts) > 1:
                                        phone = parts[1].strip()
                                        logger.info(f"Phone extracted from VCF: {phone[:4]}****")
                                        return phone
                        
                        # Также проверяем max_info на случай если телефон там
                        max_info = payload.get('max_info', {})
                        if 'phone' in max_info:
                            phone = max_info['phone']
                            logger.info(f"Phone extracted from max_info: {phone[:4]}****")
                            return phone
            else:
                logger.debug("No attachments in body")
        
        logger.debug("No phone found in message")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting phone from message: {e}\n{traceback.format_exc()}")
        return None


def create_phone_keyboard():
    """Создание клавиатуры с кнопкой для отправки номера телефона"""
    logger.debug("Creating phone keyboard")
    try:
        builder = InlineKeyboardBuilder()
        builder.row(
            RequestContactButton(
                text="📱 Поделиться номером телефона"
            )
        )
        return builder.as_markup()
    except Exception as e:
        logger.error(f"Failed to create phone keyboard: {e}")
        return None

def create_main_menu_keyboard():
    """Главное меню с 3 кнопками"""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📋 Регистрация на форум", callback_data="menu_registration", payload="menu_registration"))
    builder.row(CallbackButton(text="📢 Анонсы мероприятий", callback_data="menu_announcements", payload="menu_announcements"))
    builder.row(CallbackButton(text="🤝 Хочу коллаборацию", callback_data="menu_collaboration", payload="menu_collaboration"))
    return builder.as_markup()


def get_back_keyboard():
    """Клавиатура с кнопкой Назад"""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="◀️ Назад в меню", callback_data="back_to_menu", payload="back_to_menu"))
    return builder.as_markup()
    
# Функция для очистки старых логов (можно вызывать периодически)
def cleanup_old_logs():
    """Очистка старых лог-файлов"""
    try:
        log_files = [f for f in os.listdir(log_dir) if f.endswith('.log')]
        logger.info(f"Found {len(log_files)} log files")
        
        # Можно добавить логику удаления очень старых файлов
        # Например, удалять файлы старше 30 дней
        
    except Exception as e:
        logger.error(f"Error cleaning up logs: {e}")


# ============= Обработчики системных событий =============

@dp.dialog_removed()
@safe_execute
async def dialog_removed_handler(event: DialogRemoved):
    """Обработка удаления диалога"""
    # Проверяем, какие атрибуты доступны
    logger.debug(f"DialogRemoved attributes: {[attr for attr in dir(event) if not attr.startswith('_')]}")
    
    # Пробуем получить ID разными способами
    chat_id = getattr(event, 'chat_id', None)
    user_id = getattr(event, 'user_id', None)
    
    # Если нет user_id, пробуем другие атрибуты
    if user_id is None:
        user_id = getattr(event, 'dialog_id', None) or getattr(event, 'peer_id', None)
    
    logger.info(f"Dialog removed - chat_id: {chat_id}, user_id: {user_id}")


# ============= Обработчики команд =============

@dp.message_created(Command('start'))
@safe_execute
@log_function_call
async def start_command(event: MessageCreated):
    """Команда /start"""
    user_id = get_user_id(event)
    user_name = get_user_name(event)
    chat_id = get_chat_id(event)
    
    logger.info(f"Start command - user_id: {user_id}, name: {user_name}, chat_id: {chat_id}")
    
    if user_id in user_states:
        del user_states[user_id]
        
    keyboard = create_main_menu_keyboard()
    
    await event.message.answer(
        "👋 Добро пожаловать на форум «Дни предпринимательства»!\n\n"
        "Выберите интересующий вас раздел:",
        attachments=[keyboard]
    )
    logger.info(f"Menu sent to user {user_id}")

@dp.message_created(Command('admin'))
@safe_execute
async def admin_command(event: MessageCreated):
    """Команда администратора - показывает меню"""
    user_id = get_user_id(event)
    
    if not admin_service.is_admin(user_id):
        logger.warning(f"Unauthorized admin access from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к панели администратора.")
        return
    
    # Создаем клавиатуру администратора
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📊 Статистика", callback_data="admin_stats", payload="admin_stats"))
    builder.row(CallbackButton(text="📨 Рассылка", callback_data="admin_broadcast", payload="admin_broadcast"))
    builder.row(CallbackButton(text="📈 История рассылок", callback_data="admin_history", payload="admin_history"))
    builder.row(CallbackButton(text="📋 Отметка участников", callback_data="admin_checkin", payload="admin_checkin"))
    builder.row(CallbackButton(text="◀️ Назад в меню", callback_data="back_to_menu", payload="back_to_menu"))
    
    await event.message.answer(
        "🔧 Панель администратора\n\n"
        "Выберите действие:",
        attachments=[builder.as_markup()]
    )

@dp.message_created(Command('checkin'))
@safe_execute
async def checkin_command(event: MessageCreated):
    """Команда для отметки участников"""
    user_id = get_user_id(event)
    
    if not admin_service.is_admin(user_id):
        logger.warning(f"Unauthorized checkin access from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к отметке участников.")
        return
    
    # Загружаем список мероприятий
    await event.message.answer("🔄 Загружаю список мероприятий...")
    events = await checkin_service.fetch_events()
    
    if not events:
        await event.message.answer("❌ Не удалось загрузить мероприятия.")
        return
    
    keyboard = checkin_service.get_events_keyboard()
    await event.message.answer(
        f"📅 Доступные мероприятия ({len(events)}):\n\n"
        "Выберите мероприятие для отметки участников:",
        attachments=[keyboard]
    )

@dp.message_created(Command('stats'))
@safe_execute
async def stats_command(event: MessageCreated):
    """Показать статистику бота"""
    user_id = get_user_id(event)
    
    if not admin_service.is_admin(user_id):
        logger.warning(f"Unauthorized stats access from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    try:
        # Получаем статистику из БД
        db_stats = await db.get_stats()
        user_counts = await admin_service.get_user_count()
        
        message = "📊 Статистика бота\n\n"
        message += "Пользователи:\n"
        message += f"👥 Всего: {user_counts['total']}\n"
        message += f"✅ Зарегистрировано: {user_counts['completed']}\n"
        message += f"⏳ В процессе: {user_counts['pending']}\n"
        message += f"🚫 Заблокировали: {user_counts['blocked']}\n"
        message += f"📱 Активные: {user_counts['active']}\n\n"
        
        message += "Регистрации:\n"
        message += f"📋 Всего: {db_stats['total_registrations']}\n"
        message += f"✅ Завершено: {db_stats['completed']}\n"
        message += f"📈 Процент завершения: {db_stats['completion_rate']}\n\n"
        
        message += "База данных:\n"
        message += f"🔍 Запросов: {db_stats['db_queries']}\n"
        message += f"❌ Ошибок: {db_stats['db_errors']}\n"
        
        await event.message.answer(message)
        
    except Exception as e:
        logger.error(f"Error in stats command: {e}")
        await event.message.answer("❌ Ошибка при получении статистики.")


@dp.message_created(Command('broadcast'))
@safe_execute
async def broadcast_command(event: MessageCreated):
    """Начать рассылку сообщений"""
    user_id = get_user_id(event)
    
    if not admin_service.is_admin(user_id):
        logger.warning(f"Unauthorized broadcast access from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к рассылке.")
        return
    
    # Устанавливаем состояние ожидания сообщения для рассылки
    user_states[user_id] = 'awaiting_broadcast'
    
    # Обновляем состояние в БД
    try:
        await db.save_user(
            user_id=user_id,
            state='awaiting_broadcast'
        )
    except Exception as e:
        logger.error(f"Failed to update state in DB: {e}")
    
    # Создаем клавиатуру с кнопкой отмены
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="❌ Отмена", callback_data="cancel_broadcast", payload="cancel_broadcast"))
    
    await event.message.answer(
        "📨 Создание рассылки\n\n"
        "Отправьте текст сообщения, которое хотите разослать всем пользователям.\n\n"
        "Поддерживается HTML-форматирование:\n"
        "• &lt;b&gt;жирный&lt;/b&gt;\n"
        "• &lt;i&gt;курсив&lt;/i&gt;\n"
        "• &lt;code&gt;моноширинный&lt;/code&gt;\n\n"
        "Для отмены нажмите кнопку ниже или отправьте /cancel",
        attachments=[builder.as_markup()]
    )

@dp.message_created(Command('cancel'))
@safe_execute
async def cancel_command(event: MessageCreated):
    """Отмена текущего действия"""
    user_id = get_user_id(event)
    
    if user_id in user_states:
        state = user_states[user_id]
        
        if state == 'awaiting_broadcast':
            del user_states[user_id]
            await event.message.answer("❌ Рассылка отменена.")
            return
        
        if state == 'awaiting_broadcast_confirm':
            del user_states[user_id]
            await event.message.answer("❌ Рассылка отменена.")
            return
    
    # Обычная отмена регистрации
    if user_id in user_states:
        del user_states[user_id]
        await event.message.answer(
            "↩️ Действие отменено. Возврат в главное меню:",
            attachments=[create_main_menu_keyboard()]
        )
        
@dp.message_created(Command('logs'))
@safe_execute
async def logs_command(event: MessageCreated):
    """Команда для просмотра статистики логов (только для админов)"""
    user_id = get_user_id(event)
    
    # Проверяем, является ли пользователь админом
    admin_ids = getattr(config.bot, 'admin_ids', [])
    if user_id not in admin_ids:
        logger.warning(f"Unauthorized access to /logs from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    try:
        log_files = []
        total_size = 0
        
        for f in os.listdir(log_dir):
            if f.endswith('.log'):
                file_path = os.path.join(log_dir, f)
                size = os.path.getsize(file_path)
                total_size += size
                log_files.append({
                    'name': f,
                    'size': size,
                    'size_mb': size / (1024 * 1024)
                })
        
        if not log_files:
            await event.message.answer("📁 Лог-файлы не найдены.")
            return
        
        message = "📊 Статистика лог-файлов:\n\n"
        for log_file in sorted(log_files, key=lambda x: x['name']):
            message += f"📄 {log_file['name']}\n"
            message += f"   Размер: {log_file['size_mb']:.2f} MB\n\n"
        
        message += f"💾 Общий размер: {total_size / (1024 * 1024):.2f} MB"
        message += f"\n📁 Директория: {log_dir}"
        message += f"\n🔄 Макс. размер файла: {max_log_size / (1024 * 1024):.0f} MB"
        message += f"\n💾 Бэкапов: {backup_count}"
        
        await event.message.answer(message)
        
    except Exception as e:
        logger.error(f"Error in logs command: {e}")
        await event.message.answer("❌ Ошибка при получении статистики логов.")

# ============= CALLBACK ОБРАБОТЧИК =============

@dp.message_callback()
@safe_execute
@log_function_call
async def handle_callback(event: MessageCallback):
    """Обработка нажатий на кнопки"""
    
    # Получаем user_id из callback.user
    user_id = None
    
    if hasattr(event, 'callback') and event.callback:
        if hasattr(event.callback, 'user') and event.callback.user:
            user_id = event.callback.user.user_id
    
    # Запасной вариант
    if not user_id:
        user_id = get_user_id(event)
    
    logger.info(f"Callback from user {user_id}: {getattr(event.callback, 'payload', None) if hasattr(event, 'callback') and event.callback else 'unknown'}")
    
    # В MessageCallback данные приходят через event.callback.payload
    if not hasattr(event, 'callback') or not event.callback:
        logger.warning("No callback in event")
        return
    
    callback_data = getattr(event.callback, 'payload', None)
    
    if not callback_data:
        return
    
    # Отвечаем на callback
    try:
        await event.answer()
    except:
        pass
    
    if callback_data == "menu_registration":
        # Начинаем регистрацию
        user_states[user_id] = 'awaiting_phone'
        keyboard = create_phone_keyboard()
        try:
            await db.save_user(
                user_id=user_id,
                chat_id=get_chat_id(event),
                name=get_user_name(event),
                state='awaiting_phone',
                status='pending'
            )
        except Exception as e:
            logger.error(f"Failed to save user: {e}")
        
        await event.message.answer(
            "📋 Регистрация на форум\n\n"
            "Поделитесь вашим номером телефона.\n\n"
            "📱 Нажмите кнопку ниже:",
            attachments=[keyboard]
        )
    
    elif callback_data == "menu_announcements":
        await event.message.answer(
            "📢 Анонсы мероприятий\n\n"
            "Смотрите программу мероприятий на сайте:\n"
            f"дни-предпринимательства.рф",
            attachments=[get_back_keyboard()]
        )
    
    elif callback_data == "menu_collaboration":
        await event.message.answer(
            "🤝 Хочу коллаборацию\n\n"
            "Для поиска партнеров и коллабораций переходите в наш чат:\n\n"
            f"https://max.ru/join/wRFl6wnGv0s9HiX9dDIDLo1CfxgAgSZLzC6Dv2iRwuY",
            attachments=[get_back_keyboard()]
        )
    
    elif callback_data == "back_to_menu":
        if user_id in user_states:
            del user_states[user_id]
        await event.message.answer(
            "👋 Главное меню:\n\nВыберите интересующий вас раздел:",
            attachments=[create_main_menu_keyboard()]
        )
    
    elif callback_data == "admin_stats":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return
        
        db_stats = await db.get_stats()
        user_counts = await admin_service.get_user_count()
        
        message = "📊 Статистика бота\n\n"
        message += "Пользователи:\n"
        message += f"👥 Всего: {user_counts['total']}\n"
        message += f"✅ Зарегистрировано: {user_counts['completed']}\n"
        message += f"⏳ В процессе: {user_counts['pending']}\n"
        message += f"🚫 Заблокировали: {user_counts['blocked']}\n\n"
        
        message += "Регистрации:\n"
        message += f"📋 Всего: {db_stats['total_registrations']}\n"
        message += f"📈 Процент завершения: {db_stats['completion_rate']}\n"
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="◀️ Назад", callback_data="admin_back", payload="admin_back"))
        
        await event.message.answer(message, attachments=[builder.as_markup()])
    
    elif callback_data == "admin_broadcast":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return

        # Обновляем состояние и в user_states, и в БД
        user_states[user_id] = 'awaiting_broadcast'

        # Обновляем состояние в БД
        try:
            await db.save_user(
                user_id=user_id,
                state='awaiting_broadcast'
            )
        except Exception as e:
            logger.error(f"Failed to update state in DB: {e}")

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="❌ Отмена", callback_data="cancel_broadcast", payload="cancel_broadcast"))

        await event.message.answer(
            "📨 Создание рассылки\n\n"
            "Отправьте текст сообщения для рассылки всем пользователям.\n\n"
            "Поддерживается HTML-форматирование",
            attachments=[builder.as_markup()]
        )
    
    elif callback_data == "admin_history":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return
        
        history = await admin_service.get_broadcast_stats(limit=5)
        
        if not history:
            await event.message.answer("📈 История рассылок пуста.")
            return
        
        message = "📈 Последние рассылки:\n\n"
        for h in history:
            success_rate = (h['successful'] / h['total_users'] * 100) if h['total_users'] > 0 else 0
            message += (
                f"📨 {h['broadcast_id']}\n"
                f"   👥 {h['total_users']} | ✅ {h['successful']} | ❌ {h['failed']} | 🚫 {h['blocked']}\n"
                f"   📈 {success_rate:.1f}% | 📅 {h['created_at'][:10] if h['created_at'] else 'N/A'}\n\n"
            )
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="◀️ Назад", callback_data="admin_back", payload="admin_back"))
        
        await event.message.answer(message, attachments=[builder.as_markup()])
    
    elif callback_data == "admin_back":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return
        
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="📊 Статистика", callback_data="admin_stats", payload="admin_stats"))
        builder.row(CallbackButton(text="📨 Рассылка", callback_data="admin_broadcast", payload="admin_broadcast"))
        builder.row(CallbackButton(text="📈 История рассылок", callback_data="admin_history", payload="admin_history"))
        builder.row(CallbackButton(text="◀️ Назад в меню", callback_data="back_to_menu", payload="back_to_menu"))
        
        await event.message.answer(
            "🔧 Панель администратора\n\nВыберите действие:",
            attachments=[builder.as_markup()]
        )
    
    elif callback_data == "cancel_broadcast":
        if user_id in user_states:
            del user_states[user_id]
        
        # Сбрасываем состояние в БД на предыдущее
        try:
            await db.save_user(
                user_id=user_id,
                state='awaiting_phone'  # или другое предыдущее состояние
            )
        except Exception as e:
            logger.error(f"Failed to reset state in DB: {e}")
        
        await event.message.answer("❌ Рассылка отменена.")
    
    elif callback_data == "confirm_broadcast":
        if user_id not in user_states or user_states.get(user_id) != 'awaiting_broadcast_confirm':
            await event.message.answer("❌ Сессия рассылки истекла. Начните заново.")
            return

        broadcast_text = user_states.get(f"{user_id}_broadcast_text", "")
        if not broadcast_text:
            await event.message.answer("❌ Текст рассылки не найден.")
            del user_states[user_id]
            return

        # Запускаем рассылку
        await event.message.answer("📨 Рассылка запущена!\n⏳ Пожалуйста, подождите...")

        try:
            result = await admin_service.send_broadcast(
                message_text=broadcast_text,
                sender_id=user_id,
                exclude_sender=True
            )

            # Отправляем результат
            result_message = format_broadcast_result(result)
            await event.message.answer(result_message)

            # Возвращаем состояние в БД
            await db.save_user(
                user_id=user_id,
                state='registered'
            )

        except Exception as e:
            logger.error(f"Broadcast failed: {e}")
            await event.message.answer(f"❌ Ошибка при рассылке: {str(e)}")

        finally:
            # Очищаем состояние
            if user_id in user_states:
                del user_states[user_id]
            if f"{user_id}_broadcast_text" in user_states:
                del user_states[f"{user_id}_broadcast_text"]

    
    # Показ меню отметки участников
    elif callback_data == "admin_checkin":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return

        user_states[user_id] = 'admin_checkin_menu'

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(
            text="📋 Выбрать мероприятие",
            callback_data="checkin_select_event",
            payload="checkin_select_event"
        ))
        builder.row(CallbackButton(
            text="📷 Сканировать QR-код",
            callback_data="checkin_scan_qr",
            payload="checkin_scan_qr"
        ))
        builder.row(CallbackButton(
            text="✏️ Ввести ID участника",
            callback_data="checkin_manual_id",
            payload="checkin_manual_id"
        ))

        if checkin_service.current_event_id:
            builder.row(CallbackButton(
                text="📊 Текущее мероприятие",
                callback_data="checkin_current_event",
                payload="checkin_current_event"
            ))

        builder.row(CallbackButton(
            text="◀️ Назад в админ-панель",
            callback_data="admin_back",
            payload="admin_back"
        ))

        info = checkin_service.get_current_event_info()
        await event.message.answer(
            f"📋 Отметка участников\n\n{info}Выберите действие:",
            attachments=[builder.as_markup()]
        )

    # Выбор мероприятия из списка
    elif callback_data == "checkin_select_event":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return

        # Загружаем список мероприятий
        await event.message.answer("🔄 Загружаю список мероприятий...")

        events = await checkin_service.fetch_events()

        if not events:
            await event.message.answer(
                "❌ Не удалось загрузить список мероприятий.\n"
                "Проверьте подключение к API.",
                attachments=[InlineKeyboardBuilder().row(
                    CallbackButton(text="🔄 Попробовать снова", callback_data="checkin_select_event", payload="checkin_select_event")
                ).row(
                    CallbackButton(text="◀️ Назад", callback_data="admin_checkin", payload="admin_checkin")
                ).as_markup()]
            )
            return

        user_states[user_id] = 'admin_checkin_select_event'

        keyboard = checkin_service.get_events_keyboard(page=0, per_page=8)
        await event.message.answer(
            f"📅 Доступные мероприятия ({len(events)}):\n\n"
            "Выберите мероприятие для отметки участников:",
            attachments=[keyboard]
        )

    elif callback_data.startswith("events_page_"):
        if not admin_service.is_admin(user_id):
            return

        page_str = callback_data.replace("events_page_", "")

        if page_str == "current":
            # Просто показываем текущую страницу (заглушка)
            await event.answer()
            return

        try:
            page = int(page_str)
        except ValueError:
            return

        if not checkin_service.events:
            await event.message.answer("❌ Список мероприятий пуст. Нажмите 'Обновить список'.")
            return

        keyboard = checkin_service.get_events_keyboard(page=page, per_page=8)
        total_events = len(checkin_service.events)

        await event.message.answer(
            f"📅 Доступные мероприятия ({total_events}):\n\n"
            f"Страница {page + 1}. Выберите мероприятие:",
            attachments=[keyboard]
        )
    
    # Обновление списка мероприятий
    elif callback_data == "refresh_events":
        if not admin_service.is_admin(user_id):
            return

        events = await checkin_service.fetch_events()

        if events:
            keyboard = checkin_service.get_events_keyboard()
            await event.message.answer(
                f"✅ Список обновлен! Найдено мероприятий: {len(events)}\n\n"
                "Выберите мероприятие:",
                attachments=[keyboard]
            )
        else:
            await event.message.answer(
                "❌ Не удалось обновить список мероприятий."
            )

    # Выбор конкретного мероприятия
    elif callback_data.startswith("select_event_"):
        if not admin_service.is_admin(user_id):
            return

        event_id = callback_data.replace("select_event_", "")

        if checkin_service.select_event(event_id):
            event_obj = checkin_service.events.get(event_id)

            # Возвращаемся в меню отметки
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="📷 Сканировать QR-код",
                callback_data="checkin_scan_qr",
                payload="checkin_scan_qr"
            ))
            builder.row(CallbackButton(
                text="✏️ Ввести ID участника",
                callback_data="checkin_manual_id",
                payload="checkin_manual_id"
            ))
            builder.row(CallbackButton(
                text="📅 Выбрать другое мероприятие",
                callback_data="checkin_select_event",
                payload="checkin_select_event"
            ))
            builder.row(CallbackButton(
                text="◀️ Назад",
                callback_data="admin_checkin",
                payload="admin_checkin"
            ))

            date_info = f"\n📅 Дата: {event_obj.date}" if event_obj.date else ""
            location_info = f"\n📍 Место: {event_obj.location}" if event_obj.location else ""

            # Получаем chat_id из колбэка
            chat_id = getattr(event.callback, 'chat_id', None) or get_chat_id(event)

            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ Мероприятие выбрано:\n\n"
                        f"📋 {event_obj.name}\n"
                        f"🆔 ID: {event_obj.id}"
                        f"{date_info}"
                        f"{location_info}\n\n"
                        "Теперь вы можете отмечать участников:"
                    ),
                    attachments=[builder.as_markup()]
                )
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
                # Запасной вариант
                await event.answer(
                    text=f"✅ Выбрано: {event_obj.name}",
                    show_alert=False
                )
        else:
            await event.answer(
                text="❌ Мероприятие не найдено.",
                show_alert=True
            )

    # Информация о текущем мероприятии
    elif callback_data == "checkin_current_event":
        if not admin_service.is_admin(user_id):
            return

        info = checkin_service.get_current_event_info()
        await event.message.answer(
            f"📊 Информация о мероприятии:\n\n{info}"
        )

    # Сканирование QR-кода (ожидаем фото)
    elif callback_data == "checkin_scan_qr":
        if not admin_service.is_admin(user_id):
            return  

        if not checkin_service.current_event_id:
            await send_callback_response(
                event,
                "❌ Сначала выберите мероприятие!",
                [InlineKeyboardBuilder().row(
                    CallbackButton(text="📋 Выбрать мероприятие", 
                                 callback_data="checkin_select_event", 
                                 payload="checkin_select_event")
                ).as_markup()]
            )
            return  

        # 🔧 ИСПРАВЛЕНИЕ: обновляем состояние и в памяти, и в БД
        user_states[user_id] = 'admin_checkin_await_qr'

        try:
            await db.save_user(
                user_id=user_id,
                state='admin_checkin_await_qr'
            )
        except Exception as e:
            logger.error(f"Failed to update state in DB: {e}")  

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(
            text="❌ Отмена",
            callback_data="admin_checkin",
            payload="admin_checkin"
        ))  

        await send_callback_response(
            event,
            "📷 Отправьте фото QR-кода участника\n\n"
            "Бот распознает QR-код и отметит участника.\n"
            "Для отмены нажмите кнопку ниже.",
            [builder.as_markup()]
        )

    # Ручной ввод ID участника
    elif callback_data == "checkin_manual_id":
        if not admin_service.is_admin(user_id):
            return

        if not checkin_service.current_event_id:
            await event.message.answer(
                "❌ Сначала выберите мероприятие!",
                attachments=[InlineKeyboardBuilder().row(
                    CallbackButton(text="📋 Выбрать мероприятие", callback_data="checkin_select_event", payload="checkin_select_event")
                ).as_markup()]
            )
            return

        user_states[user_id] = 'admin_checkin_await_id'

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(
            text="❌ Отмена",
            callback_data="admin_checkin",
            payload="admin_checkin"
        ))

        await event.message.answer(
            "✏️ Введите ID участника (Lead ID):\n\n"
            "Например: DP-A1B2C3D4 или 12345\n\n"
            "Для отмены нажмите кнопку ниже.",
            attachments=[builder.as_markup()]
        )

    # Назад в админ-панель (обновленная кнопка)
    elif callback_data == "admin_back":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="📊 Статистика", callback_data="admin_stats", payload="admin_stats"))
        builder.row(CallbackButton(text="📨 Рассылка", callback_data="admin_broadcast", payload="admin_broadcast"))
        builder.row(CallbackButton(text="📈 История рассылок", callback_data="admin_history", payload="admin_history"))
        builder.row(CallbackButton(text="📋 Отметка участников", callback_data="admin_checkin", payload="admin_checkin"))  # НОВАЯ КНОПКА
        builder.row(CallbackButton(text="◀️ Назад в меню", callback_data="back_to_menu", payload="back_to_menu"))

        await event.message.answer(
            "🔧 Панель администратора\n\nВыберите действие:",
            attachments=[builder.as_markup()]
        )

    # Подтверждение отметки участника
    elif callback_data == "confirm_checkin":
        if not admin_service.is_admin(user_id):
            await event.message.answer("⛔ Доступ запрещен.")
            return

        lead_id = user_states.get(f"{user_id}_checkin_lead_id", "")

        if not lead_id:
            # Пробуем отправить сообщение через send_callback_response
            await send_callback_response(
                event,
                "❌ Сессия истекла. Начните заново."
            )
            if user_id in user_states:
                del user_states[user_id]
            return

        # Выполняем отметку
        # Отправляем сообщение о начале
        await send_callback_response(event, f"⏳ Отмечаем участника: {lead_id}...")

        result = await checkin_service.check_in_by_lead_id(lead_id)

        if result.success:
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="✏️ Ввести ещё ID",
                callback_data="checkin_manual_id",
                payload="checkin_manual_id"
            ))
            builder.row(CallbackButton(
                text="📷 Сканировать QR",
                callback_data="checkin_scan_qr",
                payload="checkin_scan_qr"
            ))
            builder.row(CallbackButton(
                text="◀️ Назад в меню отметки",
                callback_data="admin_checkin",
                payload="admin_checkin"
            ))

            # Получаем chat_id для отправки результата
            chat_id = get_chat_id(event)
            if chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{result.message}\n\n"
                        f"🆔 ID: {result.lead_id}\n"
                        f"📅 Мероприятие: {result.event_name}\n"
                        f"🕐 Время: {result.timestamp.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
                        "Можете отметить следующего участника:"
                    ),
                    attachments=[builder.as_markup()]
                )
        else:
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="✏️ Ввести другой ID",
                callback_data="checkin_manual_id",
                payload="checkin_manual_id"
            ))
            builder.row(CallbackButton(
                text="◀️ Назад",
                callback_data="admin_checkin",
                payload="admin_checkin"
            ))

            chat_id = get_chat_id(event)
            if chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ {result.message}\n\n"
                        "Попробуйте ещё раз:"
                    ),
                    attachments=[builder.as_markup()]
                )

        # Очищаем временные данные
        if f"{user_id}_checkin_lead_id" in user_states:
            del user_states[f"{user_id}_checkin_lead_id"]

        # Возвращаем состояние для возможности ввода нового ID
        user_states[user_id] = 'admin_checkin_await_id'
            
            
# ============= Обработчики обновлений =============

@dp.bot_started()
@safe_execute
@log_function_call
async def bot_started_handler(event: BotStarted):
    """Бот запущен пользователем"""
    logger.info(f"Bot started - user_id: {event.user.user_id}, chat_id: {event.chat_id}")
    
    keyboard = create_main_menu_keyboard()
    
    try:
        await bot.send_message(
            chat_id=event.chat_id,
            text=(
                "👋 Добро пожаловать на форум «Дни предпринимательства»!\n\n"
                "Выберите интересующий вас раздел:"
            ),
            attachments=[keyboard]
        )
        logger.info(f"Menu sent to chat {event.chat_id}")
    except MaxApiError as e:
        if "chat.not.found" in str(e):
            try:
                await bot.send_message(
                    user_id=event.user.user_id,
                    text=(
                        "👋 Добро пожаловать на форум «Дни предпринимательства»!\n\n"
                        "Выберите интересующий вас раздел:"
                    ),
                    attachments=[keyboard]
                )
            except:
                pass
        else:
            logger.error(f"Error in bot_started: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in bot_started: {e}")


@dp.bot_added()
@safe_execute
@log_function_call
async def bot_added_handler(event: BotAdded):
    """Бот добавлен в чат"""
    logger.info(f"Bot added to chat {event.chat_id}")
    
    keyboard = create_main_menu_keyboard()
    
    try:
        await bot.send_message(
            chat_id=event.chat_id,
            text=(
                "👋 Добро пожаловать!\n\n"
                "Я бот для регистрации на мероприятия.\n"
                "Выберите интересующий вас раздел:"
            ),
            attachments=[keyboard]
        )
        logger.info(f"Menu sent to chat {event.chat_id}")
    except Exception as e:
        logger.error(f"Failed to send bot_added message: {e}")


# ============= Основной обработчик сообщений =============

@dp.message_created()
@safe_execute
@log_function_call
async def handle_all_messages(event: MessageCreated):
    """Обработка всех сообщений"""
    user_id = get_user_id(event)
    user_name = get_user_name(event)
    chat_id = get_chat_id(event)
    
    logger.info(f"Processing message - user_id: {user_id}, name: {user_name}, chat_id: {chat_id}")
    
    # Получаем текст сообщения
    text = get_message_text(event)
    if text:
        logger.debug(f"Message text: {text[:100] if len(text) > 100 else text}")
    
    # Пробуем извлечь телефон из контакта
    phone = extract_phone_from_message(event.message)
    if phone:
        logger.info(f"Phone extracted from contact: {phone[:4]}****")
    
    # Игнорируем команды (кроме уже обработанных)
    if text and text.startswith('/'):
        logger.debug(f"Ignoring command in general handler: {text}")
        return
    
    # Получаем пользователя из БД
    try:
        user = await db.get_user(user_id)
        if user:
            logger.debug(f"User {user_id} found in database, state: {user.get('state')}, status: {user.get('registration_status')}")
        else:
            logger.debug(f"User {user_id} not found in database")
    except Exception as e:
        logger.error(f"Failed to get user {user_id} from database: {e}")
        await event.message.answer("❌ Ошибка базы данных. Попробуйте позже.")
        return
    
    # Если пользователь не найден - создаем
    if not user:
        logger.info(f"Creating new user {user_id}")
        try:
            await db.save_user(
                user_id=user_id,
                chat_id=chat_id,
                name=user_name,
                state='awaiting_phone',
                status='pending'
            )
            logger.info(f"New user {user_id} created")
        except Exception as e:
            logger.error(f"Failed to create user {user_id}: {e}")
            await event.message.answer("❌ Ошибка при создании пользователя. Попробуйте позже.")
            return
        
        user_states[user_id] = 'awaiting_phone'
        
        keyboard = create_phone_keyboard()
        if keyboard:
            await event.message.answer(
                "👋 Добро пожаловать!\n\n"
                "Нажмите кнопку ниже, чтобы поделиться номером телефона ",
                attachments=[keyboard]
            )
        return
    
    state = user_states.get(user_id) or (user.get('state') if user else 'awaiting_phone')
    logger.info(f"User {user_id} current state: {state} (memory: {user_id in user_states}, db: {user.get('state') if user else 'None'})")
    
    # ========== ОБРАБОТКА ОТМЕТКИ УЧАСТНИКОВ ==========

    if state == 'admin_checkin_confirm':
        # Это состояние обрабатывается только через callback (confirm_checkin)
        # Если сюда попали с текстом - игнорируем
        await event.message.answer(
            "⚠️ Используйте кнопки выше для подтверждения или отмены."
        )
        return

    # Ожидание QR-кода от админа
    if state == 'admin_checkin_await_qr':
        if not config.bot.is_admin(user_id):
            logger.warning(f"Non-admin user {user_id} in admin checkin state")
            del user_states[user_id]
            await event.message.answer("⛔ Доступ запрещен.")
            return
    
        # Пытаемся скачать изображение из сообщения
        image_bytes = await download_image_from_message(event.message)
        
        if not image_bytes:
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="❌ Отмена",
                callback_data="admin_checkin",
                payload="admin_checkin"
            ))
            await event.message.answer(
                "📷 Пожалуйста, отправьте фото QR-кода.\n\n"
                "Бот распознает QR-код и автоматически отметит участника.\n"
                "Для отмены нажмите кнопку ниже.",
                attachments=[builder.as_markup()]
            )
            return
    
        # Сообщаем о начале обработки
        await event.message.answer("🔍 Распознаю QR-код...")
    
        try:
            from qr_scanner import decode_qr_from_bytes, QR_AVAILABLE
            
            if not QR_AVAILABLE:
                await event.message.answer(
                    "⚠️ Библиотеки для распознавания QR-кодов не установлены.\n\n"
                    "Введите ID участника вручную:",
                    attachments=[InlineKeyboardBuilder().row(
                        CallbackButton(text="✏️ Ввести ID вручную", 
                                     callback_data="checkin_manual_id", 
                                     payload="checkin_manual_id")
                    ).as_markup()]
                )
                user_states[user_id] = 'admin_checkin_await_id'
                return
            
            # Распознаём QR-код
            qr_data = decode_qr_from_bytes(image_bytes)
            
            if qr_data:
                logger.info(f"QR decoded: {qr_data}")
                
                # 🔧 ПРЕОБРАЗУЕМ URL в правильный формат
                lead_id = checkin_service.extract_lead_id_from_qr(qr_data)
                
                if not lead_id:
                    await event.message.answer(
                        f"⚠️ Не удалось извлечь ID участника из QR-кода.\n\n"
                        f"Распознано: {qr_data[:200]}\n\n"
                        "Введите ID вручную:",
                        attachments=[InlineKeyboardBuilder().row(
                            CallbackButton(text="✏️ Ввести ID вручную", 
                                         callback_data="checkin_manual_id", 
                                         payload="checkin_manual_id")
                        ).as_markup()]
                    )
                    user_states[user_id] = 'admin_checkin_await_id'
                    return
                
                # 🔧 ФОРМИРУЕМ ПРАВИЛЬНЫЙ URL для отметки
                # Варианты QR:
                # 1. https://bitrix.neto.ru/?id=98839          → нужно /lead.php?leadid=98839
                # 2. https://bitrix.neto.ru/lead.php?leadid=98839 → уже правильный
                # 3. 98839                                       → просто ID
                
                if 'lead.php' in qr_data:
                    # Уже правильный формат, используем как есть
                    check_url = qr_data
                else:
                    # Формируем правильный URL
                    check_url = f"https://bitrix.neto.ru/lead.php?leadid={lead_id}"
                
                logger.info(f"Using check URL: {check_url}")
                
                # Выполняем отметку
                result = await checkin_service.check_in_participant(check_url)
                
                builder = InlineKeyboardBuilder()
                builder.row(CallbackButton(
                    text="📷 Сканировать ещё",
                    callback_data="checkin_scan_qr",
                    payload="checkin_scan_qr"
                ))
                builder.row(CallbackButton(
                    text="✏️ Ввести ID вручную",
                    callback_data="checkin_manual_id",
                    payload="checkin_manual_id"
                ))
                builder.row(CallbackButton(
                    text="◀️ Назад в меню отметки",
                    callback_data="admin_checkin",
                    payload="admin_checkin"
                ))
                
                if result.success:
                    await event.message.answer(
                        f"✅ {result.message}\n\n"
                        f"🆔 ID участника: {result.lead_id}\n"
                        f"📅 Мероприятие: {result.event_name}\n"
                        f"🕐 Время: {result.timestamp.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
                        "Можете отметить следующего участника:",
                        attachments=[builder.as_markup()]
                    )
                else:
                    await event.message.answer(
                        f"❌ {result.message}\n\n"
                        f"QR: {qr_data[:100]}\n"
                        f"Lead ID: {lead_id}\n\n"
                        "Попробуйте ещё раз или введите ID вручную.",
                        attachments=[builder.as_markup()]
                    )
            else:
                # QR не распознан
                await event.message.answer(
                    "❌ Не удалось распознать QR-код.\n\n"
                    "📌 Рекомендации:\n"
                    "• QR должен быть чётким и хорошо освещён\n"
                    "• Держите камеру ровно\n"
                    "• Сфотографируйте ближе\n\n"
                    "Или введите ID вручную:",
                    attachments=[InlineKeyboardBuilder().row(
                        CallbackButton(text="📷 Попробовать снова", 
                                     callback_data="checkin_scan_qr", 
                                     payload="checkin_scan_qr")
                    ).row(
                        CallbackButton(text="✏️ Ввести ID вручную", 
                                     callback_data="checkin_manual_id", 
                                     payload="checkin_manual_id")
                    ).row(
                        CallbackButton(text="◀️ Назад", 
                                     callback_data="admin_checkin", 
                                     payload="admin_checkin")
                    ).as_markup()]
                )
        
        except Exception as e:
            logger.error(f"Error processing QR: {e}\n{traceback.format_exc()}")
            await event.message.answer(
                f"❌ Ошибка: {str(e)}\n\nПопробуйте ещё раз или введите ID вручную."
            )
        
        return

    # Ожидание ID участника от админа
    if state == 'admin_checkin_await_id':
        if not config.bot.is_admin(user_id):
            logger.warning(f"Non-admin user {user_id} in admin checkin state")
            del user_states[user_id]
            return

        if not text:
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="❌ Отмена",
                callback_data="admin_checkin",
                payload="admin_checkin"
            ))
            await event.message.answer(
                "✏️ Пожалуйста, введите ID участника:",
                attachments=[builder.as_markup()]
            )
            return

        lead_id = text.strip()

        # Если ввели URL, извлекаем ID
        if lead_id.startswith('http'):
            extracted_id = checkin_service.extract_lead_id_from_qr(lead_id)
            if extracted_id:
                lead_id = extracted_id
                logger.info(f"Extracted lead ID from URL: {lead_id}")

        # Проверяем валидность ID
        if not re.match(r'^[A-Za-z0-9\-_]+$', lead_id):
            await event.message.answer(
                "❌ Неверный формат ID участника.\n"
                "ID должен содержать только буквы, цифры, дефисы и подчеркивания.\n\n"
                "Попробуйте ещё раз:"
            )
            return

        # Ищем лида для отображения информации
        await event.message.answer(f"🔍 Ищу участника: {lead_id}...")

        lead_data = await checkin_service.find_lead_by_id(lead_id)

        # Сохраняем lead_id для подтверждения
        user_states[f"{user_id}_checkin_lead_id"] = lead_id
        user_states[user_id] = 'admin_checkin_confirm'

        if lead_data:
            # 🔧 Правильные поля из API: Leadid, Inn, Phone (имени нет)
            lead_name = lead_data.get('name') or lead_data.get('NAME') or f"Участник {lead_id}"
            lead_phone = lead_data.get('Phone') or lead_data.get('phone') or 'Нет данных'
            lead_inn = lead_data.get('Inn') or lead_data.get('inn') or 'Нет данных'

            message = (
                f"👤 Найден участник:\n\n"
                f"🆔 Lead ID: {lead_data.get('Leadid', lead_id)}\n"
                f"📋 ID записи: {lead_data.get('id', 'N/A')}\n"
                f"👨 Имя: {lead_name}\n"
                f"📱 Телефон: {lead_phone}\n"
                f"📋 ИНН: {lead_inn[:4] if lead_inn != 'Нет данных' else 'Нет данных'}****\n\n"
                f"Мероприятие: {checkin_service.current_event_name}\n\n"
                "Подтвердите отметку:"
            )
        else:
            message = (
                f"⚠️ Участник с ID {lead_id} не найден в списке.\n\n"
                f"Мероприятие: {checkin_service.current_event_name}\n\n"
                "Всё равно отметить?"
            )

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(
            text="✅ Подтвердить отметку",
            callback_data="confirm_checkin",
            payload="confirm_checkin"
        ))
        builder.row(CallbackButton(
            text="❌ Отмена",
            callback_data="admin_checkin",
            payload="admin_checkin"
        ))

        await event.message.answer(message, attachments=[builder.as_markup()])
        return
    
    # ========== ПРИОРИТЕТНАЯ ПРОВЕРКА: СОСТОЯНИЯ РАССЫЛКИ ==========
    
    # Обработка состояния ожидания текста рассылки
    if state == 'awaiting_broadcast':
        if not config.bot.is_admin(user_id):
            logger.warning(f"Non-admin user {user_id} in broadcast state")
            del user_states[user_id]
            await event.message.answer("⛔ Доступ запрещен.")
            return
        
        if not text:
            await event.message.answer("📝 Пожалуйста, отправьте текст сообщения для рассылки.")
            return
        
        # Сохраняем текст рассылки
        user_states[f"{user_id}_broadcast_text"] = text
        user_states[user_id] = 'awaiting_broadcast_confirm'
        
        # Показываем превью и запрашиваем подтверждение
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(
            text="✅ Отправить всем", 
            callback_data="confirm_broadcast", 
            payload="confirm_broadcast"
        ))
        builder.row(CallbackButton(
            text="❌ Отмена", 
            callback_data="cancel_broadcast", 
            payload="cancel_broadcast"
        ))
        
        preview = text[:500] + "..." if len(text) > 500 else text
        
        await event.message.answer(
            "📨 Предпросмотр рассылки:\n\n"
            f"{preview}\n\n"
            "Подтвердите отправку всем пользователям:",
            attachments=[builder.as_markup()]
        )
        logger.info(f"Broadcast preview shown to admin {user_id}")
        return
    
    # Обработка состояния подтверждения рассылки
    if state == 'awaiting_broadcast_confirm':
        await event.message.answer(
            "⚠️ Пожалуйста, используйте кнопки выше для подтверждения или отмены рассылки."
        )
        return
    
    # ========== КОНЕЦ ПРОВЕРКИ СОСТОЯНИЙ РАССЫЛКИ ==========
    
    # Если уже зарегистрирован
    if user.get('registration_status') == 'completed':
        logger.info(f"User {user_id} is already registered")

        phone = extract_phone_from_message(event.message)
        if not phone and text:
            clean = re.sub(r'[^\d]', '', text)
            if len(clean) >= 10:
                phone = text

        qr_result = await get_qr_image_for_user(user_id, phone)

        if qr_result:
            qr_bytes, bitrix_id = qr_result
            message_text = (
                "Вы уже зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                "Покажите данный QR–код при посещении мероприятий."
            )
            await send_qr_image(event, qr_bytes, bitrix_id, message_text)
        else:
            await event.message.answer(
                "Вы уже зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                "Если вам нужен QR-код, обратитесь к организаторам."
            )
        return
    
    # Обработка состояния ожидания телефона
    if state == 'awaiting_phone':
        logger.info(f"Processing phone input for user {user_id}")
    
        # Пытаемся извлечь телефон ТОЛЬКО из контакта (кнопка "Поделиться")
        phone = extract_phone_from_message(event.message)
    
        # Если телефон получен через кнопку - обрабатываем
        if phone:
            logger.info(f"Phone from contact button: {phone[:4]}****")
    
            # Валидируем телефон сначала
            validated = validate_phone(phone)
    
            if not validated:
                logger.warning(f"Invalid phone format for user {user_id}")
                keyboard = create_phone_keyboard()
                await event.message.answer(
                    "❌ Неверный формат номера.\n"
                    "Пожалуйста, используйте кнопку ниже, чтобы поделиться номером телефона.",
                    attachments=[keyboard]
                )
                return
    
            # Нормализуем телефон для проверок
            normalized_phone = BitrixClient.normalize_phone(validated.number)
    
            # Проверка 1: Ищем в локальной базе данных бота
            existing_registration = await db.get_registration_by_phone(normalized_phone)
    
            if existing_registration:
                logger.info(f"User already registered in local DB with this phone, sending QR code")
    
                qr_result = await get_qr_image_for_user(
                    existing_registration['user_id'], 
                    normalized_phone
                )
    
                if qr_result:
                    qr_bytes, bitrix_id = qr_result
                    message_text = (
                        "Вы уже зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                        "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                        "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                        "Покажите данный QR–код при посещении мероприятий."
                    )
                    await send_qr_image(event, qr_bytes, bitrix_id, message_text)
                else:
                    await event.message.answer(
                        "Вы уже зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                        "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                        "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                        "Если вам нужен QR-код, обратитесь к организаторам."
                    )
                return
    
            # Проверка 2: Ищем в Bitrix24 по номеру телефона
            logger.info(f"Checking duplicate in Bitrix24 for phone: {normalized_phone[:4]}****")
            try:
                duplicate_in_bitrix = await bitrix_client.check_duplicate_by_phone(normalized_phone)

                if duplicate_in_bitrix:
                    logger.warning(f"Phone {normalized_phone[:4]}**** already registered in Bitrix24")

                    # Ищем регистрацию в Bitrix24 чтобы получить данные
                    bitrix_registration = await bitrix_client.find_registration_by_phone(normalized_phone)

                    if bitrix_registration:
                        # ✅ Используем Leadid вместо id
                        lead_id = bitrix_registration.get('Leadid')  # Основной ID лида
                        bitrix_id = bitrix_registration.get('id')     # ID записи
                        bitrix_inn = bitrix_registration.get('Inn', bitrix_registration.get('inn', ''))

                        logger.info(f"Found registration in Bitrix24: Leadid={lead_id}, id={bitrix_id}, INN={bitrix_inn[:4] if bitrix_inn else 'N/A'}****")

                        # Сохраняем регистрацию локально если её нет
                        try:
                            local_reg = await db.get_registration_by_phone(normalized_phone)
                            if not local_reg:
                                await db.save_registration(
                                    user_id=user_id,
                                    chat_id=chat_id,
                                    name=user_name,
                                    phone=normalized_phone,
                                    email='',
                                    inn=bitrix_inn,
                                    event_name=config.bot.event_name,
                                    bitrix_id=lead_id  
                                )
                                logger.info(f"Saved Bitrix24 registration locally for user {user_id}, Leadid={lead_id}")
                        except Exception as e:
                            logger.error(f"Failed to save Bitrix24 registration locally: {e}")

                        # Генерируем QR-код с Leadid
                        if lead_id:
                            qr_result = await get_qr_image_for_user(user_id, normalized_phone)

                            if qr_result:
                                qr_bytes, qr_id = qr_result
                                message_text = (
                                    "Вы уже зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                                    "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                                    "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                                    "Покажите данный QR–код при посещении мероприятий."
                                )
                                await send_qr_image(event, qr_bytes, qr_id, message_text)
                            else:
                                await event.message.answer(
                                    "⚠️ Вы уже зарегистрированы в системе.\n\n"
                                    "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                                    "программа форума и регистрация на сайте дни-предпринимательства.рф"
                                )
                        else:
                            await event.message.answer(
                                "⚠️ Вы уже зарегистрированы в системе.\n\n"
                                "Если вам нужен QR-код, обратитесь к организаторам."
                            )
                    else:
                        await event.message.answer(
                            "⚠️ Этот номер телефона уже зарегистрирован в системе.\n\n"
                            "Если вы забыли QR-код, обратитесь к организаторам."
                        )
                    return

            except Exception as e:
                logger.error(f"Error checking duplicate in Bitrix24: {e}")
                # Продолжаем регистрацию даже при ошибке проверки
    
            # Если дошли сюда - телефон не найден нигде, продолжаем регистрацию
            
            # Сохраняем телефон в локальной БД
            try:
                await db.save_user(
                    user_id=user_id,
                    phone=normalized_phone,  # Сохраняем нормализованный номер
                    state='awaiting_email',
                    status='pending'
                )
                logger.info(f"Phone saved for user {user_id}: {normalized_phone}")
            except Exception as e:
                logger.error(f"Failed to save phone for user {user_id}: {e}")
                await event.message.answer("❌ Ошибка сохранения данных. Попробуйте позже.")
                return
    
            user_states[user_id] = 'awaiting_email'
            logger.info(f"User {user_id} state changed to 'awaiting_email'")
    
            await event.message.answer(
                "📧 Отлично! Теперь отправьте ваш Email адрес:\n"
                "Например: example@mail.ru"
            )
            return
    
        # Если телефон не получен (пользователь отправил текст вместо контакта)
        else:
            logger.warning(f"User {user_id} sent text instead of contact")
            keyboard = create_phone_keyboard()
            await event.message.answer(
                "📱 Для регистрации необходимо поделиться номером телефона.\n\n"
                "Пожалуйста, нажмите кнопку ниже:",
                attachments=[keyboard]
            )
            return
    
    # Обработка состояния ожидания Email
    elif state == 'awaiting_email':
        logger.info(f"Processing email input for user {user_id}")

        if not text:
            logger.warning(f"No text provided for email by user {user_id}")
            await event.message.answer(
                "📧 Пожалуйста, отправьте Email адрес текстом:\n"
                "Например: example@mail.ru"
            )
            return

        # Базовая валидация email
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, text.strip()):
            logger.warning(f"Invalid email format for user {user_id}: {text}")
            await event.message.answer(
                "❌ Неверный формат Email.\n"
                "Пожалуйста, отправьте корректный Email адрес:\n"
                "Например: example@mail.ru"
            )
            return

        email = text.strip().lower()

        try:
            await db.save_user(
                user_id=user_id,
                email=email,
                state='awaiting_name',
                status='pending'
            )
            logger.info(f"Email saved for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to save email for user {user_id}: {e}")
            await event.message.answer("❌ Ошибка сохранения данных. Попробуйте позже.")
            return

        user_states[user_id] = 'awaiting_name'
        logger.info(f"User {user_id} state changed to 'awaiting_name'")

        await event.message.answer(
            "👤 Отлично! Теперь введите ваше ФИО:\n"
            "Например: Иванов Иван Иванович"
        )
        return

    # Обработка состояния ожидания ФИО
    elif state == 'awaiting_name':
        logger.info(f"Processing name input for user {user_id}")
        
        if not text:
            logger.warning(f"No text provided for name by user {user_id}")
            await event.message.answer(
                "👤 Пожалуйста, введите ваше ФИО текстом:\n"
                "Например: Иванов Иван Иванович"
            )
            return
        
        # Базовая валидация ФИО
        name_text = text.strip()
        
        # Проверяем, что ФИО состоит минимум из 2 слов
        name_parts = name_text.split()
        if len(name_parts) < 2:
            logger.warning(f"Invalid name format for user {user_id}: {name_text}")
            await event.message.answer(
                "❌ Пожалуйста, введите фамилию и имя (минимум 2 слова).\n"
                "Например: Иванов Иван"
            )
            return
        
        # Проверяем длину
        if len(name_text) < 5:
            logger.warning(f"Name too short for user {user_id}: {name_text}")
            await event.message.answer(
                "❌ Слишком короткое ФИО. Пожалуйста, введите полное ФИО.\n"
                "Например: Иванов Иван Иванович"
            )
            return
        
        try:
            await db.save_user(
                user_id=user_id,
                name=name_text,
                state='awaiting_inn',
                status='pending'
            )
            logger.info(f"Name saved for user {user_id}: {name_text}")
        except Exception as e:
            logger.error(f"Failed to save name for user {user_id}: {e}")
            await event.message.answer("❌ Ошибка сохранения данных. Попробуйте позже.")
            return
        
        user_states[user_id] = 'awaiting_inn'
        logger.info(f"User {user_id} state changed to 'awaiting_inn'")
        
        await event.message.answer(
            "📋 Отлично! Теперь отправьте ваш ИНН:\n"
            "• 10 цифр для организации\n"
            "• 12 цифр для ИП"
        )
        return
    
    # Обработка состояния ожидания ИНН
    elif state == 'awaiting_inn':
        logger.info(f"Processing INN input for user {user_id}")

        if not text:
            logger.warning(f"No text provided for INN by user {user_id}")
            await event.message.answer(
                "📋 Пожалуйста, отправьте ИНН текстом:\n"
                "• 10 цифр для организации\n"
                "• 12 цифр для ИП или самозанятого"
            )
            return

        # Отправляем сообщение о проверке
        checking_msg = await event.message.answer("🔍 Проверяем ИНН...")

        # Валидация ИНН (без блокировки)
        validation_result = await validate_inn(text)

        if not validation_result or not validation_result.get("valid"):
            await event.message.answer(
                "❌ Неверный ИНН. Должно быть 10 или 12 цифр.\n"
                "Попробуйте ещё раз:"
            )
            return

        validated_inn = validation_result["number"]
        inn_type = validation_result["type"]
        company_data = validation_result.get("company", {})
        is_fallback = validation_result.get("fallback", False)

        # Показываем результат проверки
        type_descriptions = {
            'LEGAL': 'Юридическое лицо',
            'INDIVIDUAL': 'Индивидуальный предприниматель',
            'NPD': 'Самозанятый (НПД)',
            'FIZ': 'Физическое лицо'
        }

        type_desc = type_descriptions.get(inn_type, inn_type)

        if is_fallback:
            await event.message.answer(
                f"⚠️ Проверка по базе ФНС временно недоступна.\n"
                f"ИНН принят как: {type_desc}\n"
                f"Продолжаем регистрацию..."
            )
        else:
            company_info = f"✅ ИНН проверен\n"
            company_info += f"📊 Тип: {type_desc}\n"

            if inn_type == 'NPD' and validation_result.get('npd_message'):
                company_info += f"✅ {validation_result['npd_message']}\n"

            if inn_type == 'INDIVIDUAL' or inn_type == 'LEGAL':
                if company_data.get('address', {}).get('value'):
                    company_info += f"📍 {company_data['address']['value'][:100]}...\n"

            # Проверяем статус только для юрлиц и ИП
            if inn_type in ('LEGAL', 'INDIVIDUAL'):
                if not validation_result.get('is_active', True):
                    status = validation_result.get('state_status', 'неизвестно')
                    await event.message.answer(
                        f"{company_info}\n"
                        f"⚠️ Внимание: статус компании - {status}\n"
                        f"Регистрация продолжена, но рекомендуем проверить актуальность данных."
                    )
                else:
                    await event.message.answer(company_info)
            else:
                # Для NPD и FIZ просто показываем информацию
                await event.message.answer(company_info)

        # Проверка дубликата в Bitrix24 (опционально)
        try:
            duplicate_in_bitrix = await bitrix_client.check_duplicate(
                phone=user.get('phone', '')
            )

            if duplicate_in_bitrix:
                logger.warning(f"Duplicate found in Bitrix24 for user {user_id}")
                # Не блокируем, но предупреждаем
                await event.message.answer(
                    "⚠️ Пользователь с таким телефоном уже зарегистрирован в системе.\n"
                    "Регистрация продолжена."
                )
        except Exception as e:
            logger.error(f"Failed to check duplicate in Bitrix24: {e}")

        # Определяем имя организации и тип
        company_type, org_name = get_company_type_and_name(validation_result, company_data, user.get('name', user_name))

        # Отправляем данные в Bitrix24
        try:
            # Нормализуем телефон перед отправкой
            raw_phone = user.get('phone', '')
            # Убираем все нецифровые символы и обеспечиваем формат 7XXXXXXXXXX
            clean_phone = ''.join(filter(str.isdigit, raw_phone))
            if len(clean_phone) == 11 and clean_phone.startswith('8'):
                clean_phone = '7' + clean_phone[1:]
            elif len(clean_phone) == 10:
                clean_phone = '7' + clean_phone

            # Создаем dadata_client для проверки НПД
            async with DadataClient() as dadata_client:
                bitrix_id = await bitrix_client.send_registration({
                    'name': user.get('name', user_name),
                    'inn': validated_inn,
                    'phone': clean_phone,  # Нормализованный телефон
                    'email': user.get('email', ''),
                    'company_name': org_name,
                    'company_type': company_type,
                    'is_individual': company_type in ('individual', 'npd'),
                }, dadata_client=dadata_client)  # ✅ Передаем dadata_client!

            if bitrix_id:
                logger.info(f"Bitrix24 registration ID: {bitrix_id}")
            else:
                logger.warning("Failed to get valid Bitrix24 ID")

        except Exception as e:
            logger.error(f"Error sending to Bitrix24: {e}")
            bitrix_id = None

        # Генерируем ID если Bitrix24 не вернул
        if bitrix_id:
            logger.info(f"✅ Using Bitrix24 ID: {bitrix_id}")
        else:
            import hashlib
            raw = f"{user.get('phone')}_{validated_inn}"
            local_id = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
            bitrix_id = f"DP-{local_id}"
            logger.warning(f"⚠️ Bitrix24 didn't return ID, using local: {bitrix_id}")

        # Сохраняем регистрацию локально
        try:
            reg_id = await db.save_registration(
                user_id=user_id,
                chat_id=chat_id,
                name=user.get('name', user_name),
                phone=user.get('phone', ''),
                email=user.get('email', ''),
                inn=validated_inn,
                event_name=config.bot.event_name,
                bitrix_id=bitrix_id
            )
            logger.info(f"Registration saved for user {user_id}, reg_id: {reg_id}")

            # Сохраняем данные компании если доступны
            if not is_fallback and company_data:
                await db.save_company_data(user_id, company_data)

        except Exception as e:
            logger.error(f"Failed to save registration for user {user_id}: {e}")
            await event.message.answer("❌ Ошибка сохранения регистрации. Попробуйте позже.")
            return

        if user_id in user_states:
            del user_states[user_id]

        # Генерируем и отправляем QR-код
        if bitrix_id:
            import qrcode
            import io

            qr_data = f"https://bitrix.nneto.ru/?id={bitrix_id}"

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=10,
                border=2,
            )
            qr.add_data(qr_data)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            img = img.resize((300, 300))

            img_bytes = io.BytesIO()
            img.save(img_bytes, format='PNG')
            img_bytes.seek(0)

            message_text = (
                "🎉 Регистрация успешно завершена!\n\n"
                "Вы зарегистрированы на форум «Мой бизнес: ДНИ ПРЕДПРИНИМАТЕЛЬСТВА»\n\n"
                "Ответим на ваши вопросы по телефону 8-800-234-01-24, "
                "программа форума и регистрация на сайте дни-предпринимательства.рф\n\n"
                "Покажите данный QR–код при посещении мероприятий."
            )

            await send_qr_image(event, img_bytes.getvalue(), bitrix_id, message_text)
            logger.info(f"QR code sent to user {user_id}")

        logger.info(f"✓ User {user_id} successfully registered with INN: {validated_inn[:4]}****, type: {inn_type}, ID: {bitrix_id}")
        return
    
    
    
    # Fallback
    logger.warning(f"User {user_id} in unexpected state: {state}, sending fallback")
    keyboard = create_phone_keyboard()
    await event.message.answer(
        "👋 Используйте кнопку ниже для регистрации или отправьте /start",
        attachments=[keyboard]
    )

@dp.message_created(Command('dadata_stats'))
@safe_execute
async def dadata_stats_command(event: MessageCreated):
    """Команда для просмотра статистики DaData (только для админов)"""
    user_id = get_user_id(event)
    
    # Проверяем, является ли пользователь админом
    admin_ids = getattr(config.bot, 'admin_ids', [])
    if user_id not in admin_ids:
        logger.warning(f"Unauthorized access to /dadata_stats from user {user_id}")
        await event.message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    try:
        async with DadataClient() as dadata:
            stats = dadata.get_stats()
            
            message = "📊 Статистика DaData API:\n\n"
            message += f"🔑 API Key настроен: {'✅' if stats['api_key_configured'] else '❌'}\n"
            message += f"🔐 Secret Key настроен: {'✅' if stats['secret_key_configured'] else '❌'}\n"
            message += f"📈 Всего запросов: {stats['request_count']}\n"
            message += f"⚠️ Ошибок: {stats['error_count']}\n"
            
            if stats['request_count'] > 0:
                error_rate = (stats['error_count'] / stats['request_count']) * 100
                message += f"📉 Процент ошибок: {error_rate:.1f}%\n"
            
            await event.message.answer(message)
            
    except Exception as e:
        logger.error(f"Error in dadata_stats command: {e}")
        await event.message.answer("❌ Ошибка при получении статистики DaData.")

def get_company_type_and_name(validation_result: dict, company_data: dict, name: str) -> tuple:
    """
    Определяет тип компании и название для поля Org
    
    Returns:
        (company_type, org_name)
    """
    inn_type = validation_result.get('type', 'FIZ').upper()
    
    type_mapping = {
        'LEGAL': 'organization',
        'INDIVIDUAL': 'individual',
        'NPD': 'npd',
        'FIZ': 'individual'
    }
    
    company_type = type_mapping.get(inn_type, 'individual')
    
    if inn_type == 'INDIVIDUAL':
        org_name = f"ИП {name}"
    elif inn_type == 'NPD':
        org_name = f"Самозанятый {name}"
    elif inn_type == 'LEGAL':
        org_name = company_data.get('name', {}).get('short', name)
    else:
        org_name = name
    
    return company_type, org_name


async def get_qr_image_for_user(user_id: int, phone: str = None) -> Optional[tuple]:
    """Получение QR-кода как изображения для пользователя. Возвращает (bytes, bitrix_id)"""
    try:
        logger.info(f"Generating QR image for user {user_id}")
        
        # Ищем регистрацию по user_id
        registration = await db.get_last_registration(user_id)
        
        # Если не нашли по user_id, но есть телефон - ищем по телефону
        if not registration and phone:
            logger.info(f"No registration by user_id, searching by phone")
            clean_phone = re.sub(r'[^\d]', '', phone)
            registration = await db.get_registration_by_phone(clean_phone)
        
        if not registration:
            logger.warning(f"No registration found for user {user_id}")
            return None
        
        logger.info(f"Registration found: {registration}")
        
        # Проверяем bitrix_id
        bitrix_id = registration.get('bitrix_id')
        
        if not bitrix_id:
            logger.warning(f"No bitrix_id in registration, generating local ID")
            import hashlib
            
            reg_phone = registration.get('phone', '')
            clean_reg_phone = re.sub(r'[^\d]', '', reg_phone)
            reg_inn = registration.get('inn', '')
            
            if clean_reg_phone and reg_inn:
                raw = f"{clean_reg_phone}_{reg_inn}"
                local_id = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
                bitrix_id = f"DP-{local_id}"
                logger.info(f"Generated local ID: {bitrix_id}")
                
                # Сохраняем сгенерированный ID в базу
                await db.update_registration_bitrix_id(registration['id'], bitrix_id)
            else:
                logger.error(f"Cannot generate QR: missing phone or INN")
                return None
        
        # Генерируем QR-код
        import qrcode
        import io
        
        qr_data = f"https://bitrix.nneto.ru/?id={bitrix_id}"
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        img = img.resize((300, 300))
        
        # Сохраняем в байты
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        logger.info(f"QR image generated for user {user_id}, ID: {bitrix_id}")
        return img_bytes.getvalue(), bitrix_id
        
    except Exception as e:
        logger.error(f"Failed to generate QR for user {user_id}: {e}\n{traceback.format_exc()}")
        return None

async def send_qr_image(event, qr_bytes: bytes, bitrix_id: str, message_text: str):
    """Отправка QR-кода как изображения"""
    
    # Сначала посмотрим, какие классы доступны
    from maxapi.types.attachments import Image, File
    import maxapi.types.attachments as att_module
    
    # Логируем содержимое модуля
    logger.info(f"Image class fields: {Image.__fields__.keys() if hasattr(Image, '__fields__') else 'no __fields__'}")
    logger.info(f"Image class annotations: {Image.__annotations__ if hasattr(Image, '__annotations__') else 'no __annotations__'}")
    
    # Пробуем создать Image правильно
    try:
        # Вариант 1: Создаем с type='image'
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://bitrix.neto.ru/?id={bitrix_id}"
        
        # Попробуем разные способы создания
        img = Image(type='image', payload={'url': qr_url})
        
        await event.message.answer(
            text=f"{message_text}\n\n🆔 ID: {bitrix_id}",
            attachments=[img]
        )
        logger.info("QR sent with Image type='image'")
        return
        
    except Exception as e:
        logger.warning(f"Variant 1 failed: {e}")
    
    try:
        # Вариант 2: Используем File вместо Image
        from maxapi.types.attachments import File
        
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://bitrix.neto.ru/?id={bitrix_id}"
        
        file_attachment = File(
            type='file',
            payload={'url': qr_url},
            filename=f'qr_{bitrix_id}.png'
        )
        
        await event.message.answer(
            text=f"{message_text}\n\n🆔 ID: {bitrix_id}",
            attachments=[file_attachment]
        )
        logger.info("QR sent with File attachment")
        return
        
    except Exception as e:
        logger.warning(f"Variant 2 failed: {e}")
    
    # Вариант 3: Просто URL
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://bitrix.neto.ru/?id={bitrix_id}"
    await event.message.answer(
        f"{message_text}\n\n{qr_url}\n\n🆔 ID: {bitrix_id}"
    )

async def get_image_from_message(message) -> Optional[bytes]:
    """Извлечение изображения из сообщения"""
    try:
        if not hasattr(message, 'body') or not message.body:
            return None
        
        body = message.body
        
        # Проверяем attachments через model_dump
        if hasattr(body, 'model_dump'):
            body_dict = body.model_dump()
        elif hasattr(body, 'dict'):
            body_dict = body.dict()
        else:
            return None
        
        if 'attachments' in body_dict:
            for att in body_dict['attachments']:
                if isinstance(att, dict) and att.get('type') == 'image':
                    payload = att.get('payload', {})
                    image_url = payload.get('url')
                    if image_url:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(image_url)
                            if response.status_code == 200:
                                logger.info(f"Image downloaded: {len(response.content)} bytes")
                                return response.content
        
        return None
        
    except Exception as e:
        logger.error(f"Error extracting image: {e}")
        return None
    
async def download_image_from_message(message) -> Optional[bytes]:
    """
    Скачивание изображения из сообщения
    
    Args:
        message: объект сообщения MAX API
        
    Returns:
        Байты изображения или None
    """
    try:
        import httpx
        
        # Проверяем тело сообщения
        if not hasattr(message, 'body') or not message.body:
            logger.debug("No body in message")
            return None
        
        body = message.body
        
        # Способ 1: через model_dump (самый надёжный)
        if hasattr(body, 'model_dump'):
            body_dict = body.model_dump()
        elif hasattr(body, 'dict'):
            body_dict = body.dict()
        else:
            logger.warning("Body has no model_dump/dict method")
            return None
        
        # Ищем изображения в attachments
        if 'attachments' in body_dict:
            for att in body_dict['attachments']:
                if isinstance(att, dict) and att.get('type') == 'image':
                    payload = att.get('payload', {})
                    
                    # Пробуем получить URL изображения
                    image_url = payload.get('url') or payload.get('file_url') or payload.get('link')
                    
                    if image_url:
                        logger.info(f"Downloading image from: {image_url[:100]}...")
                        
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            response = await client.get(image_url)
                            if response.status_code == 200:
                                logger.info(f"✅ Image downloaded: {len(response.content)} bytes")
                                return response.content
                            else:
                                logger.warning(f"Failed to download image: HTTP {response.status_code}")
        
        # Способ 2: прямой перебор attachments (если есть)
        if hasattr(body, 'attachments') and body.attachments:
            for attachment in body.attachments:
                if hasattr(attachment, 'type') and str(attachment.type) == 'image':
                    if hasattr(attachment, 'payload'):
                        payload = attachment.payload
                        if hasattr(payload, 'url'):
                            image_url = payload.url
                        elif isinstance(payload, dict):
                            image_url = payload.get('url')
                        else:
                            continue
                        
                        if image_url:
                            async with httpx.AsyncClient(timeout=30.0) as client:
                                response = await client.get(image_url)
                                if response.status_code == 200:
                                    logger.info(f"✅ Image downloaded: {len(response.content)} bytes")
                                    return response.content
        
        logger.warning("No image URL found in message")
        return None
        
    except Exception as e:
        logger.error(f"Error downloading image: {e}")
        return None    
    
# ============= Запуск =============

async def main():
    """Запуск бота"""
    print("\n" + "=" * 50)
    print(" MAX Registration Bot")
    print("=" * 50)
    
    logger.info("=" * 50)
    logger.info("Starting MAX Registration Bot")
    logger.info(f"Environment: {config.bot.environment}")
    logger.info(f"Event name: {config.bot.event_name}")
    logger.info("=" * 50)
    
    try:
        await db.connect()
        logger.info("Database connected successfully")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        sys.exit(1)
    
    try:
        await bot.delete_webhook()
        logger.info("Webhook subscriptions cleared")
    except Exception as e:
        logger.warning(f"Could not clear webhooks: {e}")
    
    print(f"\n📊 Event: {config.bot.event_name}")
    print(f"🌍 Environment: {config.bot.environment}")
    print(f"📁 Logs: {log_dir}/")
    print("\n🚀 Starting polling...")
    print("=" * 50 + "\n")
    
    logger.info("Starting polling...")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"Unexpected error in polling: {e}\n{traceback.format_exc()}")
    finally:
        await db.close()
        logger.info("Database connection closed")
        logger.info("Shutdown complete")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
        logger.info("Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        logger.error(f"Fatal error: {e}\n{traceback.format_exc()}")
        sys.exit(1)