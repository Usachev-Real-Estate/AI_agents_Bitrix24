"""У каждого этапа аудита должна быть своя норма, а не умолчание.

Дефект, найденный 31.08 при проверке «Переговоров». Этап вернули в аудит
(PR #53), но нормы свежести ему не задали — и он молча взял `_default: 7`,
хотя каденс основного аудита для него 3 дня.

Над таблицей `SELLER_WORK_WINDOW_DAYS` это правило записано прямым текстом:
«окна должны совпадать с каденсом основного аудита, двум правилам об одном и
том же расходиться нельзя, иначе брокер получит два разных срока за одну и
ту же работу». Комментарий был, проверки не было — и первая же добавка этап
пропустила.

Цена молчания тут двойная. Норма 7 дней вместо 3 — брокеру прощается вдвое
больше. А отсрочка считается как максимум из отсрочки и окна
(`judgement_starts_after`), то есть стала неделей: карточка, попавшая на
«Переговоры» пять дней назад, не судится вовсе — и с новыми правилами
отчёта (⏳ не печатается) исчезает из него бесследно.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE  # noqa: E402
from tools import (  # noqa: E402
    BUYERS_STAGE_AUDIT_RULE,
    SELLERS_STAGE_CADENCE,
)

PROFILES = (BUYER_PROFILE, SELLER_PROFILE)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_every_audited_stage_names_its_window(profile):
    """Умолчание — не решение: этап в аудите обязан назвать свою норму."""
    missing = [
        stage for stage in sorted(profile.audited_stages)
        if stage not in profile.work_window_days
    ]
    assert not missing, (
        f"{profile.label}: этапы в аудите без своей нормы свежести "
        f"({', '.join(missing)}) — возьмут _default и разойдутся с каденсом"
    )


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_every_audited_stage_names_the_facts_it_asks_for(profile):
    """Без списка фактов этап получает «полнота не оценивалась»."""
    from funnel_profiles import all_facts_for_stage

    empty = [
        stage for stage in sorted(profile.audited_stages)
        if not all_facts_for_stage(profile.key, stage)
    ]
    assert not empty, f"{profile.label}: этапы в аудите без фактов ({empty})"


def test_the_qc_window_matches_the_main_audit_cadence_for_sellers():
    """Двум правилам об одном и том же расходиться нельзя.

    #UC_KEOOG8: QC судил бы за 7 дней, основной аудит — за 3.
    """
    for stage in sorted(SELLER_PROFILE.audited_stages):
        cadence = SELLERS_STAGE_CADENCE.get(stage)
        if cadence is None:
            continue
        assert SELLER_PROFILE.work_window_days[stage] == cadence.days, stage


def test_the_qc_window_matches_the_main_audit_cadence_for_buyers():
    for stage in sorted(BUYER_PROFILE.audited_stages):
        cadence = BUYERS_STAGE_AUDIT_RULE.get(stage)
        if cadence is None:
            continue
        assert BUYER_PROFILE.work_window_days[stage] == int(cadence), stage


def test_no_audited_stage_is_also_out_of_quality_control():
    """Два взаимоисключающих решения об одном этапе."""
    for profile in PROFILES:
        both = profile.audited_stages & profile.stages_out_of_qc
        assert not both, f"{profile.label}: {both}"
