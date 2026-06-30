import asyncio
from fast_bitrix24 import Bitrix
from src.config import get_settings

async def main():
    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)
    
    # Получаем все подразделения
    depts = await bx.get_all("department.get")
    
    for d in depts:
        print(f"ID: {d.get('ID')} - Название: {d.get('NAME')}")

asyncio.run(main())
