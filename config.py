"""Модуль конфигурации приложения"""
import os
from dataclasses import dataclass, field
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
    admin_ids: list = field(default_factory=lambda: [
        int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()
    ])


@dataclass(frozen=True)
class DadataConfig:
    """Конфигурация DaData API"""
    api_key: str = field(default_factory=lambda: os.getenv('DADATA_API_KEY', ''))
    secret_key: str = field(default_factory=lambda: os.getenv('DADATA_SECRET_KEY', ''))


@dataclass(frozen=True)
class AppConfig:
    """Основная конфигурация приложения"""
    bot: BotConfig = field(default_factory=BotConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    dadata: DadataConfig = field(default_factory=DadataConfig)