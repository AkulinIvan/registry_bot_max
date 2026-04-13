"""Модуль для работы с базой данных"""
import asyncio
import asyncpg
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

from config import AppConfig

config = AppConfig()
logger = logging.getLogger(__name__)


class Database:
    """Асинхронный класс для работы с PostgreSQL"""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """Создание пула соединений"""
        try:
            self.pool = await asyncpg.create_pool(
                host=config.db.host,
                port=config.db.port,
                database=config.db.name,
                user=config.db.user,
                password=config.db.password,
                min_size=2,
                max_size=10
            )
            logger.info("✅ Database connection pool created")
            await self.init_schema()
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    async def close(self):
        """Закрытие пула соединений"""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection pool closed")
    
    async def init_schema(self):
        """Инициализация схемы базы данных"""
        async with self.pool.acquire() as conn:
            # Сначала удаляем старые таблицы (если нужно с чистого листа)
            # await conn.execute("DROP TABLE IF EXISTS registrations")
            # await conn.execute("DROP TABLE IF EXISTS users")
            
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL,
                    chat_id BIGINT,
                    name VARCHAR(255),
                    phone VARCHAR(20),
                    inn VARCHAR(12),
                    state VARCHAR(50) DEFAULT 'awaiting_phone',
                    registration_status VARCHAR(50) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    registered_at TIMESTAMP
                )
            """)
            logger.info("✅ Table 'users' created/verified")
            
            # Таблица регистраций
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS registrations (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    inn VARCHAR(12) NOT NULL,
                    event_name VARCHAR(255) NOT NULL,
                    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(50) DEFAULT 'active'
                )
            """)
            logger.info("✅ Table 'registrations' created/verified")
            
            # Проверяем существование колонок перед созданием индексов
            # Проверяем колонку user_id в таблице users
            columns = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'users'
            """)
            column_names = [col['column_name'] for col in columns]
            
            if 'user_id' in column_names:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)"
                )
                logger.info("✅ Index 'idx_users_user_id' created")
            
            # Индекс для ИНН в регистрациях
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registrations_inn ON registrations(inn)"
            )
            logger.info("✅ Index 'idx_registrations_inn' created")
            
            # Индекс для даты регистрации
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registrations_date ON registrations(registration_date)"
            )
            logger.info("✅ Index 'idx_registrations_date' created")
            
            logger.info("✅ Database schema initialized successfully")
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение пользователя по ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1",
                user_id
            )
            return dict(row) if row else None
    
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
            # Проверяем существование пользователя
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE user_id = $1",
                user_id
            )
            
            if existing:
                # Обновляем существующего
                result = await conn.fetchval("""
                    UPDATE users 
                    SET 
                        chat_id = COALESCE($2, users.chat_id),
                        name = COALESCE($3, users.name),
                        phone = COALESCE($4, users.phone),
                        inn = COALESCE($5, users.inn),
                        state = COALESCE($6, users.state),
                        registration_status = COALESCE($7, users.registration_status),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                    RETURNING id
                """, user_id, chat_id, name, phone, inn, state, status)
                return result
            else:
                # Создаем нового
                result = await conn.fetchval("""
                    INSERT INTO users (user_id, chat_id, name, phone, inn, state, registration_status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                """, user_id, chat_id, name, phone, inn, state, status or 'pending')
                return result
    
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
            # Сохраняем регистрацию
            reg_id = await conn.fetchval("""
                INSERT INTO registrations (user_id, chat_id, name, phone, inn, event_name)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, user_id, chat_id, name, phone, inn, event_name)
            
            # Обновляем статус пользователя
            await conn.execute("""
                UPDATE users 
                SET registration_status = 'completed',
                    registered_at = CURRENT_TIMESTAMP,
                    state = 'registered',
                    inn = $2
                WHERE user_id = $1
            """, user_id, inn)
            
            return reg_id
    
    async def check_inn_exists(self, inn: str) -> bool:
        """Проверка существования ИНН"""
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM registrations WHERE inn = $1)",
                inn
            )
            return exists
    
    async def get_stats(self) -> Dict[str, Any]:
        """Получение статистики"""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM registrations")
            completed = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE registration_status = 'completed'"
            )
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE registration_status = 'pending'"
            )
            
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
            tables = await conn.fetch("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
            """)
            print(f"Tables: {[t['table_name'] for t in tables]}")
        
        await db.close()
    except Exception as e:
        print(f"❌ Connection failed: {e}")


if __name__ == '__main__':
    asyncio.run(test_connection())