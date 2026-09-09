"""Ограничение видимости данных на уровне соединения с витриной.

Разграничение доступа можно сделать двумя способами, и разница между ними
принципиальная.

Слабый способ — добавлять фильтр по отделу в каждый запрос. Тогда защита
держится на памяти разработчика: новый запрос, написанный через полгода без
фильтра, молча покажет РОПу всю компанию. Проверить такое можно только
перечитыванием всех запросов, и с каждым новым оно устаревает.

Сильный способ, выбранный здесь: область видимости задаётся ОДИН РАЗ при
открытии соединения. Поверх таблиц создаются временные представления
(v_deal, v_lead, v_stage_event, v_user), уже суженные до разрешённых
отделов, а запросы метрик обращаются только к ним. Новый запрос получает
ограничение автоматически — забыть его невозможно, потому что забывать
нечего. Временные объекты живут в отдельной базе SQLite, поэтому создаются
даже на соединении, открытом строго на чтение.

Важное свойство: конструкция закрывается при сбое, а не открывается. РОП без
единого отдела (недонастроенная учётка) не видит НИЧЕГО. Признак «видеть всё»
— отдельный флаг, который выставляется только для роли администратора, а не
выводится из пустого списка отделов.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

from schema import analytics_session

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_ROP = "rop"
ROLES = (ROLE_ADMIN, ROLE_ROP)


@dataclass(frozen=True)
class Scope:
    """Что разрешено видеть в этом соединении."""

    unrestricted: bool
    department_ids: tuple[int, ...] = ()

    @classmethod
    def everything(cls) -> "Scope":
        """Доступ ко всей компании. Только для роли администратора."""
        return cls(unrestricted=True)

    @classmethod
    def departments(cls, department_ids: Sequence[int]) -> "Scope":
        """Доступ к перечисленным отделам. Пустой список — не видно ничего."""
        cleaned = tuple(sorted({int(d) for d in department_ids if d is not None}))
        return cls(unrestricted=False, department_ids=cleaned)

    @classmethod
    def for_user(cls, user: dict | None) -> "Scope":
        """Область видимости по учётной записи.

        Отсутствие пользователя или нераспознанная роль трактуются как
        «ничего не видно»: единственный способ получить полный доступ —
        явная роль администратора.
        """
        if not user:
            return cls.departments(())
        if user.get("role") == ROLE_ADMIN:
            return cls.everything()
        return cls.departments(user.get("department_ids") or ())

    @property
    def sees_nothing(self) -> bool:
        return not self.unrestricted and not self.department_ids

    def describe(self) -> str:
        if self.unrestricted:
            return "вся компания"
        if not self.department_ids:
            return "ничего (отделы не назначены)"
        return f"отделы {', '.join(str(d) for d in self.department_ids)}"


# Представления, через которые метрики видят данные. Прямое обращение к
# fact_deal / fact_lead / fact_stage_event / dim_user из метрик запрещено и
# проверяется тестом: такой запрос обошёл бы ограничение.
_VIEW_DDL = (
    """
    CREATE TEMP VIEW v_deal AS
    SELECT d.* FROM fact_deal d
    WHERE d.is_deleted = 0
      AND ((SELECT unrestricted FROM scope_flag) = 1
           OR d.deal_id IN (SELECT deal_id FROM scope_deal))
    """,
    """
    CREATE TEMP VIEW v_lead AS
    SELECT l.* FROM fact_lead l
    WHERE l.is_deleted = 0
      AND ((SELECT unrestricted FROM scope_flag) = 1
           OR l.lead_id IN (SELECT lead_id FROM scope_lead))
    """,
    """
    CREATE TEMP VIEW v_stage_event AS
    SELECT e.* FROM fact_stage_event e
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR (e.entity_type = 'deal' AND e.entity_id IN (SELECT deal_id FROM scope_deal))
       OR (e.entity_type = 'lead' AND e.entity_id IN (SELECT lead_id FROM scope_lead))
    """,
    # Отдел человека берётся из user_home, а не из карточки. В портале РОП
    # сплошь и рядом числится не там, где работает: руководитель отдела
    # «Волкова» сидит в служебном подразделении «Битрикс». По карточке её
    # РОП не увидел бы в своём составе собственного руководителя, и отдел
    # так и остался бы «без РОПа» — ровно та поломка, которую ростер и
    # заводился чинить.
    #
    # Условие ЗАМЕЩАЮЩЕЕ, а не дополнительное: человек виден одному отделу —
    # тому, которому его приписали. Дополнительное показывало бы его и
    # старому отделу тоже, а тот, не видя строки ростера, посчитал бы его
    # своим и ждал бы от него денег, которые уже уходят в новый отдел.
    #
    # Утечкой это не является: строки ростера пишет администратор, и
    # приписать человека к отделу — сознательное решение о том, чей он.
    """
    CREATE TEMP VIEW v_user AS
    SELECT u.* FROM dim_user u
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR (SELECT h.department_id FROM user_home h WHERE h.user_id = u.user_id)
           IN (SELECT department_id FROM scope_department)
    """,
    # Единственное представление, СОЗНАТЕЛЬНО не суженное по отделу, — норма
    # времени на стадии по всей воронке.
    #
    # Зачем: «зависшая сделка» — это сделка, стоящая дольше 75-го перцентиля
    # своей стадии. Если считать перцентиль внутри отдела, медленный отдел
    # сравнивается сам с собой и никогда не выглядит медленным, а главное —
    # одна и та же карточка оказывается «зависшей» для директора и
    # нормальной для РОПа. Числа, спорящие между собой на одной странице,
    # стоят доверия ко всему дашборду.
    #
    # Почему это не утечка: здесь нет ни идентификаторов сущностей, ни
    # названий, ни сумм, ни ответственных — только длительность и стадия.
    # По такой строке нельзя узнать ни одной чужой сделки; это отраслевой
    # ориентир «Подбор обычно занимает столько-то», а не данные.
    #
    # Только сделки. События статусов лидов ETL пишет с category_id = 0 —
    # тем же номером, что у воронки «Продавцы», — а коды у них совпадают
    # (статус лида NEW и стадия сделки NEW). Без фильтра по типу норма
    # стадии «Назначение встречи» складывалась из времени жизни лидов:
    # сорок лидов по часу перевешивали сделки, норма падала до часа, и в
    # «зависшие» попадала каждая живая карточка воронки.
    """
    CREATE TEMP VIEW v_stage_norm AS
    SELECT category_id, stage_id, duration_sec
    FROM fact_stage_event
    WHERE duration_sec IS NOT NULL AND entity_type = 'deal'
    """,
    # Нормы плана. Строка компании видна ТОЛЬКО администратору: РОП не должен
    # узнавать цель всей компании из своего экрана. Поэтому здесь не
    # «показать всё, кроме чужого», а «показать только своё», и ограниченное
    # соединение не видит company-строку ни при каких условиях.
    """
    CREATE TEMP VIEW v_plan_norm AS
    SELECT n.* FROM plan_norm n
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR (n.scope_kind = 'department'
           AND n.scope_id IN (SELECT department_id FROM scope_department))
       OR (n.scope_kind = 'user'
           AND (SELECT h.department_id FROM user_home h WHERE h.user_id = n.scope_id)
               IN (SELECT department_id FROM scope_department))
    """,
    # Ручной ростер планового состава. Строка адресная — в ней конкретный
    # человек, — поэтому сужается по отделу. Отдел берётся из самой строки,
    # если он там задан: именно так чинится РОП, административно
    # приписанный к чужому подразделению, и его строка обязана быть видна
    # РОПу того отдела, за который он отвечает, а не того, где он числится.
    """
    CREATE TEMP VIEW v_plan_roster AS
    SELECT r.* FROM plan_roster r
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR (SELECT h.department_id FROM user_home h WHERE h.user_id = r.user_id)
           IN (SELECT department_id FROM scope_department)
    """,
    # Действия сужаются по ОТВЕТСТВЕННОМУ, а не по владельцу карточки.
    #
    # Владельцем звонка сплошь и рядом оказывается контакт, а не сделка, и
    # отдела у контакта нет вовсе: привязать действие к отделу через владельца
    # значит потерять большую часть звонков — на боевом портале их на
    # контактах больше, чем на сделках. Ответственный же есть у каждого
    # действия, и это ровно тот человек, чью работу считают.
    """
    CREATE TEMP VIEW v_activity AS
    SELECT a.* FROM fact_activity a
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR (SELECT h.department_id FROM user_home h
            WHERE h.user_id = a.responsible_id)
           IN (SELECT department_id FROM scope_department)
    """,
    # Комментарий сужается по КАРТОЧКЕ, а не по автору. У действия
    # ответственный — тот, чью работу считают, а комментарий пишет кто
    # угодно: колл-центр, РОП, коллега по просьбе. Привязка к автору
    # спрятала бы от РОПа половину написанного по его же сделкам, причём
    # именно то, что писали не его люди — а «что с клиентом» в такой записи
    # часто и стоит.
    """
    CREATE TEMP VIEW v_comment AS
    SELECT c.* FROM fact_comment c
    WHERE c.entity_type = 'deal'
      AND c.entity_id IN (SELECT deal_id FROM v_deal)
    """,
    # Прочитанное сужается так же, как комментарий, из которого выросло, —
    # по карточке.
    """
    CREATE TEMP VIEW v_comment_read AS
    SELECT r.* FROM fact_comment_read r
    WHERE r.entity_type = 'deal'
      AND r.entity_id IN (SELECT deal_id FROM v_deal)
    """,
    # Периоды плана не сужаются: это календарь, в нём нет ни людей, ни денег.
    """
    CREATE TEMP VIEW v_plan_period AS SELECT * FROM plan_period
    """,
)


def apply_scope(conn, scope: Scope) -> None:
    """Создать на соединении представления, суженные до области видимости.

    Разрешённые сделки и лиды материализуются в временные таблицы с
    первичным ключом: подзапрос EXISTS на каждую строку событий стадий
    (их десятки тысяч) обходился бы заметно дороже, а список сущностей
    отдела меняться внутри одного запроса всё равно не может.
    """
    conn.execute("CREATE TEMP TABLE scope_flag (unrestricted INTEGER NOT NULL)")
    conn.execute(
        "INSERT INTO scope_flag(unrestricted) VALUES (?)",
        (1 if scope.unrestricted else 0,),
    )
    conn.execute("CREATE TEMP TABLE scope_department (department_id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE scope_deal (deal_id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE scope_lead (lead_id INTEGER PRIMARY KEY)")
    _user_home(conn)

    if not scope.unrestricted and scope.department_ids:
        placeholders = ",".join("?" * len(scope.department_ids))
        conn.executemany(
            "INSERT INTO scope_department(department_id) VALUES (?)",
            [(d,) for d in scope.department_ids],
        )
        # Отдел сделки — это отдел её ТЕКУЩЕГО ответственного: истории
        # назначений Bitrix не отдаёт. Карточка без ответственного не
        # принадлежит ни одному отделу и РОПам не видна; в общем доступе
        # она есть, и страница «Качество данных» такие карточки считает.
        #
        # Отдел берётся из user_home, то есть с учётом ростера. Иначе
        # состав отдела и его деньги расходились бы: человек, приписанный
        # ростером, попадал бы в план отдела, а его сделки оставались бы
        # видны только прежнему. У нового отдела выполнение занижено, у
        # прежнего завышено, и оба числа выглядят правдоподобно.
        conn.execute(
            f"""
            INSERT INTO scope_deal(deal_id)
            SELECT d.deal_id FROM fact_deal d
            JOIN user_home h ON h.user_id = d.assigned_by_id
            WHERE d.is_deleted = 0 AND h.department_id IN ({placeholders})
            """,
            scope.department_ids,
        )
        conn.execute(
            f"""
            INSERT INTO scope_lead(lead_id)
            SELECT l.lead_id FROM fact_lead l
            JOIN user_home h ON h.user_id = l.assigned_by_id
            WHERE l.is_deleted = 0 AND h.department_id IN ({placeholders})
            """,
            scope.department_ids,
        )

    for statement in _VIEW_DDL:
        conn.execute(statement)


def _user_home(conn) -> None:
    """Отдел каждого человека — один ответ на весь запрос.

    Отделов у человека может быть названо два: подразделение в карточке
    Битрикса и строка ростера, которой администратор сказал, за какой отдел
    этот человек на самом деле работает. Ростер сильнее — он и заводился
    затем, чтобы поправить портал, а не наоборот.

    Правило вынесено в таблицу, а не повторено в каждом представлении, по
    той же причине, по которой РОП берётся из состава плана: два места, где
    считается «чей человек», однажды разойдутся, и разойдутся молча. Здесь
    их четыре — видимость людей, видимость норм, видимость строк ростера и
    отбор сделок, — и все четыре обязаны отвечать одинаково.

    Строка периода перекрывает строку «на все периоды», а из нескольких
    периодов побеждает поздний: ORDER BY, а не LIMIT 1 наугад — при двух
    строках без него SQLite вернул бы любую, и область видимости РОПа
    менялась бы от плана запроса.
    """
    conn.execute(
        "CREATE TEMP TABLE user_home (user_id INTEGER PRIMARY KEY, department_id INTEGER)"
    )
    # Таблица заполняется сразу, а представления читают её потом. Отсюда
    # разница в поведении: отсутствующую таблицу представление обнаружило бы
    # только при запросе, а этот INSERT падает прямо в apply_scope. Витрина
    # старее третьей версии схемы ростера не знает, и открывать на ней
    # соединение всё равно нужно — например, чтобы метрика сказала «нет
    # данных», а не чтобы дашборд упал на подключении с именем чужой таблицы
    # в ошибке.
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "dim_user" not in tables:
        return
    if "plan_roster" not in tables:
        conn.execute(
            "INSERT INTO user_home(user_id, department_id) "
            "SELECT user_id, department_id FROM dim_user"
        )
        return
    conn.execute(
        """
        INSERT INTO user_home(user_id, department_id)
        SELECT ids.user_id,
               COALESCE(
                   (SELECT r.department_id FROM plan_roster r
                     WHERE r.user_id = ids.user_id AND r.department_id IS NOT NULL
                     ORDER BY r.period_code DESC LIMIT 1),
                   (SELECT u.department_id FROM dim_user u
                     WHERE u.user_id = ids.user_id)
               )
        FROM (SELECT user_id FROM dim_user
              UNION
              SELECT user_id FROM plan_roster) ids
        """
    )


@contextmanager
def scoped_session(scope: Scope) -> Iterator:
    """Соединение с витриной, суженное до области видимости.

    Единственный способ читать витрину из веба. Метрики обращаются только к
    представлениям, которые создаёт этот менеджер, поэтому вызвать их на
    неограниченном соединении нельзя — запрос упадёт на отсутствующем
    v_deal, а не молча покажет чужие данные.
    """
    with analytics_session(readonly=True) as conn:
        apply_scope(conn, scope)
        yield conn
