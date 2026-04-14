"""Основной модуль бота для регистрации на мероприятия"""
import asyncio
import logging
import sys
import re
from typing import Optional
from dataclasses import dataclass

from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, Command
from maxapi.types.updates import BotStarted
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import RequestContactButton

from config import AppConfig
from database import Database

# Настройка логирования
config = AppConfig()
logging.basicConfig(
    level=getattr(logging, config.bot.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация
bot = Bot(token=config.bot.token)
dp = Dispatcher()
db = Database()

# Хранилище состояний
user_states = {}


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
    if not phone:
        return None
    clean = re.sub(r'[^\d]', '', phone.strip())
    if len(clean) not in (10, 11):
        return None
    if len(clean) == 11:
        if clean.startswith('8'):
            clean = '7' + clean[1:]
        elif not clean.startswith('7'):
            return None
    elif len(clean) == 10:
        clean = '7' + clean
    formatted = f"+7 ({clean[1:4]}) {clean[4:7]}-{clean[7:9]}-{clean[9:11]}"
    return ValidatedPhone(number=clean, formatted=formatted)


def validate_inn(inn: str) -> Optional[ValidatedINN]:
    """Валидация ИНН"""
    if not inn:
        return None
    clean = re.sub(r'[\s\-]', '', inn.strip())
    if len(clean) not in (10, 12) or not clean.isdigit():
        return None
    inn_type = 'organization' if len(clean) == 10 else 'individual'
    return ValidatedINN(number=clean, type=inn_type)


def get_user_id(event: MessageCreated) -> int:
    """Получение user_id из события"""
    if event.message.sender:
        return event.message.sender.user_id
    return 0


def get_user_name(event: MessageCreated) -> str:
    """Получение имени пользователя из события"""
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


def get_chat_id(event: MessageCreated) -> Optional[int]:
    """Получение chat_id из события"""
    if event.message.recipient:
        return event.message.recipient.chat_id
    return None


def get_message_text(event: MessageCreated) -> Optional[str]:
    """Получение текста сообщения"""
    if event.message.body:
        return event.message.body.text
    return None


def extract_phone_from_message(message) -> Optional[str]:
    """Извлечение номера телефона из сообщения"""
    # Логируем структуру сообщения для отладки
    logger.info(f"Message dir: {[attr for attr in dir(message) if not attr.startswith('_')]}")
    
    # Проверяем разные возможные места хранения контакта
    if hasattr(message, 'contact'):
        contact = message.contact
        logger.info(f"Contact found: {contact}")
        if hasattr(contact, 'phone_number'):
            return contact.phone_number
        if hasattr(contact, 'phone'):
            return contact.phone
    
    if hasattr(message, 'body'):
        body = message.body
        if hasattr(body, 'contact'):
            contact = body.contact
            if hasattr(contact, 'phone_number'):
                return contact.phone_number
    
    return None


def create_phone_keyboard():
    """Создание клавиатуры с кнопкой для отправки номера телефона"""
    builder = InlineKeyboardBuilder()
    builder.row(
        RequestContactButton(
            text="📱 Поделиться номером телефона"
        )
    )
    return builder.as_markup()


# ============= Обработчики команд =============

@dp.message_created(Command('start'))
async def start_command(event: MessageCreated):
    """Команда /start"""
    user_id = get_user_id(event)
    user_name = get_user_name(event)
    chat_id = get_chat_id(event)
    
    logger.info(f"Start command from user {user_id} ({user_name})")
    
    await db.save_user(
        user_id=user_id,
        chat_id=chat_id,
        name=user_name,
        state='awaiting_phone',
        status='pending'
    )
    user_states[user_id] = 'awaiting_phone'
    
    keyboard = create_phone_keyboard()
    
    await event.message.answer(
        "👋 Добро пожаловать!\n\n"
        "Вы можете зарегистрироваться на наше мероприятие прямо здесь.\n\n"
        "Нажмите кнопку ниже, чтобы поделиться своим номером телефона "
        "и подтвердить участие. Это займёт всего несколько секунд!",
        attachments=[keyboard]
    )


# ============= Обработчики обновлений =============

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    """Бот запущен пользователем"""
    logger.info(f"Bot started by user {event.user.user_id}")
    
    try:
        keyboard = create_phone_keyboard()
        await bot.send_message(
            user_id=event.user.user_id,
            text=(
                "👋 Добро пожаловать!\n\n"
                "Вы можете зарегистрироваться на наше мероприятие прямо здесь.\n\n"
                "Нажмите кнопку ниже, чтобы поделиться своим номером телефона "
                "и подтвердить участие. Это займёт всего несколько секунд!"
            ),
            attachments=[keyboard]
        )
    except Exception as e:
        logger.error(f"Failed to send bot_started message: {e}")


# ============= Основной обработчик сообщений =============

@dp.message_created()
async def handle_all_messages(event: MessageCreated):
    """Обработка всех сообщений"""
    user_id = get_user_id(event)
    user_name = get_user_name(event)
    chat_id = get_chat_id(event)
    
    logger.info(f"Message from user {user_id}")
    
    # Получаем текст сообщения
    text = get_message_text(event)
    
    # Пробуем извлечь телефон из контакта
    phone = extract_phone_from_message(event.message)
    if phone:
        logger.info(f"Contact phone extracted: {phone}")
    
    # Игнорируем команды
    if text and text.startswith('/'):
        return
    
    # Получаем пользователя из БД
    user = await db.get_user(user_id)
    
    # Если пользователь не найден - создаем
    if not user:
        await db.save_user(
            user_id=user_id,
            chat_id=chat_id,
            name=user_name,
            state='awaiting_phone',
            status='pending'
        )
        user_states[user_id] = 'awaiting_phone'
        
        keyboard = create_phone_keyboard()
        await event.message.answer(
            "👋 Добро пожаловать!\n\n"
            "Нажмите кнопку ниже, чтобы поделиться номером телефона "
            "или отправьте его вручную в формате: +7 999 123-45-67",
            attachments=[keyboard]
        )
        return
    
    # Если уже зарегистрирован
    if user.get('registration_status') == 'completed':
        await event.message.answer(
            "✅ Вы уже зарегистрированы!\n"
            "До встречи на мероприятии!"
        )
        return
    
    state = user.get('state') or user_states.get(user_id, 'awaiting_phone')
    logger.info(f"User {user_id} state: {state}")
    
    # Обработка состояния ожидания телефона
    if state == 'awaiting_phone':
        # Если есть контакт - используем его
        if phone:
            validated = validate_phone(phone)
        elif text:
            validated = validate_phone(text)
        else:
            validated = None
        
        if not validated:
            keyboard = create_phone_keyboard()
            await event.message.answer(
                "❌ Неверный формат номера.\n"
                "Нажмите кнопку ниже или отправьте номер в формате: +7 999 123-45-67",
                attachments=[keyboard]
            )
            return
        
        await db.save_user(
            user_id=user_id,
            phone=validated.number,
            state='awaiting_inn',
            status='pending'
        )
        user_states[user_id] = 'awaiting_inn'
        
        await event.message.answer("🔄 Регистрируем вас...")
        
        await event.message.answer(
            "📋 Отлично! Теперь отправьте ваш ИНН:\n"
            "• 10 цифр для организации\n"
            "• 12 цифр для ИП"
        )
        return
    
    # Обработка состояния ожидания ИНН
    elif state == 'awaiting_inn':
        if not text:
            await event.message.answer(
                "📋 Пожалуйста, отправьте ИНН текстом:\n"
                "• 10 цифр для организации\n"
                "• 12 цифр для ИП"
            )
            return
        
        validated = validate_inn(text)
        if not validated:
            await event.message.answer(
                "❌ Неверный ИНН. Должно быть 10 или 12 цифр.\n"
                "Попробуйте ещё раз:"
            )
            return
        
        if await db.check_inn_exists(validated.number):
            await event.message.answer(
                "⚠️ Этот ИНН уже зарегистрирован.\n"
                "Если это ошибка, свяжитесь с организаторами."
            )
            return
        
        reg_id = await db.save_registration(
            user_id=user_id,
            chat_id=chat_id,
            name=user_name,
            phone=user['phone'],
            inn=validated.number,
            event_name=config.bot.event_name
        )
        
        if user_id in user_states:
            del user_states[user_id]
        
        await event.message.answer(
            "🎉 Отлично! Вы успешно зарегистрированы на мероприятие!\n\n"
            "Мы свяжемся с вами по указанному номеру телефона. До встречи! 👋"
        )
        
        logger.info(f"User {user_id} registered successfully")
        return
    
    # Fallback
    keyboard = create_phone_keyboard()
    await event.message.answer(
        "👋 Используйте кнопку ниже для регистрации или отправьте /start",
        attachments=[keyboard]
    )


# ============= Запуск =============

async def main():
    """Запуск бота"""
    print("\n" + "=" * 50)
    print("🤖 MAX Registration Bot")
    print("=" * 50)
    
    await db.connect()
    
    try:
        await bot.delete_webhook()
        logger.info("Webhook subscriptions cleared")
    except Exception as e:
        logger.warning(f"Could not clear webhooks: {e}")
    
    print(f"\n📊 Event: {config.bot.event_name}")
    print(f"🌍 Environment: {config.bot.environment}")
    print("\n🚀 Starting polling...")
    print("=" * 50 + "\n")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        await db.close()
        logger.info("Shutdown complete")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
        sys.exit(0)