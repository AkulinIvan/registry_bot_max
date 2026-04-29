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
    """Расширенная валидация ИНН через DaData API"""
    logger.debug(f"Validating INN with DaData: {inn[:4] if inn else 'empty'}****")
    
    if not inn:
        logger.warning("INN validation failed: empty INN")
        return None
    
    clean = re.sub(r'[\s\-]', '', inn.strip())
    
    if len(clean) not in (10, 12) or not clean.isdigit():
        logger.warning(f"INN validation failed: invalid format (len={len(clean)}, isdigit={clean.isdigit()})")
        return None
    
    try:
        async with DadataClient() as dadata:
            company = await dadata.find_company_by_inn(clean)
            
            if company:
                logger.info(f"✅ Company found via DaData: {company['name']['short']}")
                
                # Проверяем, активна ли компания
                if not company['is_active']:
                    logger.warning(f"Company {clean[:4]}**** is not active: {company['state']['status']}")
                    return {
                        "valid": False,
                        "error": "company_inactive",
                        "status": company['state']['status'],
                        "company": company
                    }
                
                return {
                    "valid": True,
                    "number": clean,
                    "type": company['type'].lower(),
                    "company": company
                }
            else:
                logger.warning(f"INN {clean[:4]}**** not found in DaData")
                return {
                    "valid": False,
                    "error": "not_found",
                    "number": clean
                }
                
    except Exception as e:
        logger.error(f"DaData API error for INN {clean[:4]}****: {e}")
        # Fallback: базовая валидация без DaData
        inn_type = 'organization' if len(clean) == 10 else 'individual'
        logger.info(f"Using fallback validation: INN type={inn_type}")
        return {
            "valid": True,
            "number": clean,
            "type": inn_type,
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
    
    # Получаем user_id из callback.user (правильное место!)
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
            "📱 Нажмите кнопку ниже или отправьте номер текстом:\n"
            "+7 999 123-45-67",
            attachments=[keyboard]
        )
    
    elif callback_data == "menu_announcements":
        await event.message.answer(
            "📢 Анонсы мероприятий\n\n"
            "Ближайшие мероприятия:\n"
            "• 15 июня - Мастер-класс по маркетингу\n"
            "• 22 июня - Нетворкинг-встреча\n"
            "• 1 июля - Бизнес-завтрак с экспертами\n\n"
            "Следите за обновлениями!",
            attachments=[get_back_keyboard()]
        )
    
    elif callback_data == "menu_collaboration":
        await event.message.answer(
            "🤝 <b>Хочу коллаборацию</b>\n\n"
            "Для поиска партнеров и коллабораций:\n\n"
            "📧 Email: collaboration@example.com\n"
            "📱 Telegram: @collab_manager\n\n"
            "Или заполните форму на сайте.",
            attachments=[get_back_keyboard()]
        )
    
    elif callback_data == "back_to_menu":
        if user_id in user_states:
            del user_states[user_id]
        await event.message.answer(
            "👋 Главное меню:\n\nВыберите интересующий вас раздел:",
            attachments=[create_main_menu_keyboard()]
        )
    
    

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
    
    # Игнорируем команды
    if text and text.startswith('/'):
        logger.debug(f"Ignoring command: {text}")
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
                "Нажмите кнопку ниже, чтобы поделиться номером телефона "
                "или отправьте его вручную в формате: +7 999 123-45-67",
                attachments=[keyboard]
            )
        return
    
    # Если уже зарегистрирован
    if user.get('registration_status') == 'completed':
        logger.info(f"User {user_id} is already registered")
        await event.message.answer(
            "✅ Вы уже зарегистрированы!\n"
            "До встречи на мероприятии!"
        )
        return
    
    state = user.get('state') or user_states.get(user_id, 'awaiting_phone')
    logger.info(f"User {user_id} current state: {state}")
    
    # Обработка состояния ожидания телефона
    if state == 'awaiting_phone':
        logger.info(f"Processing phone input for user {user_id}")

        # Если есть контакт - используем его
        if phone:
            validated = validate_phone(phone)
        elif text:
            validated = validate_phone(text)
        else:
            validated = None

        if not validated:
            logger.warning(f"Invalid phone format for user {user_id}")
            keyboard = create_phone_keyboard()
            await event.message.answer(
                "❌ Неверный формат номера.\n"
                "Нажмите кнопку ниже или отправьте номер в формате: +7 999 123-45-67",
                attachments=[keyboard]
            )
            return

        try:
            await db.save_user(
                user_id=user_id,
                phone=validated.number,
                state='awaiting_email',
                status='pending'
            )
            logger.info(f"Phone saved for user {user_id}")
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
                "• 12 цифр для ИП"
            )
            return
        
        # Отправляем сообщение о проверке
        checking_msg = await event.message.answer("🔍 Проверяем ИНН через базу ФНС...")
        
        # Расширенная валидация через DaData
        validation_result = await validate_inn(text)
        
        if not validation_result or not validation_result.get("valid"):
            error_type = validation_result.get("error") if validation_result else "invalid_format"
            
            if error_type == "company_inactive":
                status = validation_result.get("status", "неизвестно")
                company = validation_result.get("company", {})
                await event.message.answer(
                    f"⚠️ Компания с ИНН {text} имеет статус: {status}\n"
                    f"📌 {company.get('name', {}).get('short', 'Неизвестно')}\n\n"
                    "Регистрация возможна только для действующих организаций и ИП.\n"
                    "Если это ошибка, свяжитесь с организаторами."
                )
            elif error_type == "not_found":
                await event.message.answer(
                    f"❌ ИНН {text} не найден в базе ФНС.\n"
                    "Проверьте правильность ввода и попробуйте ещё раз:"
                )
            else:
                await event.message.answer(
                    "❌ Неверный ИНН. Должно быть 10 или 12 цифр.\n"
                    "Попробуйте ещё раз:"
                )
            return
        
        validated_inn = validation_result["number"]
        company_data = validation_result.get("company", {})
        is_fallback = validation_result.get("fallback", False)
        
        if is_fallback:
            logger.warning(f"Using fallback validation for INN {validated_inn[:4]}****")
            await event.message.answer(
                "⚠️ Временно недоступна проверка по базе ФНС.\n"
                "ИНН принят, но рекомендуем проверить его корректность."
            )
        else:
            # Показываем информацию о компании
            company_info = f"✅ ИНН проверен: {company_data.get('name', {}).get('short', 'Неизвестно')}\n"
            if company_data.get('address', {}).get('value'):
                company_info += f"📍 {company_data['address']['value'][:100]}...\n"
            company_info += f"📊 Статус: {'Действующая' if company_data.get('is_active') else company_data.get('state', {}).get('status', 'Неизвестно')}"
            
            await event.message.answer(company_info)
        
        try:
            # Проверяем дубликат в Bitrix24
            duplicate_in_bitrix = await bitrix_client.check_duplicate(
                inn=validated_inn,
                phone=user.get('phone', '')
            )
            
            if duplicate_in_bitrix:
                logger.warning(f"Duplicate found in Bitrix24 for user {user_id}")
                await event.message.answer(
                    "⚠️ Пользователь с таким ИНН или телефоном уже зарегистрирован в системе.\n"
                    "Если это ошибка, свяжитесь с организаторами."
                )
                return
                
        except Exception as e:
            logger.error(f"Failed to check duplicate in Bitrix24: {e}")
            
        company_type, org_name = get_company_type_and_name(validation_result, company_data, user.get('name', user_name))
        
        try:
            # Отправляем данные в Bitrix24 с правильными полями
            bitrix_id = await bitrix_client.send_registration({
                'name': user.get('name', user_name),
                'inn': validated_inn,
                'phone': user.get('phone', ''),
                'email': user.get('email', ''),
                'company_name': org_name,
                'company_type': company_type,
                'is_individual': company_type == 'individual',
            })
            
            if bitrix_id:
                logger.info(f"Bitrix24 registration ID: {bitrix_id}")
            else:
                logger.warning("Failed to get valid Bitrix24 ID")
                
        except Exception as e:
            logger.error(f"Error sending to Bitrix24: {e}")
            bitrix_id = None
        
        # Если Bitrix24 не вернул ID, генерируем локальный
        if not bitrix_id:
            import hashlib
            raw = f"{user.get('phone')}_{validated_inn}"
            local_id = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
            bitrix_id = f"DP-{local_id}"
            logger.info(f"Using local ID: {bitrix_id}")
        
        try:
            # Сохраняем регистрацию локально
            reg_id = await db.save_registration(
                user_id=user_id,
                chat_id=chat_id,
                name=user.get('name', user_name),
                phone=user.get('phone', ''),
                email=user.get('email', ''),
                inn=validated_inn,
                event_name=config.bot.event_name
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
        
        success_message = "🎉 Отлично! Вы успешно зарегистрированы на мероприятие!\n\n"
        
        if bitrix_id:
            success_message += f"🆔 Ваш ID регистрации: <b>{bitrix_id}</b>\n"
            success_message += "📱 Покажите этот номер при входе на мероприятие.\n\n"
        
        if not is_fallback and company_data:
            success_message += f"🏢 Организация: {company_data.get('name', {}).get('short', 'Неизвестно')}\n"
        
        success_message += "\nМы свяжемся с вами по указанному номеру телефона. До встречи! 👋"
        
        await event.message.answer(success_message)
        
        if bitrix_id:
            await event.message.answer(
                f"🔢 Ваш ID для входа: <code>{bitrix_id}</code>\n\n"
                "Сохраните этот номер!"
            )
        
        logger.info(f"✓ User {user_id} successfully registered with INN: {validated_inn[:4]}****, ID: {bitrix_id}")
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
    raw_type = validation_result.get('type', '').upper()
    is_fallback = validation_result.get('fallback', False)
    
    if is_fallback or not company_data:
        # Fallback: просто ФИО
        return 'individual', name
    
    # DaData возвращает: 'INDIVIDUAL' для ИП, 'LEGAL' для ООО/АО
    if raw_type == 'INDIVIDUAL':
        return 'individual', f"ИП {name}"
    elif raw_type == 'LEGAL':
        company_name = company_data.get('name', {}).get('short', name)
        return 'organization', company_name
    else:
        # Самозанятый или другой тип
        return 'unknown', name

        
# ============= Запуск =============

async def main():
    """Запуск бота"""
    print("\n" + "=" * 50)
    print("🤖 MAX Registration Bot")
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