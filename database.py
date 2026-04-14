"""Модуль для работы с базой данных MySQL"""
import asyncio
import aiomysql
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging
import traceback
from functools import wraps

from config import AppConfig

config = AppConfig()
logger = logging.getLogger(__name__)


def db_error_handler(func):
    """Декоратор для обработки ошибок базы данных"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        func_name = func.__name__
        logger.debug(f"→ DB operation: {func_name}")
        
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"← DB operation {func_name} completed successfully")
            return result
        except aiomysql.Error as e:
            logger.error(f"✗ MySQL Error in {func_name}: {e}\n{traceback.format_exc()}")
            raise
        except asyncio.TimeoutError as e:
            logger.error(f"✗ Timeout in {func_name}: {e}")
            raise
        except Exception as e:
            logger.error(f"✗ Unexpected error in {func_name}: {e}\n{traceback.format_exc()}")
            raise
    
    return wrapper


class Database:
    """Асинхронный класс для работы с MySQL"""
    
    def __init__(self):
        self.pool = None
        self.connection_attempts = 0
        self.max_connection_attempts = 3
        logger.debug("Database instance created")
    
    async def connect(self):
        """Создание пула соединений"""
        logger.info("=" * 50)
        logger.info("Initializing database connection...")
        logger.info(f"Host: {config.db.host}:{config.db.port}")
        logger.info(f"Database: {config.db.name}")
        logger.info(f"User: {config.db.user}")
        logger.info("=" * 50)
        
        for attempt in range(1, self.max_connection_attempts + 1):
            try:
                logger.debug(f"Connection attempt {attempt}/{self.max_connection_attempts}")
                
                self.pool = await aiomysql.create_pool(
                    host=config.db.host,
                    port=config.db.port,
                    db=config.db.name,
                    user=config.db.user,
                    password=config.db.password,
                    minsize=config.db.pool_min_size if hasattr(config.db, 'pool_min_size') else 2,
                    maxsize=config.db.pool_max_size if hasattr(config.db, 'pool_max_size') else 10,
                    autocommit=True,
                    charset='utf8mb4',
                    connect_timeout=10,
                    pool_recycle=3600
                )
                
                logger.info(f"✅ MySQL connection pool created (minsize={self.pool.minsize}, maxsize={self.pool.maxsize})")
                
                # Проверяем соединение
                await self._test_connection()
                
                # Инициализируем схему
                await self.init_schema()
                
                self.connection_attempts = 0
                return
                
            except aiomysql.OperationalError as e:
                logger.error(f"❌ MySQL operational error (attempt {attempt}/{self.max_connection_attempts}): {e}")
                if attempt < self.max_connection_attempts:
                    wait_time = attempt * 2
                    logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.critical(f"Failed to connect to MySQL after {self.max_connection_attempts} attempts")
                    raise
                    
            except aiomysql.Error as e:
                logger.error(f"❌ MySQL connection failed: {e}\n{traceback.format_exc()}")
                raise
                
            except Exception as e:
                logger.error(f"❌ Unexpected error during connection: {e}\n{traceback.format_exc()}")
                raise
    
    async def _test_connection(self):
        """Тестирование соединения с базой данных"""
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT VERSION()")
                    version = await cur.fetchone()
                    logger.info(f"✅ MySQL Server version: {version[0]}")
                    
                    await cur.execute("SELECT DATABASE()")
                    db_name = await cur.fetchone()
                    logger.info(f"✅ Connected to database: {db_name[0]}")
                    
                    await cur.execute("SHOW STATUS LIKE 'Uptime'")
                    uptime = await cur.fetchone()
                    logger.info(f"✅ Server uptime: {uptime[1]} seconds")
                    
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}")
            raise
    
    async def close(self):
        """Закрытие пула соединений"""
        logger.info("Closing database connection pool...")
        
        if self.pool:
            try:
                self.pool.close()
                await self.pool.wait_closed()
                logger.info("✅ MySQL connection pool closed successfully")
            except Exception as e:
                logger.error(f"❌ Error closing connection pool: {e}")
                raise
        else:
            logger.debug("No active connection pool to close")
    
    @db_error_handler
    async def init_schema(self):
        """Инициализация схемы базы данных"""
        logger.info("Initializing database schema...")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Проверяем существование базы данных
                logger.debug("Checking database existence...")
                
                # Таблица пользователей
                logger.debug("Creating/verifying 'users' table...")
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT UNIQUE NOT NULL COMMENT 'Telegram user ID',
                        chat_id BIGINT COMMENT 'Telegram chat ID',
                        name VARCHAR(255) COMMENT 'User full name',
                        phone VARCHAR(20) COMMENT 'Phone number',
                        inn VARCHAR(12) COMMENT 'INN number',
                        state VARCHAR(50) DEFAULT 'awaiting_phone' COMMENT 'Current state',
                        registration_status VARCHAR(50) DEFAULT 'pending' COMMENT 'Registration status',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Record creation time',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last update time',
                        registered_at TIMESTAMP NULL COMMENT 'Registration completion time',
                        INDEX idx_user_id (user_id),
                        INDEX idx_state (state),
                        INDEX idx_registration_status (registration_status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='Bot users table'
                """)
                logger.info("✅ Table 'users' created/verified")
                
                # Таблица регистраций
                logger.debug("Creating/verifying 'registrations' table...")
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS registrations (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT NOT NULL COMMENT 'Telegram user ID',
                        chat_id BIGINT COMMENT 'Telegram chat ID',
                        name VARCHAR(255) NOT NULL COMMENT 'User name',
                        phone VARCHAR(20) NOT NULL COMMENT 'Phone number',
                        inn VARCHAR(12) NOT NULL COMMENT 'INN number',
                        event_name VARCHAR(255) NOT NULL COMMENT 'Event name',
                        registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Registration date',
                        status VARCHAR(50) DEFAULT 'active' COMMENT 'Registration status',
                        INDEX idx_user_id (user_id),
                        INDEX idx_inn (inn),
                        INDEX idx_registration_date (registration_date),
                        INDEX idx_status (status),
                        INDEX idx_event_name (event_name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    COMMENT='Event registrations table'
                """)
                logger.info("✅ Table 'registrations' created/verified")
                
                # Проверяем структуру таблиц
                await self._verify_tables_structure(cur)
                
                logger.info("✅ Database schema initialized successfully")
    
    async def _verify_tables_structure(self, cur):
        """Проверка структуры таблиц"""
        logger.debug("Verifying tables structure...")
        
        # Проверяем users
        await cur.execute("DESCRIBE users")
        users_columns = await cur.fetchall()
        logger.debug(f"Users table has {len(users_columns)} columns")
        
        # Проверяем registrations
        await cur.execute("DESCRIBE registrations")
        reg_columns = await cur.fetchall()
        logger.debug(f"Registrations table has {len(reg_columns)} columns")
        
        # Проверяем индексы
        await cur.execute("SHOW INDEX FROM users")
        users_indexes = await cur.fetchall()
        logger.debug(f"Users table has {len(users_indexes)} indexes")
        
        await cur.execute("SHOW INDEX FROM registrations")
        reg_indexes = await cur.fetchall()
        logger.debug(f"Registrations table has {len(reg_indexes)} indexes")
    
    @db_error_handler
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение пользователя по ID"""
        logger.debug(f"Getting user {user_id} from database")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM users WHERE user_id = %s",
                    (user_id,)
                )
                user = await cur.fetchone()
                
                if user:
                    logger.debug(f"User {user_id} found in database (status: {user.get('registration_status')})")
                    # Маскируем чувствительные данные в логах
                    if user.get('phone'):
                        user['phone'] = f"{user['phone'][:4]}****" if len(user['phone']) > 4 else "****"
                    if user.get('inn'):
                        user['inn'] = f"{user['inn'][:4]}****" if len(user['inn']) > 4 else "****"
                else:
                    logger.debug(f"User {user_id} not found in database")
                
                return user
    
    @db_error_handler
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
        logger.debug(f"Saving user {user_id} to database")
        logger.debug(f"  chat_id: {chat_id}")
        logger.debug(f"  name: {name}")
        logger.debug(f"  phone: {phone[:4] if phone else None}****" if phone else "  phone: None")
        logger.debug(f"  inn: {inn[:4] if inn else None}****" if inn else "  inn: None")
        logger.debug(f"  state: {state}")
        logger.debug(f"  status: {status}")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Проверяем существование пользователя
                await cur.execute(
                    "SELECT id FROM users WHERE user_id = %s",
                    (user_id,)
                )
                existing = await cur.fetchone()
                
                if existing:
                    logger.debug(f"User {user_id} already exists (id: {existing[0]}), updating...")
                    
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
                        update_query = f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s"
                        logger.debug(f"Update query: {update_query}")
                        await cur.execute(update_query, params)
                        logger.info(f"✅ User {user_id} updated successfully")
                    else:
                        logger.debug(f"No fields to update for user {user_id}")
                    
                    return existing[0]
                else:
                    logger.debug(f"User {user_id} does not exist, creating new record...")
                    
                    # Создаем нового
                    await cur.execute("""
                        INSERT INTO users (user_id, chat_id, name, phone, inn, state, registration_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, chat_id, name, phone, inn, state, status or 'pending'))
                    
                    new_id = cur.lastrowid
                    logger.info(f"✅ New user created: user_id={user_id}, id={new_id}")
                    
                    return new_id
    
    @db_error_handler
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
        logger.info(f"Saving registration for user {user_id}")
        logger.debug(f"  chat_id: {chat_id}")
        logger.debug(f"  name: {name}")
        logger.debug(f"  phone: {phone[:4]}****" if phone else "  phone: None")
        logger.debug(f"  inn: {inn[:4]}****" if inn else "  inn: None")
        logger.debug(f"  event_name: {event_name}")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Проверяем, не зарегистрирован ли уже пользователь
                await cur.execute(
                    "SELECT id FROM registrations WHERE user_id = %s AND event_name = %s",
                    (user_id, event_name)
                )
                existing = await cur.fetchone()
                
                if existing:
                    logger.warning(f"User {user_id} already registered for event '{event_name}' (reg_id: {existing[0]})")
                    return existing[0]
                
                # Сохраняем регистрацию
                await cur.execute("""
                    INSERT INTO registrations (user_id, chat_id, name, phone, inn, event_name)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, chat_id, name, phone, inn, event_name))
                
                reg_id = cur.lastrowid
                logger.info(f"✅ Registration record created: id={reg_id}")
                
                # Обновляем статус пользователя
                await cur.execute("""
                    UPDATE users 
                    SET registration_status = 'completed',
                        registered_at = NOW(),
                        state = 'registered',
                        inn = %s
                    WHERE user_id = %s
                """, (inn, user_id))
                
                logger.info(f"✅ User {user_id} status updated to 'completed'")
                
                # Проверяем, что обновление прошло успешно
                await cur.execute(
                    "SELECT registration_status, registered_at FROM users WHERE user_id = %s",
                    (user_id,)
                )
                updated = await cur.fetchone()
                logger.debug(f"User {user_id} status after update: {updated[0]}, registered_at: {updated[1]}")
                
                return reg_id
    
    @db_error_handler
    async def check_inn_exists(self, inn: str) -> bool:
        """Проверка существования ИНН"""
        logger.debug(f"Checking if INN exists: {inn[:4]}****")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM registrations WHERE inn = %s LIMIT 1",
                    (inn,)
                )
                result = await cur.fetchone()
                exists = result is not None
                
                if exists:
                    logger.info(f"INN {inn[:4]}**** already exists in database")
                else:
                    logger.debug(f"INN {inn[:4]}**** not found in database")
                
                return exists
    
    @db_error_handler
    async def get_stats(self) -> Dict[str, Any]:
        """Получение статистики"""
        logger.info("Collecting database statistics...")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Общее количество регистраций
                await cur.execute("SELECT COUNT(*) FROM registrations")
                total = (await cur.fetchone())[0]
                logger.debug(f"Total registrations: {total}")
                
                # Завершенные регистрации
                await cur.execute(
                    "SELECT COUNT(*) FROM users WHERE registration_status = 'completed'"
                )
                completed = (await cur.fetchone())[0]
                logger.debug(f"Completed registrations: {completed}")
                
                # Ожидающие регистрации
                await cur.execute(
                    "SELECT COUNT(*) FROM users WHERE registration_status = 'pending'"
                )
                pending = (await cur.fetchone())[0]
                logger.debug(f"Pending registrations: {pending}")
                
                # Регистрации по дням (за последние 7 дней)
                await cur.execute("""
                    SELECT DATE(registration_date) as date, COUNT(*) as count
                    FROM registrations
                    WHERE registration_date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                    GROUP BY DATE(registration_date)
                    ORDER BY date DESC
                """)
                daily_stats = await cur.fetchall()
                logger.debug(f"Daily stats for last 7 days: {len(daily_stats)} days")
                
                # Статистика по состояниям
                await cur.execute("""
                    SELECT state, COUNT(*) as count
                    FROM users
                    WHERE state IS NOT NULL
                    GROUP BY state
                """)
                state_stats = await cur.fetchall()
                
                stats = {
                    "total_registrations": total,
                    "completed": completed,
                    "pending": pending,
                    "completion_rate": f"{(completed/total*100 if total > 0 else 0):.2f}%",
                    "daily_stats": [
                        {"date": str(d[0]), "count": d[1]} for d in daily_stats
                    ],
                    "state_distribution": [
                        {"state": s[0], "count": s[1]} for s in state_stats
                    ],
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                logger.info(f"Statistics collected: total={total}, completed={completed}, pending={pending}")
                return stats
    
    @db_error_handler
    async def get_users_by_state(self, state: str) -> List[Dict[str, Any]]:
        """Получение пользователей по состоянию"""
        logger.debug(f"Getting users with state '{state}'")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT user_id, name, state, created_at FROM users WHERE state = %s",
                    (state,)
                )
                users = await cur.fetchall()
                logger.info(f"Found {len(users)} users with state '{state}'")
                return users
    
    @db_error_handler
    async def cleanup_old_sessions(self, hours: int = 24) -> int:
        """Очистка старых незавершенных сессий"""
        logger.info(f"Cleaning up pending sessions older than {hours} hours")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    DELETE FROM users 
                    WHERE registration_status = 'pending' 
                    AND created_at < DATE_SUB(NOW(), INTERVAL %s HOUR)
                """, (hours,))
                
                deleted = cur.rowcount
                logger.info(f"✅ Cleaned up {deleted} old pending sessions")
                return deleted
    
    @db_error_handler
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья базы данных"""
        logger.debug("Performing database health check...")
        
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Проверяем соединение
                await cur.execute("SELECT 1")
                await cur.fetchone()
                
                # Проверяем размер базы данных
                await cur.execute(f"""
                    SELECT 
                        table_name,
                        ROUND(((data_length + index_length) / 1024 / 1024), 2) AS size_mb
                    FROM information_schema.TABLES
                    WHERE table_schema = '{config.db.name}'
                """)
                tables_size = await cur.fetchall()
                
                # Проверяем количество соединений
                await cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                threads = await cur.fetchone()
                
                health = {
                    "status": "healthy",
                    "pool_size": len(self.pool._pool) if hasattr(self.pool, '_pool') else "unknown",
                    "tables": [
                        {"name": t[0], "size_mb": t[1]} for t in tables_size
                    ],
                    "connections": int(threads[1]) if threads else 0,
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                logger.debug(f"Health check completed: {health['status']}")
                return health


# Для отладки
async def test_connection():
    """Тест подключения к БД"""
    print("\n" + "=" * 50)
    print("🧪 Testing MySQL Connection")
    print("=" * 50 + "\n")
    
    db = Database()
    try:
        await db.connect()
        print("✅ Connection successful!\n")
        
        # Проверяем таблицы
        print("📊 Checking tables...")
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                tables = await cur.fetchall()
                print(f"Tables found: {len(tables)}")
                for table in tables:
                    print(f"  - {table[0]}")
        
        print("\n📈 Getting statistics...")
        stats = await db.get_stats()
        print(f"Total registrations: {stats['total_registrations']}")
        print(f"Completed: {stats['completed']}")
        print(f"Pending: {stats['pending']}")
        
        print("\n🏥 Health check...")
        health = await db.health_check()
        print(f"Status: {health['status']}")
        print(f"Pool size: {health['pool_size']}")
        print(f"Active connections: {health['connections']}")
        
        await db.close()
        print("\n✅ Test completed successfully!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        print(traceback.format_exc())


if __name__ == '__main__':
    asyncio.run(test_connection())