"""Основной модуль бота для регистрации на мероприятия"""
import asyncio
import logging
import sys
import re
from typing import Optional
from dataclasses import dataclass

from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, Command
from maxapi.types.updates import BotAdded, BotStarted

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
    
    await event.message.answer(
        f"👋 Здравствуйте, {user_name}!\n\n"
        f"Для регистрации на {config.bot.event_name}, "
        "пожалуйста, отправьте свой номер телефона.\n\n"
        "📱 Формат: +7 999 123-45-67"
    )


@dp.message_created(Command('help'))
async def help_command(event: MessageCreated):
    """Команда /help"""
    await event.message.answer(
        "📋 Помощь по боту\n\n"
        "/start - начать регистрацию\n"
        "/help - показать эту справку\n"
        "/status - проверить статус регистрации\n\n"
        f"Мероприятие: {config.bot.event_name}"
    )


@dp.message_created(Command('status'))
async def status_command(event: MessageCreated):
    """Команда /status"""
    user_id = get_user_id(event)
    user = await db.get_user(user_id)
    
    if not user:
        await event.message.answer(
            "Вы еще не начинали регистрацию.\n"
            "Используйте /start чтобы начать."
        )
        return
    
    status = user.get('registration_status', 'pending')
    
    if status == 'completed':
        inn = user.get('inn', 'не указан')
        await event.message.answer(
            f"✅ Вы зарегистрированы!\n\n"
            f"Мероприятие: {config.bot.event_name}\n"
            f"ИНН: {inn[:4]}****{inn[-2:] if len(inn) >= 6 else ''}"
        )
    else:
        state = user.get('state', 'awaiting_phone')
        if state == 'awaiting_phone':
            await event.message.answer(
                "⏳ Ожидается ввод номера телефона.\n"
                "Отправьте номер в формате +7 999 123-45-67"
            )
        elif state == 'awaiting_inn':
            await event.message.answer(
                "⏳ Ожидается ввод ИНН.\n"
                "Отправьте ИНН (10 цифр для организации, 12 для ИП)"
            )


# ============= Обработчики обновлений =============

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    """Бот запущен пользователем"""
    logger.info(f"Bot started by user {event.user.user_id}")
    
    try:
        await bot.send_message(
            user_id=event.user.user_id,
            text=(
                f"👋 Бот для регистрации на {config.bot.event_name} запущен!\n\n"
                "Используйте команду /start для начала регистрации."
            )
        )
    except Exception as e:
        logger.error(f"Failed to send bot_started message: {e}")


@dp.bot_added()
async def bot_added_handler(event: BotAdded):
    """Бот добавлен в чат"""
    logger.info(f"Bot added to chat {event.chat_id}")
    
    try:
        await bot.send_message(
            chat_id=event.chat_id,
            text=(
                f"👋 Спасибо, что добавили меня!\n\n"
                f"Я бот для регистрации на {config.bot.event_name}.\n"
                "Используйте команду /start для начала регистрации."
            )
        )
    except Exception as e:
        logger.error(f"Failed to send bot_added message: {e}")


# ============= Обработчики сообщений =============

@dp.message_created(F.message.body.text)
async def handle_message(event: MessageCreated):
    """Обработка текстовых сообщений"""
    user_id = get_user_id(event)
    text = get_message_text(event)
    user_name = get_user_name(event)
    chat_id = get_chat_id(event)
    
    # Игнорируем команды
    if text and text.startswith('/'):
        return
    
    user = await db.get_user(user_id)
    
    if not user:
        # Новый пользователь
        await db.save_user(
            user_id=user_id,
            chat_id=chat_id,
            name=user_name,
            state='awaiting_phone',
            status='pending'
        )
        user_states[user_id] = 'awaiting_phone'
        await event.message.answer(
            f"👋 Здравствуйте, {user_name}!\n\n"
            "Отправьте ваш номер телефона для регистрации.\n"
            "Формат: +7 999 123-45-67"
        )
        return
    
    if user.get('registration_status') == 'completed':
        await event.message.answer(
            "✅ Вы уже зарегистрированы!\n"
            "Используйте /status для просмотра информации."
        )
        return
    
    state = user.get('state') or user_states.get(user_id, 'awaiting_phone')
    
    if state == 'awaiting_phone':
        validated = validate_phone(text)
        if not validated:
            await event.message.answer(
                "❌ Неверный формат номера.\n"
                "Отправьте номер в формате: +7 999 123-45-67"
            )
            return
        
        await db.save_user(
            user_id=user_id,
            phone=validated.number,
            state='awaiting_inn',
            status='pending'
        )
        user_states[user_id] = 'awaiting_inn'
        
        await event.message.answer(
            f"✅ Номер {validated.formatted} принят!\n\n"
            "Теперь отправьте ИНН:\n"
            "• 10 цифр для организации\n"
            "• 12 цифр для ИП"
        )
    
    elif state == 'awaiting_inn':
        validated = validate_inn(text)
        if not validated:
            await event.message.answer(
                "❌ Неверный ИНН. Должно быть 10 или 12 цифр."
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
        
        inn_type = "организации" if validated.type == 'organization' else "ИП"
        
        await event.message.answer(
            f"✅ Регистрация завершена!\n\n"
            f"📋 ИНН {inn_type}: {validated.number}\n"
            f"🔢 Номер регистрации: {reg_id}\n\n"
            f"Спасибо за регистрацию на {config.bot.event_name}! 🎉"
        )
        
        logger.info(f"User {user_id} registered with INN: {validated.number[:4]}****")


@dp.message_created()
async def fallback_handler(event: MessageCreated):
    """Обработчик для всех остальных сообщений"""
    await event.message.answer(
        "Используйте команду /start для начала регистрации\n"
        "или /help для получения справки."
    )


# ============= Запуск =============

async def main():
    """Запуск бота"""
    print("\n" + "=" * 50)
    print("🤖 MAX Registration Bot")
    print("=" * 50)
    
    await db.connect()
    
    # Удаляем старые webhook подписки для polling
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