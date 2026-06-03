"""List Bitrix24 departments (step-44)."""
import sys

sys.path.insert(0, "src")
from config import get_settings
from fast_bitrix24 import Bitrix

bx = Bitrix(get_settings().b24_webhook_url)
depts = bx.get_all("department.get")
print("ID | Название")
print("-" * 60)
for d in depts:
    if isinstance(d, dict):
        print(f"{d.get('ID', '?')} | {d.get('NAME', '?')}")
