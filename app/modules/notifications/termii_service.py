import httpx
from tenacity import retry, stop_after_attempt, wait_fixed
from app.core.config import settings

TERMII_BASE_URL = "https://api.ng.termii.com/api/sms/send"


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def send_sms(phone_number: str, message: str) -> bool:
    if settings.ENVIRONMENT == "development":
        print(f"[DEV MODE] SMS to {phone_number}: {message}")  # shows up in your terminal/logs
        return True

    async with httpx.AsyncClient(timeout=10) as client:
        payload = {
            "to": phone_number,
            "from": settings.TERMII_SENDER_ID,
            "sms": message,
            "type": "plain",
            "channel": "generic",
            "api_key": settings.TERMII_API_KEY,
        }
        response = await client.post(TERMII_BASE_URL, json=payload)
        response.raise_for_status()
        return True