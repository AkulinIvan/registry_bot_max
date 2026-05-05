"""Сервис администратора для массовой рассылки сообщений"""
import asyncio
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import traceback

from maxapi import Bot
from maxapi.exceptions.max import MaxApiError

from config import AppConfig
from database import Database

logger = logging.getLogger(__name__)


class BroadcastStatus(Enum):
    """Статусы рассылки"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BroadcastResult:
    """Результат рассылки"""
    total_users: int = 0
    successful: int = 0
    failed: int = 0
    blocked: int = 0
    errors: List[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []
    
    @property
    def success_rate(self) -> float:
        """Процент успешных отправок"""
        if self.total_users == 0:
            return 0.0
        return (self.successful / self.total_users) * 100
    
    def add_error(self, user_id: int, error: str):
        """Добавление ошибки"""
        self.errors.append({
            'user_id': user_id,
            'error': str(error),
            'timestamp': datetime.now().isoformat()
        })
        self.failed += 1


class AdminService:
    """Сервис для административных функций"""
    
    def __init__(self, bot: Bot, db: Database, config: AppConfig):
        self.bot = bot
        self.db = db
        self.config = config
        self.active_broadcasts: Dict[str, BroadcastResult] = {}
        self.broadcast_status = BroadcastStatus.PENDING
        self._cancel_broadcast = False
        
        logger.info("AdminService initialized")
    
    def is_admin(self, user_id: int) -> bool:
        """Проверка прав администратора"""
        return self.config.bot.is_admin(user_id)
    
    async def get_all_users(self, only_active: bool = True) -> List[Dict[str, Any]]:
        """
        Получение списка всех пользователей бота
        
        Args:
            only_active: Только активные пользователи (не заблокировавшие бота)
        """
        try:
            # Получаем всех пользователей из БД
            query = """
                SELECT DISTINCT user_id, chat_id, name, registration_status, created_at
                FROM users
                WHERE 1=1
            """
            
            if only_active:
                query += " AND registration_status != 'blocked'"
            
            query += " ORDER BY created_at DESC"
            
            async with self.db.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query)
                    users = await cur.fetchall()
            
            logger.info(f"Retrieved {len(users)} users for broadcast")
            return users
            
        except Exception as e:
            logger.error(f"Failed to get users list: {e}\n{traceback.format_exc()}")
            return []
    
    async def send_broadcast(
        self,
        message_text: str,
        sender_id: int,
        broadcast_type: str = "all",
        test_mode: bool = False
    ) -> BroadcastResult:
        """
        Массовая рассылка сообщений
        
        Args:
            message_text: Текст сообщения для рассылки
            sender_id: ID администратора, запустившего рассылку
            broadcast_type: Тип рассылки ('all', 'registered', 'pending')
            test_mode: Тестовый режим (отправка только админу)
        """
        # Проверяем права
        if not self.is_admin(sender_id):
            raise PermissionError(f"User {sender_id} is not an admin")
        
        # Генерируем ID рассылки
        broadcast_id = f"broadcast_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Инициализируем результат
        result = BroadcastResult()
        self.active_broadcasts[broadcast_id] = result
        self.broadcast_status = BroadcastStatus.IN_PROGRESS
        self._cancel_broadcast = False
        
        logger.info(f"Starting broadcast {broadcast_id} by admin {sender_id}")
        logger.info(f"Type: {broadcast_type}, Test mode: {test_mode}")
        
        try:
            # Получаем список пользователей
            users = await self.get_all_users()
            
            if test_mode:
                # В тестовом режиме отправляем только админу
                users = [u for u in users if u[0] == sender_id]
                logger.info(f"Test mode: sending only to admin {sender_id}")
            
            if not users:
                logger.warning("No users found for broadcast")
                self.broadcast_status = BroadcastStatus.COMPLETED
                return result
            
            result.total_users = len(users)
            logger.info(f"Broadcasting to {result.total_users} users")
            
            # Отправляем сообщения с задержкой чтобы не перегружать API
            delay = 0.05  # 50ms между отправками
            
            for i, user in enumerate(users):
                if self._cancel_broadcast:
                    logger.info(f"Broadcast {broadcast_id} cancelled by admin")
                    self.broadcast_status = BroadcastStatus.CANCELLED
                    break
                
                user_id = user[0]
                chat_id = user[1]
                
                try:
                    if chat_id:
                        # Отправляем в чат если есть chat_id
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=message_text
                        )
                    else:
                        # Иначе отправляем по user_id
                        await self.bot.send_message(
                            user_id=user_id,
                            text=message_text
                        )
                    
                    result.successful += 1
                    logger.debug(f"Message sent to user {user_id} ({i+1}/{result.total_users})")
                    
                except MaxApiError as e:
                    if "chat.not.found" in str(e) or "bot.blocked" in str(e):
                        # Пользователь заблокировал бота или удалил чат
                        result.blocked += 1
                        logger.warning(f"User {user_id} blocked bot or chat not found")
                        
                        # Помечаем пользователя как заблокированного
                        try:
                            await self.db.save_user(
                                user_id=user_id,
                                status='blocked'
                            )
                        except Exception as db_error:
                            logger.error(f"Failed to update blocked status for user {user_id}: {db_error}")
                    else:
                        result.add_error(user_id, f"MAX API Error: {e}")
                        logger.error(f"Failed to send to user {user_id}: {e}")
                        
                except Exception as e:
                    result.add_error(user_id, f"Unexpected error: {e}")
                    logger.error(f"Unexpected error sending to user {user_id}: {e}\n{traceback.format_exc()}")
                
                # Задержка между отправками
                if i < len(users) - 1:
                    await asyncio.sleep(delay)
                
                # Логируем прогресс каждые 100 отправок
                if (i + 1) % 100 == 0:
                    logger.info(f"Broadcast progress: {i+1}/{result.total_users} "
                              f"(success: {result.successful}, failed: {result.failed}, "
                              f"blocked: {result.blocked})")
            
            if not self._cancel_broadcast:
                self.broadcast_status = BroadcastStatus.COMPLETED
            
            # Логируем итоги
            logger.info(f"Broadcast {broadcast_id} completed:")
            logger.info(f"  Total: {result.total_users}")
            logger.info(f"  Successful: {result.successful}")
            logger.info(f"  Failed: {result.failed}")
            logger.info(f"  Blocked: {result.blocked}")
            logger.info(f"  Success rate: {result.success_rate:.1f}%")
            
            # Сохраняем статистику в БД
            await self._save_broadcast_stats(broadcast_id, sender_id, result)
            
            return result
            
        except Exception as e:
            logger.error(f"Broadcast {broadcast_id} failed: {e}\n{traceback.format_exc()}")
            self.broadcast_status = BroadcastStatus.FAILED
            raise
        
        finally:
            # Очищаем активные рассылки через час
            asyncio.create_task(self._cleanup_broadcast(broadcast_id, delay=3600))
    
    async def cancel_broadcast(self):
        """Отмена текущей рассылки"""
        if self.broadcast_status == BroadcastStatus.IN_PROGRESS:
            self._cancel_broadcast = True
            logger.info("Broadcast cancellation requested")
            return True
        return False
    
    async def _save_broadcast_stats(
        self,
        broadcast_id: str,
        admin_id: int,
        result: BroadcastResult
    ):
        """Сохранение статистики рассылки в БД"""
        try:
            await self.db.save_broadcast_stats(
                broadcast_id=broadcast_id,
                admin_id=admin_id,
                total_users=result.total_users,
                successful=result.successful,
                failed=result.failed,
                blocked=result.blocked,
                status=self.broadcast_status.value
            )
            logger.info(f"Broadcast stats saved: {broadcast_id}")
        except Exception as e:
            logger.error(f"Failed to save broadcast stats: {e}")
    
    async def _cleanup_broadcast(self, broadcast_id: str, delay: int = 3600):
        """Очистка данных о рассылке через указанное время"""
        await asyncio.sleep(delay)
        if broadcast_id in self.active_broadcasts:
            del self.active_broadcasts[broadcast_id]
            logger.debug(f"Cleaned up broadcast {broadcast_id}")
    
    async def get_broadcast_stats(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Получение статистики последних рассылок"""
        return await self.db.get_broadcast_stats(limit)
    
    async def get_user_count(self) -> Dict[str, int]:
        """Получение количества пользователей по статусам"""
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # Общее количество
                    await cur.execute("SELECT COUNT(*) FROM users")
                    total = (await cur.fetchone())[0]
                    
                    # Активные (завершившие регистрацию)
                    await cur.execute(
                        "SELECT COUNT(*) FROM users WHERE registration_status = 'completed'"
                    )
                    completed = (await cur.fetchone())[0]
                    
                    # В процессе регистрации
                    await cur.execute(
                        "SELECT COUNT(*) FROM users WHERE registration_status = 'pending'"
                    )
                    pending = (await cur.fetchone())[0]
                    
                    # Заблокированные
                    await cur.execute(
                        "SELECT COUNT(*) FROM users WHERE registration_status = 'blocked'"
                    )
                    blocked = (await cur.fetchone())[0]
                    
                    return {
                        'total': total,
                        'completed': completed,
                        'pending': pending,
                        'blocked': blocked,
                        'active': total - blocked
                    }
                    
        except Exception as e:
            logger.error(f"Failed to get user count: {e}")
            return {
                'total': 0,
                'completed': 0,
                'pending': 0,
                'blocked': 0,
                'active': 0
            }


# Форматтер для красивого вывода результатов рассылки
def format_broadcast_result(result: BroadcastResult) -> str:
    """Форматирование результатов рассылки для сообщения"""
    return (
        "📊 Результаты рассылки\n\n"
        f"👥 Всего пользователей: {result.total_users}\n"
        f"✅ Успешно отправлено: {result.successful}\n"
        f"❌ Ошибок отправки: {result.failed}\n"
        f"🚫 Заблокировали бота: {result.blocked}\n"
        f"📈 Процент доставки: {result.success_rate:.1f}%\n\n"
        f"⏱ Время завершения: {datetime.now().strftime('%H:%M:%S')}"
    )