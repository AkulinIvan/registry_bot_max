"""Клиент для отправки данных регистрации в Bitrix24"""
import httpx
import logging
import re
import json
from typing import Optional, Dict, Any, List

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
        self.list_url = list_url or "https://bitrix.neto.ru/.bot_dp_register_list.php"
        self.timeout = timeout
        logger.info(f"BitrixClient initialized")
        logger.info(f"  Register URL: {self.register_url}")
        logger.info(f"  List URL: {self.list_url}")
    
    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Нормализация телефона для сравнения
        Приводит к формату: 79676030166 (11 цифр, начиная с 7)
        
        Args:
            phone: Номер телефона в любом формате
        
        Returns:
            Нормализованный номер (только цифры)
        
        Examples:
            >>> BitrixClient.normalize_phone("+7 (967) 603-01-66")
            '79676030166'
            >>> BitrixClient.normalize_phone("89676030166")
            '79676030166'
            >>> BitrixClient.normalize_phone("79676030166")
            '79676030166'
        """
        if not phone:
            return ""
        
        # Удаляем все нецифровые символы
        digits = ''.join(filter(str.isdigit, phone))
        
        # Приводим к формату 7XXXXXXXXXX
        if len(digits) == 11 and digits.startswith('8'):
            return '7' + digits[1:]
        elif len(digits) == 10:
            return '7' + digits
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
            raw_phone = user_data.get('phone', '')
            email = user_data.get('email', '')
            company_type = user_data.get('company_type', '')
            company_name = user_data.get('company_name', '')
            is_individual = user_data.get('is_individual', False)
            
            # Нормализуем телефон
            phone_formatted = self.normalize_phone(raw_phone)
            logger.info(f"Phone normalized: '{raw_phone}' → '{phone_formatted}'")
            
            # Определяем тип регистрации
            registration_type = await self._determine_registration_type(
                inn=inn,
                company_type=company_type,
                is_individual=is_individual,
                dadata_client=dadata_client
            )
            
            # Формируем поле Org в зависимости от типа
            if registration_type == "INDIVIDUAL":
                org = f"ИП {name}" if name else "ИП"
            elif registration_type == "LEGAL":
                org = company_name if company_name else "Организация"
            elif registration_type == "NPD":
                org = name if name else "Самозанятый"
            else:
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
        
        if company_type == 'organization':
            logger.info("  → Type: LEGAL (organization)")
            return "LEGAL"
        
        if company_type == 'individual':
            logger.info("  → Type: INDIVIDUAL (individual entrepreneur)")
            return "INDIVIDUAL"
        
        if is_individual or company_type == '' or company_type is None:
            if dadata_client and inn:
                try:
                    logger.info(f"  Checking NPD status via DaData client...")
                    npd_status = await dadata_client.check_npd_status(inn)
                    
                    if npd_status and npd_status.get("is_npd"):
                        logger.info(f"  → Type: NPD (self-employed confirmed by FNS)")
                        return "NPD"
                except Exception as e:
                    logger.error(f"  Error checking NPD status: {e}")
            
            if inn and len(inn) == 12:
                logger.info(f"  → Type: FIZ (individual with 12-digit INN)")
                return "FIZ"
            elif inn and len(inn) == 10:
                logger.info(f"  → Type: LEGAL (10-digit INN)")
                return "LEGAL"
            else:
                logger.info(f"  → Type: FIZ (default)")
                return "FIZ"
        
        logger.info(f"  → Type: FIZ (fallback)")
        return "FIZ"
    
    def _extract_id_from_response(self, response_text: str) -> Optional[str]:
        """
        Извлечение ID из ответа сервера
        Сервер возвращает: $resultJson='{"id": "97854"}'
        Или просто: {"Leadid":"98835","id":"197",...}
        """
        logger.debug(f"Extracting ID from response: {response_text[:200]}")

        # Сначала ищем Leadid (основной ID лида в Bitrix24)
        lead_id_patterns = [
            r'"Leadid"\s*:\s*"(\d+)"',     # "Leadid" : "98835"
            r'"Leadid"\s*:\s*(\d+)',        # "Leadid" : 98835
        ]

        for pattern in lead_id_patterns:
            match = re.search(pattern, response_text)
            if match:
                lead_id = match.group(1)
                logger.info(f"Found Leadid using pattern: {lead_id}")
                return lead_id

        # Если Leadid не найден, ищем обычный id
        json_patterns = [
            r'\{\s*"id"\s*:\s*"(\d+)"\s*\}',
            r'\{\s*"id"\s*:\s*(\d+)\s*\}',
            r'"id"\s*:\s*"(\d+)"',
            r'"id"\s*:\s*(\d+)',
            r'id["\']?\s*[=:]\s*["\']?(\d+)',
        ]

        for pattern in json_patterns:
            match = re.search(pattern, response_text)
            if match:
                reg_id = match.group(1)
                logger.info(f"Found id (fallback): {reg_id}")
                return reg_id

        # Пробуем распарсить JSON
        try:
            json_match = re.search(r'\{[^}]+\}', response_text)
            if json_match:
                result = json.loads(json_match.group())
                if isinstance(result, dict):
                    # Приоритет: Leadid > id
                    if 'Leadid' in result:
                        return str(result['Leadid'])
                    elif 'id' in result:
                        return str(result['id'])
        except json.JSONDecodeError:
            pass
        
        try:
            result = json.loads(response_text)
            if isinstance(result, dict):
                if 'Leadid' in result:
                    return str(result['Leadid'])
                elif 'id' in result:
                    return str(result['id'])
        except json.JSONDecodeError:
            pass
        
        logger.warning("Could not extract ID from response")
        return None
    
    async def get_registration_list(self) -> Optional[List[Dict[str, Any]]]:
        """
        Получение списка ВСЕХ регистраций с сервера Bitrix24
        
        Returns:
            Список регистраций или None в случае ошибки
        """
        logger.info("Getting registration list from Bitrix24")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.list_url)
                response.raise_for_status()
                
                response_text = response.text.strip()
                logger.debug(f"Raw response: {response_text[:500]}")
                
                # Пробуем распарсить ответ
                try:
                    result = response.json()
                except json.JSONDecodeError:
                    # Может быть JSON в JSONP или другой обертке
                    json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
                    else:
                        logger.error(f"Cannot parse response: {response_text[:200]}")
                        return None
                
                if isinstance(result, list):
                    logger.info(f"✅ Got {len(result)} registrations from Bitrix24")
                    return result
                elif isinstance(result, dict):
                    # Если вернулся один объект, а не массив
                    logger.info(f"✅ Got single registration from Bitrix24")
                    return [result]
                else:
                    logger.error(f"Unexpected response format: {type(result)}")
                    return None
                    
        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting list from Bitrix24: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting list from Bitrix24: {e}")
            return None
    
    async def find_registration_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """
        Поиск регистрации по номеру телефона в Bitrix24
        
        Returns:
            Данные регистрации с полями: id, Leadid, Inn, Phone
        """
        logger.info(f"Searching registration in Bitrix24 by phone: {phone[:4]}****")
        
        search_phone = self.normalize_phone(phone)
        
        if not search_phone:
            return None
        
        try:
            registrations = await self.get_registration_list()
            
            if not registrations:
                return None
            
            for reg in registrations:
                reg_phone = reg.get('Phone', reg.get('phone', ''))
                normalized_reg_phone = self.normalize_phone(reg_phone)
                
                if normalized_reg_phone == search_phone:
                    logger.info(f"✅ Found: Leadid={reg.get('Leadid')}, id={reg.get('id')}, Phone={reg_phone}")
                    return reg
            
            return None
            
        except Exception as e:
            logger.error(f"Error searching registration by phone: {e}")
            return None
    
    async def check_duplicate_by_phone(self, phone: str) -> bool:
        """
        Проверка наличия регистрации по телефону в Bitrix24
        
        Args:
            phone: Номер телефона для проверки
        
        Returns:
            True если регистрация найдена
        """
        logger.info(f"Checking duplicate in Bitrix24 by phone: {phone[:4]}****")
        
        registration = await self.find_registration_by_phone(phone)
        
        if registration:
            logger.warning(f"⚠️ Duplicate found in Bitrix24! Phone: {phone[:4]}****, ID: {registration.get('id')}")
            return True
        
        logger.info(f"✅ No duplicate found in Bitrix24 for phone: {phone[:4]}****")
        return False
    
    async def check_duplicate(self, inn: str = None, phone: str = None) -> bool:
        """
        Проверка на дубликат регистрации по ИНН и/или телефону
        
        Args:
            inn: ИНН для проверки (опционально)
            phone: Телефон для проверки (опционально)
            
        Returns:
            True если дубликат найден
        """
        logger.info(f"Checking duplicate in Bitrix24 - INN: {inn[:4] if inn else 'N/A'}****, Phone: {phone[:4] if phone else 'N/A'}****")
        
        try:
            registrations = await self.get_registration_list()
            
            if not registrations:
                logger.warning("Could not get registration list, skipping duplicate check")
                return False
            
            # Нормализуем телефон для сравнения
            normalized_phone = self.normalize_phone(phone) if phone else ""
            
            for reg in registrations:
                reg_inn = reg.get('Inn', reg.get('inn', reg.get('INN', '')))
                reg_phone = self.normalize_phone(
                    reg.get('Phone', reg.get('phone', reg.get('PHONE', '')))
                )
                
                # Проверка по ИНН
                if inn and reg_inn == inn:
                    logger.warning(f"Duplicate found by INN: {inn[:4]}****")
                    return True
                
                # Проверка по телефону
                if normalized_phone and reg_phone and normalized_phone == reg_phone:
                    logger.warning(f"Duplicate found by Phone: {phone[:4]}****")
                    return True
            
            logger.info("No duplicate found in Bitrix24")
            return False
            
        except Exception as e:
            logger.error(f"Error checking duplicate in Bitrix24: {e}")
            return False


# Пример использования
async def test_bitrix_check():
    """Тест проверки дубликатов в Bitrix24"""
    print("\n" + "=" * 50)
    print("🧪 Testing Bitrix24 Duplicate Check")
    print("=" * 50 + "\n")
    
    client = BitrixClient()
    
    # Тест 1: Проверка по телефону
    test_phone = "+7 (967) 603-01-66"
    print(f"📱 Checking phone: {test_phone}")
    duplicate = await client.check_duplicate_by_phone(test_phone)
    print(f"  Result: {'❌ Duplicate found!' if duplicate else '✅ No duplicate'}\n")
    
    # Тест 2: Проверка по ИНН и телефону
    test_inn = "246417869701"
    test_phone2 = "79676030166"
    print(f"📋 Checking INN: {test_inn[:4]}**** and Phone: {test_phone2[:4]}****")
    duplicate = await client.check_duplicate(inn=test_inn, phone=test_phone2)
    print(f"  Result: {'❌ Duplicate found!' if duplicate else '✅ No duplicate'}\n")
    
    # Тест 3: Поиск регистрации по телефону
    print(f"🔍 Searching registration by phone: {test_phone}")
    reg = await client.find_registration_by_phone(test_phone)
    if reg:
        print(f"  ✅ Found: ID={reg.get('id')}, INN={reg.get('Inn', '')[:4]}****, Phone={reg.get('Phone')}")
    else:
        print(f"  ❌ Not found\n")


if __name__ == '__main__':
    import asyncio
    asyncio.run(test_bitrix_check())