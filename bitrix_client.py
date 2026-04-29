"""Клиент для отправки данных регистрации в Bitrix24"""
import httpx
import logging
import re
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class BitrixClient:
    """Клиент для работы с API Bitrix24"""
    
    def __init__(self, register_url: str = None, list_url: str = None, timeout: float = 10.0):
        """
        Инициализация клиента
        
        Args:
            register_url: URL для регистрации пользователя
            list_url: URL для получения списка регистраций
            timeout: Таймаут запроса в секундах
        """
        self.register_url = register_url or "https://bitrix.neto.ru/.bot_dp_register.php"
        self.list_url = list_url or "https://bitrix.neto.ru/.bot_dp_register_list.php"
        self.timeout = timeout
        logger.info(f"BitrixClient initialized")
        logger.info(f"  Register URL: {self.register_url}")
        logger.info(f"  List URL: {self.list_url}")
    
    async def send_registration(self, user_data: Dict[str, Any]) -> Optional[str]:
        """
        Отправка данных регистрации на сервер Bitrix24
        
        Args:
            user_data: {
                'name': str,           # ФИО
                'inn': str,            # ИНН
                'phone': str,          # Телефон (форматированный)
                'phone_raw': str,      # Телефон (только цифры)
                'email': str,          # Email
                'company_name': str,   # Название компании (из DaData)
                'company_type': str,   # Тип: 'individual' (ИП) или 'organization' (ООО и т.д.)
                'is_individual': bool, # True если ИП/Физлицо
            }
            
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
            
            # Формируем поле Org
            if company_type == 'individual':
                # ИП - пишем "ИП ФИО"
                org = f"ИП {name}" if name else "ИП"
            elif company_type == 'organization':
                # Организация - пишем название компании
                org = company_name if company_name else "Организация"
            else:
                # Самозанятый или физлицо - просто ФИО
                org = name if name else "Самозанятый"
            
            # Формируем payload точно как ожидает сервер
            payload = {
                "Name": name,
                "inn": inn,
                "Org": org,
                "Phone": phone_formatted,
                "Email": email,
            }
            
            logger.info(f"Sending payload: Name={name}, inn={inn[:4]}****, Org={org}, Phone={phone_formatted[:4]}****, Email={email}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.register_url,
                    data=payload
                )
                
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"Response body: {response.text[:300]}")
                
                response_text = response.text
                
                # Извлекаем ID из JSON ответа
                json_match = re.search(r'\{\s*"id"\s*:\s*"(\d+)"\s*\}', response_text)
                
                if json_match:
                    reg_id = json_match.group(1)
                    # Игнорируем тестовый ID 97854
                    if reg_id == "97854":
                        logger.warning(f"Server returned test ID 97854, generating local ID")
                        return None
                    logger.info(f"✅ Registration sent to Bitrix24, ID: {reg_id}")
                    return reg_id
                
                # Пробуем распарсить чистый JSON
                try:
                    import json
                    first_line = response_text.split('\n')[0].strip()
                    result = json.loads(first_line)
                    if isinstance(result, dict) and 'id' in result:
                        reg_id = str(result['id'])
                        if reg_id == "97854":
                            logger.warning(f"Server returned test ID 97854, generating local ID")
                            return None
                        logger.info(f"✅ Registration ID from JSON: {reg_id}")
                        return reg_id
                except:
                    pass
                
                logger.error(f"Cannot extract ID from response")
                return None
                
        except Exception as e:
            logger.error(f"Error sending to Bitrix24: {e}")
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
                reg_phone = reg.get('Phone', reg.get('PHONE', ''))
                
                # Очищаем телефон от форматирования для сравнения
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