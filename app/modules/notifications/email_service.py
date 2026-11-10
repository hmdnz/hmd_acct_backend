import aiosmtplib
from email.message import EmailMessage
from tenacity import retry, stop_after_attempt, wait_fixed
from app.core.config import settings


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def send_email(to_email: str, subject: str, body: str) -> bool:
    if settings.ENVIRONMENT == "development":
        print(f"[DEV MODE] Email to {to_email} | Subject: {subject}\n{body}")
        return True

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USERNAME,
        password=settings.SMTP_PASSWORD,
        start_tls=True,
    )
    return True