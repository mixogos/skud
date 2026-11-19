"""Служебные команды.

    python -m app.cli create-admin [логин]      создать первого администратора
    python -m app.cli reset-password <логин>    сбросить пароль пользователя
    python -m app.cli download-models           скачать модели распознавания лиц
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from . import config, models, security
from .bootstrap import create_superadmin, init_db
from .db import SessionLocal


def _ask_password() -> str:
    while True:
        first = getpass.getpass("Пароль: ")
        if len(first) < config.MIN_PASSWORD_LENGTH:
            print(f"Пароль должен быть не короче {config.MIN_PASSWORD_LENGTH} символов.")
            continue
        if first != getpass.getpass("Повторите пароль: "):
            print("Пароли не совпадают.")
            continue
        return first


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="создать первого администратора")
    p.add_argument("name", nargs="?", default="admin")
    p = sub.add_parser("reset-password", help="сбросить пароль пользователя")
    p.add_argument("name")
    sub.add_parser("download-models", help="скачать модели распознавания лиц")
    args = parser.parse_args()

    if args.cmd == "download-models":
        from .recognition import model_store

        model_store.download()
        model_store.download_liveness()
        model_store.download_attributes()
        model_store.download_people()
        return 0

    init_db()
    with SessionLocal() as db:
        if args.cmd == "create-admin":
            try:
                password = _ask_password()
                user = create_superadmin(db, args.name, password)
            except ValueError as exc:
                print(exc)
                return 1
            print(f"Администратор «{user.name}» создан.")
        else:
            user = db.scalar(select(models.User).where(models.User.name == args.name))
            if user is None:
                print("Пользователь не найден.")
                return 1
            user.password_hash = security.hash_password(_ask_password())
            user.failed_logins, user.locked_until = 0, None
            for s in list(user.sessions):
                db.delete(s)
            db.commit()
            print(f"Пароль пользователя «{user.name}» изменён, все его сессии закрыты.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
