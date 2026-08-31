"""Управление доступом к дашборду.

    python src/web/manage.py adduser ivanov
    python src/web/manage.py passwd ivanov
    python src/web/manage.py list
    python src/web/manage.py revoke ivanov
    python src/web/manage.py gen-secret

Пароль всегда читается из потока ввода, никогда не берётся аргументом
командной строки: аргумент осел бы в истории шелла и был бы виден в `ps`
любому пользователю сервера.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

if __package__ in (None, ""):
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "analytics")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

import store  # noqa: E402

MIN_PASSWORD_LEN = 10


def _read_password(confirm: bool = True) -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Пароль: ")
        if confirm and password != getpass.getpass("Повторите пароль: "):
            raise SystemExit("Пароли не совпали")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(f"Пароль короче {MIN_PASSWORD_LEN} символов")
    return password


def cmd_adduser(args: argparse.Namespace) -> int:
    store.init_store()
    departments = _departments_arg(args)
    store.create_user(
        args.username, _read_password(), args.name or "",
        role=args.role, department_ids=departments,
    )
    print(f"Пользователь {args.username} создан, роль {args.role}, "
          f"{_scope_text(args.role, departments)}")
    return 0


def cmd_setrole(args: argparse.Namespace) -> int:
    store.init_store()
    departments = _departments_arg(args)
    if not store.set_user_role(args.username, args.role, departments):
        raise SystemExit(f"Нет такого пользователя: {args.username}")
    print(f"{args.username}: роль {args.role}, {_scope_text(args.role, departments)}")
    return 0


def cmd_departments(_: argparse.Namespace) -> int:
    """Отделы из витрины — чтобы админ знал, какие ID назначать РОПам."""
    import analytics  # noqa: F401  — кладёт src/analytics на sys.path
    import metrics
    from scope import Scope, scoped_session

    with scoped_session(Scope.everything()) as conn:
        rows = metrics.departments_options(conn)
    if not rows:
        print("В витрине нет отделов. Сначала запустите ETL.")
        return 0
    print(f"{'ID':<8} {'сделок':<8} отдел")
    for row in rows:
        print(f"{row['department_id']:<8} {row['deals']:<8} {row['name']}")
    return 0


def _departments_arg(args: argparse.Namespace) -> list[int]:
    departments = list(args.department or [])
    if args.role == store.ROLE_ROP and not departments:
        # Учётка без отделов не видит ничего. Это безопасно, но чаще всего
        # означает забытый флаг, а не намерение — предупреждаем вслух.
        print("Внимание: отделы не заданы — этот пользователь не увидит НИЧЕГО. "
              "Список отделов: manage.py departments")
    if args.role == store.ROLE_ADMIN and departments:
        print("Внимание: у администратора отделы не используются — он видит всё.")
    return departments


def _scope_text(role: str, departments: list[int]) -> str:
    if role == store.ROLE_ADMIN:
        return "видит всю компанию"
    if not departments:
        return "не видит ничего (отделы не назначены)"
    return f"видит отделы {', '.join(str(d) for d in departments)}"


def cmd_passwd(args: argparse.Namespace) -> int:
    store.init_store()
    existing = {u["username"] for u in store.list_users()}
    if args.username.strip().lower() not in existing:
        raise SystemExit(f"Нет такого пользователя: {args.username}")
    store.create_user(args.username, _read_password())
    revoked = store.revoke_all_sessions(args.username)
    # Смена пароля обязана выкидывать старые сессии: иначе тот, ради кого
    # пароль меняли, продолжит сидеть в дашборде по своей куке.
    print(f"Пароль обновлён, отозвано сессий: {revoked}")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    store.init_store()
    users = store.list_users()
    if not users:
        print("Пользователей нет. Заведите: manage.py adduser <логин> --role admin")
        return 0
    print(f"{'логин':<18} {'роль':<7} {'активен':<9} {'видит':<28} имя")
    for user in users:
        print(
            f"{user['username']:<18} {user['role']:<7} "
            f"{'да' if user['is_active'] else 'нет':<9} "
            f"{_scope_text(user['role'], user['department_ids']):<28} "
            f"{user['display_name']}"
        )
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    store.init_store()
    if not store.set_user_active(args.username, False):
        raise SystemExit(f"Нет такого пользователя: {args.username}")
    revoked = store.revoke_all_sessions(args.username)
    print(f"Доступ закрыт, отозвано сессий: {revoked}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    store.init_store()
    if not store.set_user_active(args.username, True):
        raise SystemExit(f"Нет такого пользователя: {args.username}")
    print("Доступ открыт")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    store.init_store()
    rows = store.list_sessions(args.username)
    if not rows:
        print("Активных сессий нет")
        return 0
    print(f"{'логин':<20} {'создана':<20} {'активность':<20} {'адрес':<16} статус")
    for row in rows:
        status = "отозвана" if row["revoked_at"] else "активна"
        print(
            f"{row['username']:<20} {row['created_at'][:19]:<20} "
            f"{row['last_seen_at'][:19]:<20} {row['ip']:<16} {status}"
        )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    store.init_store()
    print(f"Отозвано сессий: {store.revoke_all_sessions(args.username)}")
    return 0


def cmd_gen_secret(_: argparse.Namespace) -> int:
    print(f"DASHBOARD_SECRET_KEY={secrets.token_urlsafe(48)}")
    return 0


def cmd_purge(_: argparse.Namespace) -> int:
    store.init_store()
    store.purge_expired_sessions()
    store.purge_old_attempts()
    print("Протухшие сессии и старые попытки входа удалены")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление доступом к дашборду")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, needs_user, help_text in (
        ("adduser", cmd_adduser, True, "завести пользователя"),
        ("setrole", cmd_setrole, True, "сменить роль и отделы"),
        ("departments", cmd_departments, False, "список отделов из витрины"),
        ("passwd", cmd_passwd, True, "сменить пароль и отозвать сессии"),
        ("disable", cmd_disable, True, "закрыть доступ"),
        ("enable", cmd_enable, True, "открыть доступ"),
        ("revoke", cmd_revoke, True, "отозвать все сессии пользователя"),
        ("list", cmd_list, False, "список пользователей"),
        ("sessions", cmd_sessions, False, "список сессий"),
        ("gen-secret", cmd_gen_secret, False, "сгенерировать DASHBOARD_SECRET_KEY"),
        ("purge", cmd_purge, False, "почистить протухшие сессии"),
    ):
        command = sub.add_parser(name, help=help_text)
        if needs_user:
            command.add_argument("username")
        if name == "adduser":
            command.add_argument("--name", default="", help="отображаемое имя")
        if name in ("adduser", "setrole"):
            command.add_argument(
                "--role", choices=store.ROLES, default=store.ROLE_ROP,
                help="admin — вся компания; rop — только свои отделы (по умолчанию)")
            command.add_argument(
                "--department", type=int, action="append", metavar="ID",
                help="отдел РОПа; можно указать несколько раз. "
                     "ID смотреть в manage.py departments")
        if name == "sessions":
            command.add_argument("username", nargs="?", default=None)
        command.set_defaults(handler=handler)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
