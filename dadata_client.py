"""Модуль для работы с DaData API"""
from datetime import datetime
import logging
import traceback
from typing import Optional, Dict, Any
from functools import wraps
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
import os
import sys
import httpx
import time

from config import AppConfig

config = AppConfig()

# Настройка логгера для DaData
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
    print(f"Created log directory: {log_dir}")

# Создаем отдельный логгер для DaData
dadata_logger = logging.getLogger('dadata')
dadata_logger.setLevel(logging.DEBUG)

# Очищаем существующие хендлеры
dadata_logger.handlers.clear()

# Форматтер для логов
log_format = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Основной файл логов с ротацией по размеру (10 MB)
dadata_log_file = os.path.join(log_dir, "dadata.log")
file_handler = RotatingFileHandler(
    dadata_log_file,
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,  # Хранить 5 бэкапов
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_format)
dadata_logger.addHandler(file_handler)

# Файл для ошибок с ротацией по размеру
dadata_error_log_file = os.path.join(log_dir, "dadata_error.log")
error_handler = RotatingFileHandler(
    dadata_error_log_file,
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=10,  # Хранить 10 бэкапов ошибок
    encoding='utf-8'
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(log_format)
dadata_logger.addHandler(error_handler)

# Дополнительный хендлер с ротацией по дням (для долгосрочного хранения)
daily_log_file = os.path.join(log_dir, "dadata_daily.log")
daily_handler = TimedRotatingFileHandler(
    daily_log_file,
    when='midnight',  # Ротация каждый день в полночь
    interval=1,
    backupCount=30,  # Хранить логи за 30 дней
    encoding='utf-8'
)
daily_handler.setLevel(logging.INFO)
daily_handler.setFormatter(log_format)
dadata_logger.addHandler(daily_handler)

# Консольный хендлер (только для важных сообщений)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_format)
dadata_logger.addHandler(console_handler)

# Запрещаем propagation чтобы не дублировать логи в корневой логгер
dadata_logger.propagate = False

logger = dadata_logger

# Логируем информацию о настройке логирования
logger.info("=" * 60)
logger.info("DaData logging configuration:")
logger.info(f"  Log directory: {log_dir}")
logger.info(f"  Main log file: {dadata_log_file}")
logger.info(f"  Error log file: {dadata_error_log_file}")
logger.info(f"  Daily log file: {daily_log_file}")
logger.info(f"  Max log size: 10 MB")
logger.info(f"  Backup count (size): 5")
logger.info(f"  Backup count (daily): 30 days")
logger.info(f"  Log levels - File: DEBUG, Console: INFO")
logger.info(f"  API Key configured: {bool(config.dadata.api_key)}")
logger.info(f"  Secret Key configured: {bool(config.dadata.secret_key)}")
logger.info("=" * 60)


def dadata_error_handler(func):
    """Декоратор для обработки ошибок DaData API с детальным логированием"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        func_name = func.__name__
        logger.debug(f"→ DaData API call: {func_name}")
        
        # Логируем аргументы (без чувствительных данных)
        if args:
            logger.debug(f"  Args: {args}")
        if kwargs:
            # Фильтруем чувствительные данные
            safe_kwargs = {k: (str(v)[:4] + '****' if hasattr(v, '__len__') and len(str(v)) > 4 and 'inn' in k.lower() else v) 
                          for k, v in kwargs.items()}
            logger.debug(f"  Kwargs: {safe_kwargs}")
        
        try:
            start_time = time.time()
            
            result = await func(*args, **kwargs)
            
            elapsed_time = time.time() - start_time
            logger.debug(f"← DaData API call {func_name} completed in {elapsed_time:.3f}s")
            
            # Логируем результат (частично)
            if result:
                if isinstance(result, dict):
                    safe_result = {
                        'inn': str(result.get('inn', ''))[:4] + '****' if result.get('inn') else None,
                        'type': result.get('type'),
                        'is_active': result.get('is_active'),
                        'name': result.get('name', {}).get('short', 'Unknown') if isinstance(result.get('name'), dict) else 'Unknown'
                    }
                    logger.debug(f"  Result summary: {safe_result}")
                else:
                    logger.debug(f"  Result type: {type(result)}")
            
            return result
            
        except httpx.TimeoutException as e:
            logger.error(f"✗ DaData timeout in {func_name}: {e}")
            logger.error(f"  Timeout details: Request exceeded time limit")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"✗ DaData HTTP error in {func_name}: {e.response.status_code}")
            logger.error(f"  Response body: {e.response.text[:500]}")  # Первые 500 символов
            logger.error(f"  Request URL: {e.request.url}")
            raise
        except Exception as e:
            logger.error(f"✗ DaData unexpected error in {func_name}: {e}")
            logger.error(f"  Traceback:\n{traceback.format_exc()}")
            raise
    
    return wrapper


class DadataClient:
    """Клиент для работы с DaData API"""
    
    def __init__(self):
        self.api_key = config.dadata.api_key
        self.secret_key = config.dadata.secret_key
        self.base_url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
        self.client = None
        self.request_count = 0
        self.error_count = 0
        self.success_count = 0
        self.total_response_time = 0
        
        if not self.api_key:
            logger.warning("⚠️ DaData API key is not configured!")
        else:
            logger.info("DaData client initialized successfully")
    
    async def __aenter__(self):
        """Вход в контекстный менеджер"""
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {self.api_key}"
            },
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
        )
        logger.debug("HTTP client created with connection pool")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Выход из контекстного менеджера"""
        if self.client:
            await self.client.aclose()
            logger.debug("HTTP client closed")
        
        # Логируем статистику использования
        avg_response_time = self.total_response_time / max(self.success_count, 1)
        logger.info(f"DaData session statistics - "
                   f"Requests: {self.request_count}, "
                   f"Success: {self.success_count}, "
                   f"Errors: {self.error_count}, "
                   f"Avg response time: {avg_response_time:.2f}s")
    
    async def _make_request(self, endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
        """Выполнение запроса с отслеживанием времени"""
        start_time = time.time()
        
        try:
            response = await self.client.post(
                f"{self.base_url}/{endpoint}",
                json=data
            )
            
            elapsed_time = time.time() - start_time
            self.total_response_time += elapsed_time
            
            response.raise_for_status()
            self.success_count += 1
            
            logger.debug(f"Request to {endpoint} completed in {elapsed_time:.3f}s")
            return response.json()
            
        except Exception as e:
            self.error_count += 1
            elapsed_time = time.time() - start_time
            logger.error(f"Request to {endpoint} failed after {elapsed_time:.3f}s")
            raise
    
    @dadata_error_handler
    async def find_company_by_inn(self, inn: str) -> Optional[Dict[str, Any]]:
        """Поиск компании по ИНН"""
        logger.info(f"Searching company by INN: {inn[:4]}****")
        self.request_count += 1
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        if not inn or not inn.strip():
            logger.warning("Empty INN provided")
            return None
        
        try:
            data = await self._make_request("findById/party", {"query": inn})
            
            if data.get("suggestions") and len(data["suggestions"]) > 0:
                company = data["suggestions"][0]
                logger.info(f"✅ Company found: {company.get('value', 'Unknown')}")
                return self._parse_company_data(company)
            else:
                logger.warning(f"No company found for INN: {inn[:4]}****")
                return None
                
        except Exception as e:
            logger.error(f"Failed to find company by INN {inn[:4]}****: {e}")
            raise
    
    @dadata_error_handler
    async def find_company_by_inn_kpp(self, inn: str, kpp: str) -> Optional[Dict[str, Any]]:
        """Поиск компании по ИНН и КПП"""
        logger.info(f"Searching company by INN+KPP: {inn[:4]}**** / {kpp}")
        self.request_count += 1
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        try:
            data = await self._make_request("findById/party", {"query": inn, "kpp": kpp})
            
            if data.get("suggestions") and len(data["suggestions"]) > 0:
                company = data["suggestions"][0]
                logger.info(f"✅ Company found: {company.get('value', 'Unknown')}")
                return self._parse_company_data(company)
            else:
                logger.warning(f"No company found for INN+KPP: {inn[:4]}****/{kpp}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to find company by INN+KPP: {e}")
            raise
    
    def _parse_company_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Парсинг данных компании из ответа DaData"""
        data = raw_data.get("data", {})
        
        parsed = {
            "value": raw_data.get("value", ""),
            "inn": data.get("inn", ""),
            "kpp": data.get("kpp", ""),
            "ogrn": data.get("ogrn", ""),
            "ogrn_date": data.get("ogrn_date"),
            "type": data.get("type", ""),  # LEGAL или INDIVIDUAL
            "name": {
                "full": data.get("name", {}).get("full_with_opf", ""),
                "short": data.get("name", {}).get("short_with_opf", ""),
            },
            "opf": {
                "code": data.get("opf", {}).get("code", ""),
                "full": data.get("opf", {}).get("full", ""),
                "short": data.get("opf", {}).get("short", ""),
            },
            "address": {
                "value": data.get("address", {}).get("value", ""),
                "unrestricted_value": data.get("address", {}).get("unrestricted_value", ""),
            },
            "state": {
                "status": data.get("state", {}).get("status", ""),  # ACTIVE, LIQUIDATED и т.д.
                "registration_date": data.get("state", {}).get("registration_date"),
                "liquidation_date": data.get("state", {}).get("liquidation_date"),
            },
            "management": {
                "name": data.get("management", {}).get("name", ""),
                "post": data.get("management", {}).get("post", ""),
            },
            "okved": data.get("okved", ""),
            "okved_type": data.get("okved_type", ""),
            "branch_type": data.get("branch_type", ""),  # MAIN или BRANCH
            "employee_count": data.get("employee_count"),
            "finance": data.get("finance", {}),
            "is_active": data.get("state", {}).get("status") == "ACTIVE"
        }
        
        # Для ИП добавляем ФИО
        if data.get("type") == "INDIVIDUAL" and data.get("fio"):
            parsed["fio"] = {
                "surname": data["fio"].get("surname", ""),
                "name": data["fio"].get("name", ""),
                "patronymic": data["fio"].get("patronymic", ""),
            }
        
        logger.debug(f"Parsed company data: {parsed['type']} - {parsed['name']['short']} (Active: {parsed['is_active']})")
        return parsed
    
    async def get_balance(self) -> Optional[Dict[str, Any]]:
        """Получение статистики использования DaData"""
        logger.debug("Getting DaData statistics")
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        try:
            response = await self.client.get(
                "https://dadata.ru/api/v2/stat/daily",
                headers={"Authorization": f"Token {self.api_key}"}
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"DaData daily stats retrieved")
            return data
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики использования клиента"""
        avg_response_time = self.total_response_time / max(self.success_count, 1)
        error_rate = (self.error_count / max(self.request_count, 1)) * 100
        
        return {
            "request_count": self.request_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "error_rate": f"{error_rate:.1f}%",
            "avg_response_time": f"{avg_response_time:.3f}s",
            "api_key_configured": bool(self.api_key),
            "secret_key_configured": bool(self.secret_key)
        }
    
    @staticmethod
    def cleanup_old_logs(days: int = 30):
        """Очистка старых лог-файлов"""
        try:
            cutoff_time = time.time() - (days * 24 * 60 * 60)
            
            for filename in os.listdir(log_dir):
                if filename.startswith('dadata') and filename.endswith('.log'):
                    filepath = os.path.join(log_dir, filename)
                    if os.path.getmtime(filepath) < cutoff_time:
                        os.remove(filepath)
                        logger.info(f"Removed old log file: {filename}")
                        
        except Exception as e:
            logger.error(f"Error cleaning up old logs: {e}")
    
    @staticmethod
    def get_log_files_info() -> Dict[str, Any]:
        """Получение информации о лог-файлах"""
        try:
            log_files = []
            total_size = 0
            
            for filename in os.listdir(log_dir):
                if filename.startswith('dadata') and filename.endswith('.log'):
                    filepath = os.path.join(log_dir, filename)
                    size = os.path.getsize(filepath)
                    total_size += size
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    
                    log_files.append({
                        'name': filename,
                        'size_mb': round(size / (1024 * 1024), 2),
                        'modified': mtime.strftime('%Y-%m-%d %H:%M:%S')
                    })
            
            return {
                'log_directory': log_dir,
                'files': sorted(log_files, key=lambda x: x['name']),
                'total_files': len(log_files),
                'total_size_mb': round(total_size / (1024 * 1024), 2)
            }
            
        except Exception as e:
            logger.error(f"Error getting log files info: {e}")
            return {'error': str(e)}

    @dadata_error_handler
    async def check_npd_status(self, inn: str) -> Optional[Dict[str, Any]]:
        """Проверка статуса самозанятого через API ФНС (НПД)"""
        logger.info(f"Checking NPD status for INN: {inn[:4]}****")
        self.request_count += 1
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        from datetime import date
        
        try:
            request_data = {
                "inn": inn,
                "requestDate": date.today().isoformat()
            }
            
            response = await self.client.post(
                "https://statusnpd.nalog.ru/api/v1/tracker/taxpayer_status",
                json=request_data,
                headers={
                    "Content-Type": "application/json",
                    # Убираем Authorization для этого запроса
                }
            )
            
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") is True:
                logger.info(f"✅ INN {inn[:4]}**** is NPD taxpayer")
                return {
                    "is_npd": True,
                    "message": data.get("message", ""),
                    "type": "NPD"
                }
            else:
                logger.info(f"❌ INN {inn[:4]}**** is NOT NPD taxpayer")
                return {
                    "is_npd": False,
                    "message": data.get("message", ""),
                    "type": None
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"NPD API HTTP error for INN {inn[:4]}****: {e.response.status_code}")
            logger.error(f"  Response body: {e.response.text[:500]}")
            return None
        except Exception as e:
            logger.error(f"NPD API error for INN {inn[:4]}****: {e}")
            return None
    
    @staticmethod
    def determine_registration_type(da_data_type: str, inn_length: int, is_npd: bool = False) -> str:
        """
        Определение типа регистрации для Bitrix24 на основе данных DaData
        
        Args:
            da_data_type: Тип из DaData (LEGAL, INDIVIDUAL)
            inn_length: Длина ИНН (10 для юрлиц, 12 для физлиц/ИП)
            is_npd: Является ли самозанятым (НПД)
        
        Returns:
            Тип для Bitrix24: "LEGAL", "INDIVIDUAL", "NPD", "FIZ"
        """
        if da_data_type == "LEGAL":
            return "LEGAL"
        elif da_data_type == "INDIVIDUAL":
            return "INDIVIDUAL"
        elif is_npd:
            return "NPD"
        elif inn_length == 12:
            return "FIZ"  # Физическое лицо с ИНН
        else:
            return "FIZ"  # По умолчанию физлицо


# Для тестирования
async def test_dadata():
    """Тест подключения к DaData API"""
    print("\n" + "=" * 50)
    print("🧪 Testing DaData API Connection")
    print("=" * 50 + "\n")
    
    # Тестовые ИНН
    test_inns = [
        "7707083893",  # Сбербанк (юрлицо)
        "500100732259",  # ИП (пример)
        "123456789012",  # Физлицо (пример)
    ]
    
    async with DadataClient() as client:
        for inn in test_inns:
            print(f"Testing INN: {inn}")
            try:
                company = await client.find_company_by_inn(inn)
                if company:
                    print(f"  ✅ Found: {company['name']['short']}")
                    print(f"     DaData Type: {company['type']}")
                    
                    # Определяем тип для Bitrix24
                    reg_type = client.determine_registration_type(
                        da_data_type=company['type'],
                        inn_length=len(inn),
                        is_npd=False  # Для теста
                    )
                    print(f"     Bitrix24 Type: {reg_type}")
                    print(f"     Status: {company['state']['status']}")
                    print(f"     Active: {company['is_active']}")
                    
                    if company.get('address', {}).get('value'):
                        addr = company['address']['value']
                        print(f"     Address: {addr[:50]}..." if len(addr) > 50 else f"     Address: {addr}")
                else:
                    print(f"  ❌ Not found")
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print()
        
        print("\n📊 Statistics:")
        stats = client.get_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")
        
        print("\n📁 Log files info:")
        log_info = client.get_log_files_info()
        if 'error' not in log_info:
            print(f"  Directory: {log_info['log_directory']}")
            print(f"  Total files: {log_info['total_files']}")
            print(f"  Total size: {log_info['total_size_mb']} MB")
            for log_file in log_info['files']:
                print(f"    - {log_file['name']}: {log_file['size_mb']} MB (modified: {log_file['modified']})")


if __name__ == '__main__':
    import asyncio
    asyncio.run(test_dadata())