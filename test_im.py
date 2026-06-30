import asyncio
from fast_bitrix24 import Bitrix
from src.config import get_settings

async def main():
    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)
    res = await bx.call("im.dialog.messages.get", {"DIALOG_ID": "chat22358", "LIMIT": 2})
    print(type(res))
    if isinstance(res, dict):
        print(res.keys())
    print(res)

asyncio.run(main())
