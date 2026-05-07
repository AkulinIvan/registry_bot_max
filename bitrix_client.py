"""Клиент для отправки данных регистрации в Bitrix24"""
import httpx
import logging
import re
import json
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class BitrixClient:
    """Клиент для работы с API Bitrix24"""
    
    def __init__(self, register_url: str = None, list_url: str = None, timeout: int = 30):
        """
        Инициализация клиента
        
        Args:
            register_url: URL для регистрации пользователя
            list_url: URL для получения списка регистраций
            timeout: Таймаут запроса в секундах
        """
        self.register_url = register_url or "https://bitrix.neto.ru/bot_dp_register.php"
        self.list_url = list_url or "https://bitrix.neto.ru/bot_dp_register_list.php"
        self.timeout = timeout
        logger.info(f"BitrixClient initialized")
        logger.info(f"  Register URL: {self.register_url}")
        logger.info(f"  List URL: {self.list_url}")
    
    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Нормализация телефона в формат 7XXXXXXXXXX (11 цифр)
        """
        if not phone:
            return ""
        
        # Удаляем все нецифровые символы
        digits = ''.join(filter(str.isdigit, phone))
        
        if len(digits) == 11 and digits.startswith('8'):
            return '7' + digits[1:]
        elif len(digits) == 11 and digits.startswith('7'):
            return digits
        elif len(digits) == 10:
            return '7' + digits
        else:
            return digits
        
    async def send_registration(self, user_data: Dict[str, Any], dadata_client=None) -> Optional[str]:
        """
        Отправка данных регистрации на сервер Bitrix24
        
        Args:
            user_data: {
                'name': str,           # ФИО
                'inn': str,            # ИНН
                'phone': str,          # Телефон (форматированный)
                'email': str,          # Email
                'company_name': str,   # Название компании (из DaData)
                'company_type': str,   # Тип: 'individual' (ИП) или 'organization'
                'is_individual': bool, # True если ИП/Физлицо
            }
            dadata_client: Экземпляр DadataClient для проверки НПД статуса
        
        Returns:
            ID регистрации или None в случае ошибки
        """
        logger.info(f"Sending registration to Bitrix24: INN={user_data.get('inn', 'N/A')[:4]}****")
        
        try:
            name = user_data.get('name', '')
            inn = user_data.get('inn', '')
            phone_formatted = user_data.get('phone', '')
            email = user_data.get('email', '')
            company_type = user_data.get('company_type', '')
            company_name = user_data.get('company_name', '')
            is_individual = user_data.get('is_individual', False)
            
            # Определяем тип регистрации
            registration_type = await self._determine_registration_type(
                inn=inn,
                company_type=company_type,
                is_individual=is_individual,
                dadata_client=dadata_client
            )
            
            # Формируем поле Org в зависимости от типа
            if registration_type == "INDIVIDUAL":
                # ИП - пишем "ИП ФИО"
                org = f"ИП {name}" if name else "ИП"
            elif registration_type == "LEGAL":
                # Организация - пишем название компании
                org = company_name if company_name else "Организация"
            elif registration_type == "NPD":
                # Самозанятый - просто ФИО
                org = name if name else "Самозанятый"
            else:
                # Физическое лицо - просто ФИО
                org = name if name else "Физическое лицо"
            
            # Формируем JSON payload с правильным типом
            payload = {
                "NAME": name,
                "inn": inn,
                "org": org,
                "phone": phone_formatted,
                "Email": email,
                "Type": registration_type,
            }
            
            logger.info(f"Sending JSON payload to {self.register_url}")
            logger.info(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
            logger.info(f"Registration type: {registration_type}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.register_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"Response body: {response.text[:500]}")
                
                response_text = response.text.strip()
                
                # Пробуем разные способы извлечения ID
                reg_id = self._extract_id_from_response(response_text)
                
                if reg_id:
                    logger.info(f"✅ Registration sent to Bitrix24, ID: {reg_id}, Type: {registration_type}")
                    return reg_id
                
                logger.error(f"Cannot extract ID from response: {response_text[:200]}")
                return None
                
        except httpx.TimeoutException:
            logger.error(f"Timeout while sending to Bitrix24")
            return None
        except httpx.HTTPError as e:
            logger.error(f"HTTP error sending to Bitrix24: {e}")
            return None
        except Exception as e:
            logger.error(f"Error sending to Bitrix24: {e}")
            return None
    
    async def _determine_registration_type(self, inn: str, company_type: str, 
                                          is_individual: bool, dadata_client=None) -> str:
        """
        Определение типа регистрации для Bitrix24
        
        Args:
            inn: ИНН
            company_type: Тип компании из DaData ('individual', 'organization')
            is_individual: Флаг ИП/физлица
            dadata_client: Клиент DaData для проверки НПД
            
        Returns:
            Тип регистрации: "LEGAL", "INDIVIDUAL", "NPD", "FIZ"
        """
        logger.info(f"Determining registration type for INN: {inn[:4]}****")
        logger.info(f"  company_type: {company_type}, is_individual: {is_individual}")
        
        # Если это организация (юридическое лицо)
        if company_type == 'organization':
            logger.info("  → Type: LEGAL (organization)")
            return "LEGAL"
        
        # Если это ИП (индивидуальный предприниматель)
        if company_type == 'individual':
            logger.info("  → Type: INDIVIDUAL (individual entrepreneur)")
            return "INDIVIDUAL"
        
        # Если это физическое лицо или неопределенный тип
        if is_individual or company_type == '' or company_type is None:
            # Проверяем, является ли самозанятым через API ФНС
            if dadata_client and inn:
                try:
                    logger.info(f"  Checking NPD status via DaData client...")
                    npd_status = await dadata_client.check_npd_status(inn)
                    
                    if npd_status and npd_status.get("is_npd"):
                        logger.info(f"  → Type: NPD (self-employed confirmed by FNS)")
                        return "NPD"
                    else:
                        logger.info(f"  → Not NPD, checking INN length...")
                except Exception as e:
                    logger.error(f"  Error checking NPD status: {e}, falling back to INN length check")
            
            # Если не удалось проверить через API или нет dadata_client
            # Определяем по длине ИНН
            if inn and len(inn) == 12:
                # ИНН из 12 цифр может быть у физлица или ИП
                # Если нет данных из DaData, пробуем определить по другим признакам
                logger.info(f"  → Type: FIZ (individual with 12-digit INN)")
                return "FIZ"
            elif inn and len(inn) == 10:
                # ИНН из 10 цифр - это юрлицо
                logger.info(f"  → Type: LEGAL (10-digit INN)")
                return "LEGAL"
            else:
                # По умолчанию - физическое лицо
                logger.info(f"  → Type: FIZ (default)")
                return "FIZ"
        
        # По умолчанию
        logger.info(f"  → Type: FIZ (fallback)")
        return "FIZ"
    
    def _extract_id_from_response(self, response_text: str) -> Optional[str]:
        """
        Извлечение ID из ответа сервера
        
        Args:
            response_text: Текст ответа от сервера
            
        Returns:
            ID регистрации или None
        """
        logger.debug(f"Extracting ID from response: {response_text[:200]}")
        
        # Учитываем, что сервер возвращает $resultJson='{"id": "97854"}';
        json_patterns = [
            r'\{\s*"id"\s*:\s*"(\d+)"\s*\}',  # { "id" : "97854" }
            r'\{\s*"id"\s*:\s*(\d+)\s*\}',      # { "id" : 97854 }
            r'"id"\s*:\s*"(\d+)"',              # "id" : "97854"
            r'"id"\s*:\s*(\d+)',                # "id" : 97854
            r'id["\']?\s*[=:]\s*["\']?(\d+)',   # id: 97854 или id="97854"
        ]
        
        for pattern in json_patterns:
            match = re.search(pattern, response_text)
            if match:
                reg_id = match.group(1)
                logger.info(f"Found ID using pattern '{pattern}': {reg_id}")
                return reg_id
        
        # Пробуем найти JSON в ответе и распарсить его
        try:
            json_match = re.search(r'\{[^}]+\}', response_text)
            if json_match:
                json_str = json_match.group()
                result = json.loads(json_str)
                if isinstance(result, dict) and 'id' in result:
                    reg_id = str(result['id'])
                    logger.info(f"Found ID from JSON: {reg_id}")
                    return reg_id
        except json.JSONDecodeError:
            pass
        
        # Пробуем распарсить весь ответ как JSON
        try:
            result = json.loads(response_text)
            if isinstance(result, dict) and 'id' in result:
                reg_id = str(result['id'])
                logger.info(f"Found ID from full JSON: {reg_id}")
                return reg_id
        except json.JSONDecodeError:
            pass
        
        logger.warning(f"Could not extract ID from response")
        return None
    
    async def get_registration_list(self) -> Optional[list]:
        """
        Получение списка регистраций с сервера Bitrix24
        
        Returns:
            Список регистраций или None в случае ошибки
        """
        logger.info("Getting registration list from Bitrix24")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.list_url)
                response.raise_for_status()
                
                result = response.json()
                
                if isinstance(result, list):
                    logger.info(f"✅ Got {len(result)} registrations from Bitrix24")
                    return result
                else:
                    logger.error(f"Unexpected response format: {type(result)}")
                    return None
                    
        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting list from Bitrix24: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting list from Bitrix24: {e}")
            return None
    
    async def check_duplicate(self, inn: str, phone: str) -> bool:
        """
        Проверка на дубликат регистрации по ИНН и телефону
        
        Args:
            inn: ИНН для проверки
            phone: Телефон для проверки
            
        Returns:
            True если дубликат найден
        """
        logger.info(f"Checking duplicate for INN: {inn[:4]}****")
        
        try:
            registrations = await self.get_registration_list()
            
            if not registrations:
                logger.warning("Could not get registration list, skipping duplicate check")
                return False
            
            for reg in registrations:
                reg_inn = reg.get('inn', reg.get('INN', ''))
                reg_phone = reg.get('phone', reg.get('Phone', reg.get('PHONE', '')))
                
                clean_phone = ''.join(filter(str.isdigit, phone))
                clean_reg_phone = ''.join(filter(str.isdigit, reg_phone))
                
                if reg_inn == inn or (clean_phone and clean_reg_phone and clean_phone == clean_reg_phone):
                    logger.warning(f"Duplicate found! INN: {inn[:4]}****")
                    return True
            
            logger.info("No duplicate found")
            return False
            
        except Exception as e:
            logger.error(f"Error checking duplicate: {e}")
            return False


# Пример использования с DaData клиентом
async def example_with_npd_check():
    """Пример отправки регистрации с проверкой НПД статуса"""
    from dadata_client import DadataClient
    
    # Создаем клиенты
    bitrix_client = BitrixClient()
    
    # Данные пользователя (пример для самозанятого)
    user_data = {
        "name": "ТЕСТОВОЕ ИМЯ ФАМИЛИЯ",
        "inn": "246417869701",
        "phone": "79676030166",
        "email": "960866@xmail.ru",
        "company_name": "",
        "company_type": "",
        "is_individual": True
    }
    
    # Используем DaData клиент для проверки НПД
    async with DadataClient() as dadata_client:
        result = await bitrix_client.send_registration(
            user_data=user_data,
            dadata_client=dadata_client
        )
        
        if result:
            print(f"✅ Registration successful! ID: {result}")
        else:
            print("❌ Registration failed!")


if __name__ == '__main__':
    import asyncio
    asyncio.run(example_with_npd_check())