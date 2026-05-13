"""Сервис для отметки участников на мероприятиях"""
import json
import logging
import re
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass

import httpx
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import CallbackButton

from config import AppConfig

config = AppConfig()
logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Модель мероприятия"""
    id: str
    name: str
    date: Optional[str] = None
    location: Optional[str] = None


@dataclass
class CheckInResult:
    """Результат отметки участника"""
    success: bool
    lead_id: str = ""
    event_id: str = ""
    event_name: str = ""
    message: str = ""
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class CheckInService:
    """Сервис для работы с отметками участников"""

    def __init__(self):
        self.base_url = "https://bitrix.neto.ru"
        self.events: Dict[str, Event] = {}
        self.current_event_id: Optional[str] = None
        self.current_event_name: Optional[str] = None

        logger.info("CheckInService initialized")

    async def fetch_events(self) -> List[Event]:
        """
        Получение списка мероприятий с API
        GET https://bitrix.neto.ru/.bot_dp_tema.php
        """
        logger.info("Fetching events from API...")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/.bot_dp_tema.php",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "MAX-Bot/1.0"
                    }
                )

                logger.debug(f"API Response status: {response.status_code}")

                if response.status_code != 200:
                    logger.error(f"Failed to fetch events: HTTP {response.status_code}")
                    return []

                data = response.json()
                events = self._parse_events(data)
                self.events = {e.id: e for e in events}

                logger.info(f"Loaded {len(events)} events")
                return events

        except Exception as e:
            logger.error(f"Error fetching events: {e}")
            return []

    def _parse_events(self, data: Any) -> List[Event]:
        """Парсинг ответа API в список мероприятий"""
        events = [] 

        try:
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get('data') or data.get('items') or data.get('results') or []  

            for item in items:
                if isinstance(item, dict):
                    event_id = str(item.get('id') or item.get('ID') or '')

                    # 🔧 ИСПРАВЛЕНИЕ: поле называется 'value', а не 'name'
                    name = str(
                        item.get('value') or          # ← ВАШ ФОРМАТ
                        item.get('name') or           # запасной вариант
                        item.get('title') or 
                        item.get('NAME') or 
                        ''
                    )   

                    if event_id and name:
                        events.append(Event(
                            id=event_id,
                            name=name,
                            date=str(item.get('date', '')) if item.get('date') else None,
                            location=str(item.get('location', '')) if item.get('location') else None
                        ))  

            # 🔧 ДОБАВИТЬ ЛОГ ДЛЯ ОТЛАДКИ
            logger.info(f"Parsed {len(events)} events from {len(items)} items")
            if events:
                logger.debug(f"First event: id={events[0].id}, name={events[0].name[:50]}") 

        except Exception as e:
            logger.error(f"Error parsing events: {e}", exc_info=True)   

        return events

    def select_event(self, event_id: str) -> bool:
        """Выбор мероприятия по ID"""
        event = self.events.get(event_id)
        if event:
            self.current_event_id = event.id
            self.current_event_name = event.name
            logger.info(f"Selected event: {event.name} (ID: {event.id})")
            return True

        logger.warning(f"Event {event_id} not found")
        return False

    async def check_in_participant(self, qr_url: str) -> CheckInResult:
        """
        Отметка участника по URL из QR-кода
        
        Логика:
        1. Из QR извлекается URL: https://bitrix.neto.ru/lead.php?leadid=ID_ЛИДА
        2. К URL добавляется &eventid=ID_МЕРОПРИЯТИЯ
        3. Делается GET-запрос на этот URL
        4. Ответ — JSON с ID лида
        
        Args:
            qr_url: URL из QR-кода
            
        Returns:
            Результат отметки
        """
        if not self.current_event_id:
            return CheckInResult(
                success=False,
                message="❌ Не выбрано мероприятие. Сначала выберите мероприятие."
            )

        # Извлекаем ID лида из URL
        lead_id = self.extract_lead_id_from_qr(qr_url)
        if not lead_id:
            logger.warning(f"Cannot extract lead ID from QR: {qr_url}")
            return CheckInResult(
                success=False,
                message="❌ Не удалось извлечь ID участника из QR-кода.\nПроверьте QR-код и попробуйте снова."
            )

        # Проверяем, что ID лида в ожидаемом диапазоне
        try:
            lead_num = int(lead_id)
            if not (97000 <= lead_num <= 100000):
                logger.warning(f"Lead ID {lead_id} outside expected range (97854-99999)")
                # Не блокируем, но логируем
        except ValueError:
            pass

        # Формируем URL для отметки
        # Исходный URL из QR: https://bitrix.neto.ru/lead.php?leadid=ID_ЛИДА
        # Добавляем eventid
        check_url = f"{qr_url}&eventid={self.current_event_id}" if '?' in qr_url else f"{qr_url}?eventid={self.current_event_id}"

        logger.info(f"Check-in URL: {check_url}")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    check_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "MAX-Bot/1.0"
                    }
                )

                logger.debug(f"Check-in response: {response.status_code} - {response.text[:300]}")

                if response.status_code == 200:
                    try:
                        result_data = response.json()
                        # Ожидаемый формат: {"id": 99999}
                        returned_id = result_data.get('id', lead_id)

                        return CheckInResult(
                            success=True,
                            lead_id=str(returned_id),
                            event_id=self.current_event_id,
                            event_name=self.current_event_name or "",
                            message=f" Участник {returned_id} успешно отмечен на мероприятии"
                        )
                    except Exception as e:
                        # Если не JSON, но статус 200 — считаем успехом
                        logger.warning(f"Non-JSON response: {response.text[:200]}")
                        return CheckInResult(
                            success=True,
                            lead_id=lead_id,
                            event_id=self.current_event_id,
                            event_name=self.current_event_name or "",
                            message=f" Участник {lead_id} отмечен (ответ не в JSON)"
                        )
                else:
                    return CheckInResult(
                        success=False,
                        lead_id=lead_id,
                        message=f"❌ Ошибка отметки: HTTP {response.status_code}\nПроверьте ID участника и мероприятия."
                    )

        except httpx.TimeoutException:
            logger.error(f"Timeout checking in participant {lead_id}")
            return CheckInResult(
                success=False,
                lead_id=lead_id,
                message="❌ Таймаут при отметке участника. Попробуйте снова."
            )
        except Exception as e:
            logger.error(f"Error checking in participant: {e}")
            return CheckInResult(
                success=False,
                lead_id=lead_id,
                message=f"❌ Ошибка: {str(e)}"
            )

    async def check_in_by_lead_id(self, lead_id: str) -> CheckInResult:
        """
        Отметка участника по ID лида (без QR)
        """
        if not self.current_event_id:
            return CheckInResult(
                success=False,
                lead_id=lead_id,
                message="❌ Не выбрано мероприятие."
            )

        # Формируем полный URL
        qr_url = f"https://bitrix.neto.ru/lead.php?leadid={lead_id}"
        return await self.check_in_participant(qr_url)

    @staticmethod
    def extract_lead_id_from_qr(qr_data: str) -> Optional[str]:
        """
        Извлечение ID лида из QR-кода
        
        Формат QR: https://bitrix.neto.ru/lead.php?leadid=ID_ЛИДА
        
        Args:
            qr_data: данные из QR-кода (URL или просто ID)
            
        Returns:
            ID лида или None
        """
        if not qr_data:
            return None

        qr_data = qr_data.strip()

        # Если это URL с leadid
        match = re.search(r'leadid=([^&\s]+)', qr_data, re.IGNORECASE)
        if match:
            return match.group(1)

        # Если это URL с id
        match = re.search(r'[?&]id=([^&\s]+)', qr_data)
        if match:
            return match.group(1)

        # Если это просто ID (число)
        clean = qr_data.strip()
        if clean.isdigit():
            return clean

        # Если это ID в формате DP-XXXX
        if re.match(r'^[A-Za-z0-9\-_]+$', clean):
            return clean

        return None

    def get_events_keyboard(self, page: int = 0, per_page: int = 10):
        """
        Создание клавиатуры с мероприятиями (с пагинацией)

        Args:
            page: номер страницы (начиная с 0)
            per_page: количество мероприятий на странице
        """
        builder = InlineKeyboardBuilder()

        events_list = list(self.events.values())
        total_events = len(events_list)
        total_pages = (total_events + per_page - 1) // per_page

        # Вычисляем границы текущей страницы
        start_idx = page * per_page
        end_idx = min(start_idx + per_page, total_events)

        # Добавляем мероприятия текущей страницы
        for event in events_list[start_idx:end_idx]:
            # Обрезаем длинные названия
            name = event.name[:40] + "..." if len(event.name) > 40 else event.name
            date_str = f" | {event.date}" if event.date else ""

            builder.row(CallbackButton(
                text=f"📅 {name}{date_str}",
                callback_data=f"select_event_{event.id}",
                payload=f"select_event_{event.id}"
            ))

        # Добавляем навигацию по страницам
        if total_pages > 1:
            nav_row = []

            if page > 0:
                nav_row.append(CallbackButton(
                    text="◀️ Назад",
                    callback_data=f"events_page_{page - 1}",
                    payload=f"events_page_{page - 1}"
                ))

            # Индикатор страницы
            nav_row.append(CallbackButton(
                text=f"📄 {page + 1}/{total_pages}",
                callback_data="events_page_current",
                payload="events_page_current"
            ))

            if page < total_pages - 1:
                nav_row.append(CallbackButton(
                    text="Вперёд ▶️",
                    callback_data=f"events_page_{page + 1}",
                    payload=f"events_page_{page + 1}"
                ))

            builder.row(*nav_row)

        # Кнопки действий
        builder.row(CallbackButton(
            text="🔄 Обновить список",
            callback_data="refresh_events",
            payload="refresh_events"
        ))
        builder.row(CallbackButton(
            text="◀️ Назад в админ-панель",
            callback_data="admin_back",
            payload="admin_back"
        ))

        return builder.as_markup()

    def get_current_event_info(self) -> str:
        """Информация о текущем мероприятии"""
        if self.current_event_id:
            return (
                f"📅 Текущее мероприятие:\n"
                f"   ID: {self.current_event_id}\n"
                f"   Название: {self.current_event_name}\n\n"
            )
        return "❌ Мероприятие не выбрано\n\n"

    async def find_lead_by_id(self, lead_id: str) -> Optional[Dict[str, Any]]:
        """
        Поиск лида по ID (Leadid) в списке Bitrix24

        Args:
            lead_id: ID лида (Leadid)

        Returns:
            Данные лида или None
        """
        logger.info(f"Searching for lead: {lead_id}")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/.bot_dp_register_list.php",
                    params={"id": lead_id},
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "MAX-Bot/1.0"
                    }
                )

                logger.debug(f"Lead search response: status={response.status_code}")

                if response.status_code == 200:
                    try:
                        data = response.json()

                        # API возвращает список всех лидов
                        if isinstance(data, list):
                            logger.info(f"Searching in {len(data)} leads for Leadid={lead_id}")

                            # Ищем лида с нужным Leadid
                            for item in data:
                                if isinstance(item, dict):
                                    item_lead_id = str(item.get('Leadid', ''))
                                    if item_lead_id == str(lead_id):
                                        logger.info(f"✅ Found lead: {item}")
                                        return item

                            logger.warning(f"Lead with Leadid={lead_id} not found in {len(data)} items")
                            return None

                        elif isinstance(data, dict):
                            logger.info(f"Lead data keys: {list(data.keys())}")
                            return data

                    except Exception as e:
                        logger.warning(f"Failed to parse lead data: {e}")
                        logger.debug(f"Raw response: {response.text[:500]}")

                # Пробуем lead.php как запасной вариант
                logger.info(f"Trying lead.php for lead {lead_id}")
                response2 = await client.get(
                    f"{self.base_url}/lead.php",
                    params={"leadid": lead_id},
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "MAX-Bot/1.0"
                    }
                )

                if response2.status_code == 200:
                    try:
                        return response2.json()
                    except:
                        return {"Leadid": lead_id}

                logger.warning(f"Lead {lead_id} not found")
                return None

        except Exception as e:
            logger.error(f"Error finding lead {lead_id}: {e}")
            return None
    
# Инициализация сервиса
checkin_service = CheckInService()