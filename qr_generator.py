"""Генератор QR-кодов для регистрации"""
import qrcode
import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class QRGenerator:
    """Генератор QR-кодов"""
    
    @staticmethod
    def generate_qr_bytes(data: str, size: int = 300) -> Optional[bytes]:
        """
        Генерация QR-кода в виде байтов
        
        Args:
            data: Данные для кодирования
            size: Размер изображения в пикселях
            
        Returns:
            Байты PNG изображения или None
        """
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=10,
                border=2,
            )
            qr.add_data(data)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Изменяем размер
            img = img.resize((size, size))
            
            # Сохраняем в байты
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            
            logger.info(f"QR code generated for data: {data[:20]}...")
            return img_bytes.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to generate QR code: {e}")
            return None
        
