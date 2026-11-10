from app.core.celery_app import celery_app
from app.modules.notifications.termii_service import send_sms
import asyncio


@celery_app.task(bind=True, max_retries=3)
def send_otp_sms_task(self, phone_number: str, code: str):
    message = f"Your verification code is {code}. It expires in 10 minutes. Do not share this code."
    try:
        asyncio.run(send_sms(phone_number, message))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)