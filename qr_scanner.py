"""Модуль для распознавания QR-кодов из изображений"""
import logging
import io
from typing import Optional

logger = logging.getLogger(__name__)

# Пробуем импортировать библиотеки
try:
    import cv2
    import numpy as np
    from pyzbar.pyzbar import decode
    from PIL import Image
    
    QR_AVAILABLE = True
    logger.info("✅ QR scanner libraries loaded successfully")
except ImportError as e:
    QR_AVAILABLE = False
    logger.warning(f"⚠️ QR scanner not available: {e}")
    logger.warning("Install: pip install opencv-python-headless pyzbar Pillow")


def decode_qr_from_bytes(image_bytes: bytes) -> Optional[str]:
    """
    Распознавание QR-кода из байтов изображения
    
    Args:
        image_bytes: изображение в байтах (JPEG, PNG и т.д.)
        
    Returns:
        Распознанный текст из QR-кода или None
    """
    if not QR_AVAILABLE:
        logger.warning("QR scanner libraries not installed")
        return None
    
    try:
        # Способ 1: Через OpenCV + pyzbar (более надёжный)
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            logger.warning("Failed to decode image with OpenCV")
        else:
            # Пробуем разные методы улучшения изображения
            for method_name, processed_img in _get_image_variants(img):
                decoded_objects = decode(processed_img)
                if decoded_objects:
                    qr_data = decoded_objects[0].data.decode('utf-8')
                    logger.info(f"✅ QR decoded via {method_name}: {qr_data[:100]}")
                    return qr_data
        
        # Способ 2: Через Pillow (если OpenCV не сработал)
        try:
            from PIL import Image
            pil_image = Image.open(io.BytesIO(image_bytes))
            decoded_objects = decode(pil_image)
            if decoded_objects:
                qr_data = decoded_objects[0].data.decode('utf-8')
                logger.info(f"✅ QR decoded via Pillow: {qr_data[:100]}")
                return qr_data
        except Exception as e:
            logger.debug(f"Pillow decode failed: {e}")
        
        logger.warning("❌ No QR code found in image")
        return None
        
    except Exception as e:
        logger.error(f"Error decoding QR: {e}")
        return None


def _get_image_variants(img):
    """
    Генератор различных вариантов обработки изображения
    для повышения шансов распознавания QR-кода
    """
    try:
        import cv2
        
        # 1. Оригинальное изображение
        yield "original", img
        
        # 2. Grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        yield "grayscale", gray
        
        # 3. Увеличение контраста (CLAHE)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        yield "clahe", enhanced
        
        # 4. Простая бинаризация
        _, binary = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY)
        yield "binary", binary
        
        # 5. Адаптивная бинаризация
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2
        )
        yield "adaptive", adaptive
        
        # 6. Размытие + бинаризация (убирает шум)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        _, blurred_binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield "blurred_binary", blurred_binary
        
        # 7. Увеличение размера (если QR маленький)
        height, width = gray.shape[:2]
        if width < 500 or height < 500:
            scale = 2
            enlarged = cv2.resize(img, (width * scale, height * scale), interpolation=cv2.INTER_CUBIC)
            yield "enlarged", enlarged
            
    except Exception as e:
        logger.debug(f"Error in image variants: {e}")


def decode_qr_from_file(file_path: str) -> Optional[str]:
    """
    Распознавание QR-кода из файла
    
    Args:
        file_path: путь к файлу изображения
        
    Returns:
        Распознанный текст или None
    """
    if not QR_AVAILABLE:
        return None
    
    try:
        with open(file_path, 'rb') as f:
            image_bytes = f.read()
        return decode_qr_from_bytes(image_bytes)
    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
        return None