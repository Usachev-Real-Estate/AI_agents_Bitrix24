import sqlite3
from datetime import datetime

def main():
    conn = sqlite3.connect('data/audit.db')
    c = conn.cursor()
    
    # Посмотрим, какие есть запуски за сегодня (16 июня)
    c.execute("SELECT id, run_time, violations_count FROM audit_runs WHERE date(run_time) = '2026-06-16' ORDER BY run_time ASC")
    runs = c.fetchall()
    
    print("Запуски аудита за 16 июня:")
    for r in runs:
        print(f"Run ID: {r[0]}, Time: {r[1]}, Violations: {r[2]}")
        
    if not runs:
        print("Запусков за сегодня не найдено.")
        return
        
    # Последний запуск
    last_run_id = runs[-1][0]
    print(f"\nПоследний запуск: ID {last_run_id}")
    
    # Найдем все предыдущие запуски за сегодня
    previous_runs = [r[0] for r in runs[:-1]]
    
    if not previous_runs:
        print("Предыдущих запусков за сегодня нет. Удалять нечего.")
        return
        
    print(f"Предыдущие запуски для удаления: {previous_runs}")
    
    # Считаем сколько нарушений будет удалено
    placeholders = ','.join('?' * len(previous_runs))
    c.execute(f"SELECT COUNT(*) FROM violations WHERE run_id IN ({placeholders})", previous_runs)
    count = c.fetchone()[0]
    
    print(f"Будет удалено {count} нарушений.")
    
    # Удаляем нарушения
    c.execute(f"DELETE FROM violations WHERE run_id IN ({placeholders})", previous_runs)
    
    # Удаляем сами запуски
    c.execute(f"DELETE FROM audit_runs WHERE id IN ({placeholders})", previous_runs)
    
    conn.commit()
    print("Успешно удалено!")
    
    conn.close()

if __name__ == "__main__":
    main()
