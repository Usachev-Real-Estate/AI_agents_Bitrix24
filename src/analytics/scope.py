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
    # Последнее условие — про людей, которых ростер плана приписал к этому
    # отделу. В портале РОП сплошь и рядом числится не там, где работает:
    # руководитель отдела «Волкова» сидит в служебном подразделении
    # «Битрикс». Без этой строки её РОП не увидел бы в своём составе
    # собственного руководителя, и отдел так и остался бы «без РОПа» —
    # ровно та поломка, которую ростер и заводился чинить.
    #
    # Утечкой это не является: строки ростера пишет администратор, и
    # приписать человека к отделу — сознательное решение о том, чей он.
    """
    CREATE TEMP VIEW v_user AS
    SELECT u.* FROM dim_user u
    WHERE (SELECT unrestricted FROM scope_flag) = 1
       OR u.department_id IN (SELECT department_id FROM scope_department)
       OR u.user_id IN (SELECT r.user_id FROM plan_roster r
                        WHERE r.department_id IN
                              (SELECT department_id FROM scope_department))
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
       OR COALESCE(
              r.department_id,
              (SELECT u.department_id FROM dim_user u WHERE u.user_id = r.user_id)
          ) IN (SELECT department_id FROM scope_department)
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
        conn.execute(
            f"""
            INSERT INTO scope_deal(deal_id)
            SELECT d.deal_id FROM fact_deal d
            JOIN dim_user u ON u.user_id = d.assigned_by_id
            WHERE d.is_deleted = 0 AND u.department_id IN ({placeholders})
            """,
            scope.department_ids,
        )
        conn.execute(
            f"""
            INSERT INTO scope_lead(lead_id)
            SELECT l.lead_id FROM fact_lead l
            JOIN dim_user u ON u.user_id = l.assigned_by_id
            WHERE l.is_deleted = 0 AND u.department_id IN ({placeholders})
            """,
            scope.department_ids,
        )

    for statement in _VIEW_DDL:
        conn.execute(statement)


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
