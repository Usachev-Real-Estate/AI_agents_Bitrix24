import asyncio
from fast_bitrix24 import Bitrix
from src.config import get_settings
from src.notify import send_chat_message_chunked

async def main():
    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)
    
    report = """b24-ai-auditor — Скоринг брокера
Дата: 16.06.2026

👤 Тарасова Ирина (Кретов)
═══════════════════════════════

📋 НАРУШЕНИЯ CRM (за 30 дней)
  Лиды: 2
    • lead_rule_1: 2 раз(а)

🏠 НОВЫЕ СОБСТВЕННИКИ (за 30 дней)
  Добавлено: 1
  KPI (10/мес): ❌ не выполнено (10%)

═══════════════════════════════
📊 ОЦЕНКА: 🟡 СРЕДНЕ
═══════════════════════════════
🔴 Нарушения: 2 (хорошо)
🟢 Собственники: 1 (плохо)
⚠️ Рекомендации:
  • Обратить внимание на скорость квалификации лидов (статус «новый» > 2 часов)
  • Увеличить количество добавляемых собственников (осталось 9 до kpi)"""

    send_chat_message_chunked(settings.report_chat_id, report)
    print("Sent!")

asyncio.run(main())
