"""Модуль конфигурации приложения"""
import os
import json
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class DatabaseConfig:
    """Конфигурация базы данных"""
    host: str = field(default_factory=lambda: os.getenv('DB_HOST', 'localhost'))
    port: int = field(default_factory=lambda: int(os.getenv('DB_PORT', '3306')))
    name: str = field(default_factory=lambda: os.getenv('DB_NAME', 'max_bot'))
    user: str = field(default_factory=lambda: os.getenv('DB_USER', 'root'))
    password: str = field(default_factory=lambda: os.getenv('DB_PASSWORD', ''))


@dataclass(frozen=True)
class BotConfig:
    """Конфигурация бота"""
    token: str = field(default_factory=lambda: os.getenv('MAX_BOT_TOKEN', ''))
    event_name: str = field(default_factory=lambda: os.getenv('EVENT_NAME', 'Дни предпринимательства'))
    log_level: str = field(default_factory=lambda: os.getenv('LOG_LEVEL', 'INFO'))
    environment: str = field(default_factory=lambda: os.getenv('ENVIRONMENT', 'development'))
    admin_ids: List[int] = field(default_factory=lambda: BotConfig._parse_admin_ids())

    @staticmethod
    def _parse_admin_ids() -> List[int]:
        """Парсинг списка ID администраторов из переменной окружения"""
        admin_ids_str = os.getenv('ADMIN_IDS', '[]')
        try:
            # Пробуем распарсить как JSON
            admin_ids = json.loads(admin_ids_str)
            if isinstance(admin_ids, list):
                return [int(id) for id in admin_ids]
        except (json.JSONDecodeError, ValueError, TypeError):
            # Если не JSON, пробуем распарсить как строку с запятыми
            if admin_ids_str and admin_ids_str != '[]':
                try:
                    return [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()]
                except ValueError:
                    pass
        return []

    def is_admin(self, user_id: int) -> bool:
        """Проверка, является ли пользователь администратором"""
        return user_id in self.admin_ids


@dataclass(frozen=True)
class DadataConfig:
    """Конфигурация DaData API"""
    api_key: str = field(default_factory=lambda: os.getenv('DADATA_API_KEY', ''))
    secret_key: str = field(default_factory=lambda: os.getenv('DADATA_SECRET_KEY', ''))


@dataclass(frozen=True)
class LoggingConfig:
    """Конфигурация логирования"""
    log_dir: str = field(default_factory=lambda: os.getenv('LOG_DIR', 'logs'))
    max_log_size_mb: int = field(default_factory=lambda: int(os.getenv('MAX_LOG_SIZE_MB', '10')))
    backup_count: int = field(default_factory=lambda: int(os.getenv('LOG_BACKUP_COUNT', '5')))
    console_log_level: str = field(default_factory=lambda: os.getenv('CONSOLE_LOG_LEVEL', 'INFO'))
    file_log_level: str = field(default_factory=lambda: os.getenv('FILE_LOG_LEVEL', 'DEBUG'))

@dataclass(frozen=True)
class BitrixConfig:
    """Конфигурация Bitrix24 API"""
    register_url: str = field(default_factory=lambda: os.getenv('BITRIX_REGISTER_URL', 'https://bitrix.neto.ru/.bot_dp_register.php'))
    list_url: str = field(default_factory=lambda: os.getenv('BITRIX_LIST_URL', 'https://bitrix.neto.ru/.bot_dp_register_list.php'))
    timeout: float = field(default_factory=lambda: float(os.getenv('BITRIX_TIMEOUT', '10.0')))

@dataclass(frozen=True)
class AppConfig:
    """Основная конфигурация приложения"""
    bot: BotConfig = field(default_factory=BotConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dadata: DadataConfig = field(default_factory=DadataConfig)
    bitrix: BitrixConfig = field(default_factory=BitrixConfig)

    def __post_init__(self):
        """Валидация конфигурации после инициализации"""
        if not self.bot.token:
            raise ValueError("MAX_BOT_TOKEN is required! Please set it in .env file")

        if self.bot.environment not in ['development', 'production', 'testing']:
            raise ValueError(f"Invalid ENVIRONMENT: {self.bot.environment}")

        if self.bot.log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            raise ValueError(f"Invalid LOG_LEVEL: {self.bot.log_level}")

    def is_development(self) -> bool:
        """Проверка, запущено ли приложение в режиме разработки"""
        return self.bot.environment == 'development'

    def is_production(self) -> bool:
        """Проверка, запущено ли приложение в продакшн режиме"""
        return self.bot.environment == 'production'

    def get_log_file_path(self, name: str) -> str:
        """Получение полного пути к лог-файлу"""
        return os.path.join(self.logging.log_dir, f"{name}.log")

    def get_max_log_size_bytes(self) -> int:
        """Получение максимального размера лог-файла в байтах"""
        return self.logging.max_log_size_mb * 1024 * 1024
    
    
@dataclass(frozen=True)
class AdminConfig:
    """Конфигурация административного сервиса"""
    broadcast_delay: float = field(default_factory=lambda: float(os.getenv('BROADCAST_DELAY', '0.05')))
    broadcast_batch_size: int = field(default_factory=lambda: int(os.getenv('BROADCAST_BATCH_SIZE', '100')))
    broadcast_history_limit: int = field(default_factory=lambda: int(os.getenv('BROADCAST_HISTORY_LIMIT', '10')))
    cleanup_broadcast_after: int = field(default_factory=lambda: int(os.getenv('CLEANUP_BROADCAST_AFTER', '3600')))


@dataclass(frozen=True)
class AppConfig:
    """Основная конфигурация приложения"""
    bot: BotConfig = field(default_factory=BotConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dadata: DadataConfig = field(default_factory=DadataConfig)
    bitrix: BitrixConfig = field(default_factory=BitrixConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)  # ← добавить