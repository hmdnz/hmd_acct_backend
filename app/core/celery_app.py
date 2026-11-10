from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "marketplace",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,          # don't lose tasks if worker crashes mid-task
    worker_prefetch_multiplier=1,  # important for payment/OTP reliability
    task_default_retry_delay=10,
)