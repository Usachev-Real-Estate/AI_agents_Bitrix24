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
    store.create_user(args.username, _read_password(), args.name or "")
    print(f"Пользователь {args.username} создан")
    return 0


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
        print("Пользователей нет. Заведите: manage.py adduser <логин>")
        return 0
    print(f"{'логин':<20} {'активен':<9} {'последний вход':<20} имя")
    for user in users:
        print(
            f"{user['username']:<20} {'да' if user['is_active'] else 'нет':<9} "
            f"{(user['last_login_at'] or '—')[:19]:<20} {user['display_name']}"
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
        if name == "sessions":
            command.add_argument("username", nargs="?", default=None)
        command.set_defaults(handler=handler)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
