"""Модуль для работы с DaData API"""
import logging
import traceback
from typing import Optional, Dict, Any
from functools import wraps
from logging.handlers import RotatingFileHandler
import os
import httpx

from config import AppConfig

config = AppConfig()

# Настройка логгера для DaData
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

dadata_logger = logging.getLogger('dadata')
dadata_logger.setLevel(logging.DEBUG)
dadata_logger.handlers.clear()

log_format = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

dadata_log_file = os.path.join(log_dir, "dadata.log")
file_handler = RotatingFileHandler(
    dadata_log_file,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_format)
dadata_logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_format)
dadata_logger.addHandler(console_handler)

dadata_logger.propagate = False

logger = dadata_logger


def dadata_error_handler(func):
    """Декоратор для обработки ошибок DaData API"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        func_name = func.__name__
        logger.debug(f"→ DaData API call: {func_name}")
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"← DaData API call {func_name} completed")
            return result
        except httpx.TimeoutException as e:
            logger.error(f"✗ DaData timeout in {func_name}: {e}")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"✗ DaData HTTP error in {func_name}: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"✗ DaData unexpected error in {func_name}: {e}\n{traceback.format_exc()}")
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
        logger.info("DaData client initialized")
    
    async def __aenter__(self):
        """Вход в контекстный менеджер"""
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {self.api_key}"
            }
        )
        logger.debug("HTTP client created")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Выход из контекстного менеджера"""
        if self.client:
            await self.client.aclose()
            logger.debug("HTTP client closed")
    
    @dadata_error_handler
    async def find_company_by_inn(self, inn: str) -> Optional[Dict[str, Any]]:
        """Поиск компании по ИНН"""
        logger.info(f"Searching company by INN: {inn[:4]}****")
        self.request_count += 1
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        try:
            response = await self.client.post(
                f"{self.base_url}/findById/party",
                json={"query": inn}
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("suggestions") and len(data["suggestions"]) > 0:
                company = data["suggestions"][0]
                logger.info(f"✅ Company found: {company.get('value', 'Unknown')}")
                return self._parse_company_data(company)
            else:
                logger.warning(f"No company found for INN: {inn[:4]}****")
                return None
                
        except Exception as e:
            self.error_count += 1
            raise
    
    @dadata_error_handler
    async def find_company_by_inn_kpp(self, inn: str, kpp: str) -> Optional[Dict[str, Any]]:
        """Поиск компании по ИНН и КПП"""
        logger.info(f"Searching company by INN+KPP: {inn[:4]}**** / {kpp}")
        self.request_count += 1
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        try:
            response = await self.client.post(
                f"{self.base_url}/findById/party",
                json={"query": inn, "kpp": kpp}
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("suggestions") and len(data["suggestions"]) > 0:
                company = data["suggestions"][0]
                logger.info(f"✅ Company found: {company.get('value', 'Unknown')}")
                return self._parse_company_data(company)
            else:
                logger.warning(f"No company found for INN+KPP: {inn[:4]}****/{kpp}")
                return None
                
        except Exception as e:
            self.error_count += 1
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
        
        logger.debug(f"Parsed company data: {parsed['type']} - {parsed['name']['short']}")
        return parsed
    
    async def get_balance(self) -> Optional[float]:
        """Получение баланса DaData"""
        logger.debug("Getting DaData balance")
        
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        try:
            response = await self.client.get(
                "https://dadata.ru/api/v2/stat/daily",
                headers={"Authorization": f"Token {self.api_key}"}
            )
            response.raise_for_status()
            # Баланс можно получить из заголовков ответа
            return None  # DaData не предоставляет прямого метода для баланса
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики использования"""
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "api_key_configured": bool(self.api_key),
            "secret_key_configured": bool(self.secret_key)
        }


# Для тестирования
async def test_dadata():
    """Тест подключения к DaData API"""
    print("\n" + "=" * 50)
    print("🧪 Testing DaData API Connection")
    print("=" * 50 + "\n")
    
    # Тестовые ИНН
    test_inns = [
        "7707083893",  # Сбербанк
        "7706107510",  # РЖД
    ]
    
    async with DadataClient() as client:
        for inn in test_inns:
            print(f"Testing INN: {inn}")
            try:
                company = await client.find_company_by_inn(inn)
                if company:
                    print(f"  ✅ Found: {company['name']['short']}")
                    print(f"     Type: {company['type']}")
                    print(f"     Status: {company['state']['status']}")
                    print(f"     Address: {company['address']['value'][:50]}...")
                else:
                    print(f"  ❌ Not found")
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print()
        
        stats = client.get_stats()
        print(f"Stats: {stats}")


if __name__ == '__main__':
    import asyncio
    asyncio.run(test_dadata())