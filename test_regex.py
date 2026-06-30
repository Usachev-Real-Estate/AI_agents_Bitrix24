from src.chat_poller import find_command

messages = [
    {"id": 1, "text": "проверь 40"},
    {"id": 2, "text": "Проверка 40"},
    {"id": 3, "text": "!score 40"},
    {"id": 4, "text": "/score 40"},
    {"id": 5, "text": "проверь 40 и еще что-то"},
    {"id": 6, "text": "просто текст"}
]

print(find_command(messages))
