"""Модуль для работы с базой данных MySQL"""
import asyncio
import aiomysql
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

from config import AppConfig

config = AppConfig()
logger = logging.getLogger(__name__)


class Database:
    """Асинхронный класс для работы с MySQL"""
    
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        """Создание пула соединений"""
        try:
            self.pool = await aiomysql.create_pool(
                host=config.db.host,
                port=config.db.port,
                db=config.db.name,
                user=config.db.user,
                password=config.db.password,
                minsize=2,
                maxsize=10,
                autocommit=True,
                charset='utf8mb4'
            )
            logger.info("✅ MySQL connection pool created")
            await self.init_schema()
        except Exception as e:
            logger.error(f"❌ MySQL connection failed: {e}")
            raise
    
    async def close(self):
        """Закрытие пула соединений"""
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            logger.info("MySQL connection pool closed")
    
    async def init_schema(self):
        """Инициализация схемы базы данных"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Таблица пользователей
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT UNIQUE NOT NULL,
                        chat_id BIGINT,
                        name VARCHAR(255),
                        phone VARCHAR(20),
                        inn VARCHAR(12),
                        state VARCHAR(50) DEFAULT 'awaiting_phone',
                        registration_status VARCHAR(50) DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        registered_at TIMESTAMP NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                logger.info("✅ Table 'users' created/verified")
                
                # Таблица регистраций
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS registrations (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        chat_id BIGINT,
                        name VARCHAR(255) NOT NULL,
                        phone VARCHAR(20) NOT NULL,
                        inn VARCHAR(12) NOT NULL,
                        event_name VARCHAR(255) NOT NULL,
                        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(50) DEFAULT 'active',
                        INDEX idx_user_id (user_id),
                        INDEX idx_inn (inn),
                        INDEX idx_registration_date (registration_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                logger.info("✅ Table 'registrations' created/verified")
                
                # Индексы для users
                try:
                    await cur.execute("CREATE INDEX idx_users_user_id ON users(user_id)")
                except Exception:
                    pass  # Индекс уже существует
                
                try:
                    await cur.execute("CREATE INDEX idx_users_state ON users(state)")
                except Exception:
                    pass
                
                logger.info("✅ Database schema initialized successfully")
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение пользователя по ID"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM users WHERE user_id = %s",
                    (user_id,)
                )
                return await cur.fetchone()
    
    async def save_user(
        self,
        user_id: int,
        chat_id: int = None,
        name: str = None,
        phone: str = None,
        inn: str = None,
        state: str = None,
        status: str = None
    ) -> int:
        """Сохранение или обновление пользователя"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Проверяем существование пользователя
                await cur.execute(
                    "SELECT id FROM users WHERE user_id = %s",
                    (user_id,)
                )
                existing = await cur.fetchone()
                
                if existing:
                    # Обновляем существующего
                    updates = []
                    params = []
                    
                    if chat_id is not None:
                        updates.append("chat_id = %s")
                        params.append(chat_id)
                    if name is not None:
                        updates.append("name = %s")
                        params.append(name)
                    if phone is not None:
                        updates.append("phone = %s")
                        params.append(phone)
                    if inn is not None:
                        updates.append("inn = %s")
                        params.append(inn)
                    if state is not None:
                        updates.append("state = %s")
                        params.append(state)
                    if status is not None:
                        updates.append("registration_status = %s")
                        params.append(status)
                    
                    if updates:
                        params.append(user_id)
                        await cur.execute(
                            f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s",
                            params
                        )
                    
                    return existing[0]
                else:
                    # Создаем нового
                    await cur.execute("""
                        INSERT INTO users (user_id, chat_id, name, phone, inn, state, registration_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, chat_id, name, phone, inn, state, status or 'pending'))
                    
                    return cur.lastrowid
    
    async def save_registration(
        self,
        user_id: int,
        chat_id: int,
        name: str,
        phone: str,
        inn: str,
        event_name: str
    ) -> int:
        """Сохранение регистрации"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Сохраняем регистрацию
                await cur.execute("""
                    INSERT INTO registrations (user_id, chat_id, name, phone, inn, event_name)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, chat_id, name, phone, inn, event_name))
                
                reg_id = cur.lastrowid
                
                # Обновляем статус пользователя
                await cur.execute("""
                    UPDATE users 
                    SET registration_status = 'completed',
                        registered_at = NOW(),
                        state = 'registered',
                        inn = %s
                    WHERE user_id = %s
                """, (inn, user_id))
                
                return reg_id
    
    async def check_inn_exists(self, inn: str) -> bool:
        """Проверка существования ИНН"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM registrations WHERE inn = %s LIMIT 1",
                    (inn,)
                )
                result = await cur.fetchone()
                return result is not None
    
    async def get_stats(self) -> Dict[str, Any]:
        """Получение статистики"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM registrations")
                total = (await cur.fetchone())[0]
                
                await cur.execute(
                    "SELECT COUNT(*) FROM users WHERE registration_status = 'completed'"
                )
                completed = (await cur.fetchone())[0]
                
                await cur.execute(
                    "SELECT COUNT(*) FROM users WHERE registration_status = 'pending'"
                )
                pending = (await cur.fetchone())[0]
                
                return {
                    "total_registrations": total,
                    "completed": completed,
                    "pending": pending,
                    "timestamp": datetime.utcnow().isoformat()
                }


# Для отладки
async def test_connection():
    """Тест подключения к БД"""
    db = Database()
    try:
        await db.connect()
        print("✅ Connection successful!")
        
        # Проверяем таблицы
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                tables = await cur.fetchall()
                print(f"Tables: {[t[0] for t in tables]}")
        
        await db.close()
    except Exception as e:
        print(f"❌ Connection failed: {e}")


if __name__ == '__main__':
    asyncio.run(test_connection())