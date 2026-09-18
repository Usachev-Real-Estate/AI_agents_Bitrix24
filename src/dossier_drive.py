"""Доставка выгрузки досье в папку Google Drive.

Отдельным файлом от dossier.py нарочно. Библиотеки Google — единственная
часть выгрузки, которой может не оказаться в образе, и если бы их импорт
стоял рядом со сбором, отсутствие пакета роняло бы прогон целиком: данные
собраны, лежат на диске, а процесс упал на строке import и отчитался
ошибкой. Здесь же ошибка ограничена доставкой, а доставка — не сбор.

Авторизация — сервисный аккаунт. Ключ монтируется в контейнер отдельным
томом только на чтение и НЕ лежит в data/: там базы, туда пишет всё
подряд, и ключ доступа к общему диску компании там оказаться не должен.

Папку назначения надо заранее расшарить на почту сервисного аккаунта
(она в поле client_email файла ключа) с правом редактирования — иначе
загрузка вернёт 404 на идентификатор папки, что читается как «папки нет».
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/drive.file",)
# Тип содержимого по расширению. Drive не угадывает его сам, и файл,
# загруженный без типа, скачивается как бинарный мусор.
MIME_BY_SUFFIX = {
    ".jsonl": "application/x-ndjson",
    ".json": "application/json",
    ".md": "text/markdown",
}


def _service() -> Any:
    """Клиент Drive на сервисном аккаунте."""
    from google.oauth2 import service_account  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415

    settings = get_settings()
    credentials = service_account.Credentials.from_service_account_file(
        settings.gdrive_credentials_file, scopes=list(SCOPES),
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _find(service: Any, folder_id: str, name: str) -> str | None:
    """Идентификатор файла с таким именем в папке, если он там уже есть.

    Drive разрешает два файла с одним именем в одной папке. Без этой
    проверки README и повторный прогон того же дня плодили бы двойники, и
    читающий не знал бы, какой из них свежий.
    """
    escaped = name.replace("'", "\\'")
    response = service.files().list(
        q=f"name = '{escaped}' and '{folder_id}' in parents and trashed = false",
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files") or []
    return files[0]["id"] if files else None


def upload(service: Any, folder_id: str, path: Path) -> str:
    """Загрузить или перезаписать один файл. Возвращает id в Drive."""
    from googleapiclient.http import MediaFileUpload  # noqa: PLC0415

    mime = MIME_BY_SUFFIX.get(path.suffix, "application/octet-stream")
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    existing = _find(service, folder_id, path.name)
    if existing:
        result = service.files().update(
            fileId=existing, media_body=media, supportsAllDrives=True,
        ).execute()
    else:
        result = service.files().create(
            body={"name": path.name, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
    return str(result.get("id") or existing or "")


def upload_many(directory: Path, names: list[str]) -> list[str]:
    """Загрузить комплект прогона и подчистить старые выгрузки."""
    settings = get_settings()
    folder_id = settings.gdrive_folder_id
    service = _service()

    uploaded: list[str] = []
    for name in names:
        path = directory / name
        if not path.exists():
            logger.warning("Нет файла для загрузки: %s", name)
            continue
        upload(service, folder_id, path)
        uploaded.append(name)
        logger.info("В Drive загружен %s", name)

    prune(service, folder_id, settings.dossier_keep_files)
    return uploaded


def prune(service: Any, folder_id: str, keep: int) -> list[str]:
    """Оставить в папке последние `keep` выгрузок.

    Комплект прогона удаляется целиком — строки, мета и очередь вместе.
    Строки без своей меты это выгрузка, о которой ничего не известно: ни
    сколько карточек не собралось, ни сколько разговоров не прочиталось.
    """
    if keep <= 0:
        return []
    response = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files") or []

    # Список имён под удаление считает dossier.doomed_names — там же, где
    # он считается для локального каталога. Две копии этой логики означали
    # бы, что однажды они разойдутся, и в Drive останется не то, что на
    # диске, причём узнается это в момент, когда файл понадобится.
    from dossier import doomed_names  # noqa: PLC0415

    doomed = set(doomed_names([f["name"] for f in files], keep))
    removed: list[str] = []
    for item in files:
        if item["name"] not in doomed:
            continue
        try:
            service.files().delete(
                fileId=item["id"], supportsAllDrives=True,
            ).execute()
            removed.append(item["name"])
        except Exception:  # noqa: BLE001 — уборка не важнее доставки
            logger.warning("Не удалён из Drive %s", item["name"])
    if removed:
        logger.info("Из Drive удалено старых файлов: %d", len(removed))
    return removed
