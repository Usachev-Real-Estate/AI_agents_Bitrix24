import sqlite3

def main():
    conn = sqlite3.connect('data/violations.db')
    c = conn.cursor()
    
    # Удаляем старые запуски аудита из таблицы audit_runs
    c.execute("SELECT id, run_time FROM audit_runs WHERE date(run_time) = '2026-06-16' ORDER BY run_time")
    runs = c.fetchall()
    
    if len(runs) > 1:
        last_run_id = runs[-1][0]
        c.execute("DELETE FROM audit_runs WHERE date(run_time) = '2026-06-16' AND id != ?", (last_run_id,))
        deleted = c.rowcount
        conn.commit()
        print(f"Удалено {deleted} старых запусков аудита за сегодня.")
    else:
        print("Нет старых запусков аудита для удаления.")
        
    conn.close()

if __name__ == "__main__":
    main()
