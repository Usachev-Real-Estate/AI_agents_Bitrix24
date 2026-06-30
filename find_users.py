import asyncio
from fast_bitrix24 import Bitrix
from src.config import get_settings

async def main():
    bx = Bitrix(get_settings().b24_webhook_url)
    names = ["Агентство Недвижимости", "Вера Волкова", "Артем Ягудин", "Леонид Целиков", "Даниил Юкин", "Дарья Акиншина"]
    users = await bx.get_all("user.get")
    for u in users:
        full_name = f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip()
        if full_name in names or u.get('NAME') in names or u.get('LAST_NAME') in names:
            print(f"ID: {u.get('ID')} - {full_name}")

asyncio.run(main())
