"""Именные нормы квартала: сопоставить список с порталом и записать в витрину.

План агентства на квартал — поимённый: у каждого брокера своя ступень
(3,5 / 4,5 / 5,5 млн), а новички норму не несут вовсе. Значит план нельзя
вывести из штата, его надо привязать к конкретным учётным записям Битрикса.

Сопоставление по имени — единственный доступный путь и одновременно самое
опасное место: однофамильцы, разный порядок слов («Трутаева Светлана» против
«Светлана Трутаева»), «ё» вместо «е», уволенные тёзки. Поэтому скрипт по
умолчанию ничего не пишет: он показывает, кого нашёл, кого не нашёл и где
выбор неоднозначен, — а запись включается отдельным флагом, после того как
человек посмотрел глазами.

Молчаливой недостачи тут быть не должно: ненайденная фамилия означает, что
её норма не попадёт в план, и отдел будет выглядеть лучше, чем он есть.

Формат файла — две колонки через табуляцию, сумма в миллионах рублей:

    Алексей Закурдаев<TAB>3.5
    Юлия Шпырная<TAB>3.5

Запуск:

    python scripts/plan_norms.py data/plan-2026Q3.tsv 2026-Q3
    python scripts/plan_norms.py data/plan-2026Q3.tsv 2026-Q3 --write
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for extra in (_SRC_DIR, _SRC_DIR / "analytics"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from config import get_settings  # noqa: E402
from schema import analytics_session, get_connection, init_analytics_db  # noqa: E402

logger = logging.getLogger(__name__)

LINE = "─" * 72
MILLION = 1_000_000


def name_key(text: str) -> tuple[str, ...]:
    """Имя как множество слов: порядок и «ё» не должны решать судьбу нормы.

    В портале встречаются и «Шпырная Юлия», и «Юлия Шпырная», а в списке от
    агентства — то одно, то другое. Сравнение по строке целиком отбросило бы
    половину списка, и отделы выглядели бы лучше, чем они есть.
    """
    cleaned = (text or "").lower().replace("ё", "е").replace(",", " ")
    return tuple(sorted(part for part in cleaned.split() if part))


def read_list(path: Path) -> list[tuple[str, float]]:
    """Разобрать файл плана. Строка без суммы — ошибка, а не пропуск."""
    rows: list[tuple[str, float]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.replace(";", "\t").split("\t") if part.strip()]
        if len(parts) < 2:
            raise ValueError(f"{path}:{number}: нужны имя и сумма через табуляцию — {raw!r}")
        try:
            amount = float(parts[1].replace(",", ".").replace(" ", ""))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: не число — {parts[1]!r}") from exc
        rows.append((parts[0], amount))
    return rows


def match(conn, wanted: list[tuple[str, float]]) -> dict[str, list]:
    """Сопоставить список с активными пользователями витрины."""
    users = [
        dict(row) for row in conn.execute(
            "SELECT user_id, name, last_name, department_id, department_name "
            "FROM dim_user WHERE is_active = 1"
        ).fetchall()
    ]
    by_full: dict[tuple[str, ...], list[dict]] = {}
    by_last: dict[str, list[dict]] = {}
    for user in users:
        by_full.setdefault(name_key(user["name"]), []).append(user)
        surname = (user["last_name"] or "").strip().lower().replace("ё", "е")
        if surname:
            by_last.setdefault(surname, []).append(user)

    matched, missing, ambiguous = [], [], []
    for label, amount in wanted:
        key = name_key(label)
        found = by_full.get(key, [])
        how = "полное имя"
        if not found:
            # Запасной путь — фамилия. Он же самый опасный, поэтому
            # единственное совпадение принимается, а два уходят в спорные.
            for word in key:
                candidates = by_last.get(word, [])
                if candidates:
                    found, how = candidates, "фамилия"
                    break
        if not found:
            missing.append((label, amount))
        elif len(found) > 1:
            ambiguous.append((label, amount, found))
        else:
            matched.append((label, amount, found[0], how))

    listed = {user["user_id"] for _, _, user, _ in matched}
    without_norm = [
        user for user in users
        if user["user_id"] not in listed
        and user["department_id"] in _sales_departments()
    ]
    return {"matched": matched, "missing": missing,
            "ambiguous": ambiguous, "without_norm": without_norm}


def _sales_departments() -> tuple[int, ...]:
    try:
        return tuple(int(value) for value in get_settings().owner_sales_dept_ids)
    except Exception:  # pragma: no cover
        return ()


def report(result: dict[str, list], period_code: str) -> float:
    """Напечатать разбор и вернуть сумму сопоставленных норм в рублях."""
    matched = result["matched"]
    total = sum(amount for _, amount, _, _ in matched) * MILLION

    print(f"\n{LINE}\nСопоставлено: {len(matched)}\n{LINE}")
    for label, amount, user, how in sorted(matched, key=lambda row: row[2]["department_name"]):
        mark = "" if how == "полное имя" else "  ← по фамилии, проверьте"
        print(f"  id={user['user_id']:<5} {label:<24} {amount:>4} млн  "
              f"«{user['department_name']}»{mark}")

    if result["missing"]:
        print(f"\n{LINE}\nНЕ НАЙДЕНО: {len(result['missing'])}\n{LINE}")
        for label, amount in result["missing"]:
            print(f"  {label:<24} {amount:>4} млн — активного пользователя с таким именем нет")
        print("\n  Их нормы в план НЕ попадут, и отдел будет выглядеть лучше,")
        print("  чем он есть. Проверьте написание или активность учётки.")

    if result["ambiguous"]:
        print(f"\n{LINE}\nНЕОДНОЗНАЧНО: {len(result['ambiguous'])}\n{LINE}")
        for label, amount, found in result["ambiguous"]:
            print(f"  {label} ({amount} млн) — подходят несколько:")
            for user in found:
                print(f"      id={user['user_id']:<5} {user['name']:<26} "
                      f"«{user['department_name']}»")
        print("\n  Угадывать нельзя: не та учётка означает норму не тому отделу.")

    if result["without_norm"]:
        print(f"\n{LINE}\nБЕЗ НОРМЫ (в отделах продаж, но не в списке): "
              f"{len(result['without_norm'])}\n{LINE}")
        for user in sorted(result["without_norm"], key=lambda u: u["department_name"]):
            print(f"  id={user['user_id']:<5} {user['name']:<26} «{user['department_name']}»")
        print("\n  Это новички и руководители вне плана. Они остаются в отделе,")
        print("  но плана не несут — так и будет показано на экране.")

    print(f"\n{LINE}")
    print(f"ПЛАН {period_code}: {total / MILLION:.1f} млн ₽ по {len(matched)} строкам")
    if result["missing"] or result["ambiguous"]:
        lost = sum(a for _, a in result["missing"])
        lost += sum(a for _, a, _ in result["ambiguous"])
        print(f"Не учтено из-за несопоставленных строк: {lost:.1f} млн ₽")
    print(LINE)
    return total


def write(period_code: str, matched: list, metric: str = "commission") -> int:
    """Записать нормы. Период переписывается целиком: список — источник правды."""
    now = datetime.now(timezone.utc).isoformat()
    with analytics_session() as conn:
        conn.execute(
            "DELETE FROM plan_norm WHERE period_code = ? AND scope_kind = 'user' "
            "AND metric = ?",
            (period_code, metric),
        )
        conn.executemany(
            "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
            " basis, amount, source, updated_at)"
            " VALUES (?, 'user', ?, ?, 'absolute', ?, 'list', ?)",
            [(period_code, user["user_id"], metric, amount * MILLION, now)
             for _, amount, user, _ in matched],
        )
    return len(matched)


def main() -> int:
    parser = argparse.ArgumentParser(description="Именные нормы квартала")
    parser.add_argument("path", type=Path, help="файл со списком: имя<TAB>млн")
    parser.add_argument("period", help="код периода, например 2026-Q3")
    parser.add_argument("--write", action="store_true",
                        help="записать нормы в витрину (по умолчанию только показать)")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"Файл не найден: {args.path}")
        return 1

    wanted = read_list(args.path)
    init_analytics_db()
    conn = get_connection(get_settings().analytics_db_path, readonly=True)
    try:
        result = match(conn, wanted)
    finally:
        conn.close()

    report(result, args.period)

    if not args.write:
        print("\nЭто разбор. Чтобы записать нормы, повторите запуск с --write.")
        return 0
    if result["missing"] or result["ambiguous"]:
        print("\nЗапись отменена: сначала разберитесь с ненайденными и спорными.")
        print("Норма, не привязанная к человеку, тихо теряется из плана.")
        return 2
    written = write(args.period, result["matched"])
    print(f"\nЗаписано норм: {written}. Период {args.period} перезаписан целиком.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
