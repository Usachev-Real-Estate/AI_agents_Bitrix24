"""Всё, что крон запускает в контейнере, должно быть внутри образа.

Прогон 01.09: задание #15 запускает QC-агента как
`docker run … python scripts/run_client_state_qc_pilot.py`, а Dockerfile
копировал только `src/`. Контейнер файла не нашёл бы, и отчёт РОПам не
ушёл бы вовсе — «can't open file». Завтрашний прогон в 12:00 был первым
по крону, то есть дефект вышел бы сразу в бой.

Поймать его руками нельзя: ручные прогоны идут через .venv на хосте, где
scripts/ есть всегда, и разницы не видно. Двенадцать остальных заданий
живут в src/ и потому работали — единственный job не из src/ оказался и
единственным сломанным.

Тест сверяет два файла, которые правятся порознь и никогда вместе:
crontab.txt и Dockerfile. Пока их связывает только память, они разъедутся
снова — следующий скрипт вне src/ добавят так же.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# `docker run … <образ> python <путь>` — путь считается от WORKDIR /app.
_IN_CONTAINER = re.compile(r"docker run .*?\bpython3?\s+(\S+\.py)")
# `COPY src/ ./src/` — забираем каталог-источник.
_COPIED = re.compile(r"^COPY\s+(?!--from)(\S+?)/?\s+\./", re.MULTILINE)


def _cron_entrypoints() -> set[str]:
    text = (_ROOT / "crontab.txt").read_text(encoding="utf-8")
    return set(_IN_CONTAINER.findall(text))


def _copied_dirs() -> set[str]:
    text = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    return {d.strip("./") for d in _COPIED.findall(text)}


def test_the_scan_actually_finds_the_cron_jobs():
    """Пустой разбор прошёл бы «успешно», ничего не проверив.

    Регулярка по чужому формату — самое ломкое место теста: строка крона
    поменяется, совпадений станет ноль, и тест начнёт молча одобрять что
    угодно. Проверяем, что он видит и QC-агента, и задания из src/.
    """
    found = _cron_entrypoints()
    assert len(found) >= 10, found
    assert "scripts/run_client_state_qc_pilot.py" in found
    assert any(p.startswith("src/") for p in found)


def test_every_cron_entrypoint_lives_in_the_image():
    copied = _copied_dirs()
    assert copied, "Dockerfile не копирует ни одного каталога"
    for entry in sorted(_cron_entrypoints()):
        top = entry.split("/")[0]
        assert top in copied, (
            f"крон запускает {entry} в контейнере, но каталог {top}/ "
            f"в образ не копируется (COPY есть только для: "
            f"{', '.join(sorted(copied))})"
        )


def test_the_files_the_entrypoints_name_exist():
    """Скопировать каталог мало — файл должен в нём быть."""
    for entry in sorted(_cron_entrypoints()):
        assert (_ROOT / entry).is_file(), f"{entry} нет в репозитории"


def test_dockerignore_does_not_undo_the_copy():
    """.dockerignore сильнее COPY: исключённый каталог не попадёт в образ."""
    ignored = {
        line.strip().strip("/")
        for line in (_ROOT / ".dockerignore").read_text(
            encoding="utf-8",
        ).splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    }
    for entry in sorted(_cron_entrypoints()):
        top = entry.split("/")[0]
        assert top not in ignored, (
            f"{top}/ исключён в .dockerignore, а крон запускает оттуда {entry}"
        )
