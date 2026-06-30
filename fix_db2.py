import sqlite3

def main():
    conn = sqlite3.connect('data/audit.db')
    c = conn.cursor()
    
    # Посмотрим структуру БД
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = c.fetchall()
    print(f"Таблицы: {tables}")
    
    # Посмотрим нарушения за сегодня
    c.execute("SELECT COUNT(*), MIN(detected_at), MAX(detected_at) FROM violations WHERE date(detected_at) = '2026-06-16'")
    stats = c.fetchone()
    print(f"Всего нарушений за сегодня: {stats[0]}, с {stats[1]} по {stats[2]}")
    
    # Посмотрим уникальные метки времени за сегодня
    c.execute("SELECT DISTINCT detected_at, COUNT(*) FROM violations WHERE date(detected_at) = '2026-06-16' GROUP BY detected_at ORDER BY detected_at")
    times = c.fetchall()
    print("\nГруппы нарушений по времени:")
    for t in times:
        print(f"Время: {t[0]}, Количество: {t[1]}")
        
    if len(times) <= 1:
        print("\nНет предыдущих отчетов за сегодня для удаления.")
        return
        
    # Оставляем только последнее время
    last_time = times[-1][0]
    print(f"\nОставляем нарушения от: {last_time}")
    
    # Удаляем все нарушения за сегодня, кроме последнего времени
    c.execute("DELETE FROM violations WHERE date(detected_at) = '2026-06-16' AND detected_at != ?", (last_time,))
    deleted = c.rowcount
    
    conn.commit()
    print(f"Успешно удалено {deleted} старых нарушений за сегодня!")
    
    conn.close()

if __name__ == "__main__":
    main()
