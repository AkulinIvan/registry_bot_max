"""Миграция для добавления поля bitrix_id"""
import asyncio
import aiomysql
from config import AppConfig

config = AppConfig()

async def migrate():
    """Добавление поля bitrix_id в таблицу registrations"""
    print("🔄 Starting migration: adding bitrix_id field...")
    
    pool = await aiomysql.create_pool(
        host=config.db.host,
        port=config.db.port,
        db=config.db.name,
        user=config.db.user,
        password=config.db.password,
        autocommit=True,
        charset='utf8mb4'
    )
    
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'registrations' 
                AND COLUMN_NAME = 'bitrix_id'
            """, (config.db.name,))
            
            has_bitrix_id = (await cur.fetchone())[0]
            
            if not has_bitrix_id:
                print("📝 Adding bitrix_id field to registrations table...")
                await cur.execute("""
                    ALTER TABLE registrations 
                    ADD COLUMN bitrix_id VARCHAR(50) COMMENT 'Bitrix24 registration ID' AFTER inn
                """)
                print("✅ bitrix_id field added to registrations table")
            else:
                print("ℹ️ bitrix_id field already exists in registrations table")
    
    pool.close()
    await pool.wait_closed()
    print("✅ Migration completed!")

if __name__ == '__main__':
    asyncio.run(migrate())