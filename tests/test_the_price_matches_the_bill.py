"""Счёт прогона должен сходиться с тем, что списал провайдер.

Выгрузка RouterAI за 01.09.2026: 758 оплаченных вызовов, 3 007 752 входных
и 1 256 144 выходных токена, списано 782,27 ₽. Прежний тариф в конфиге
(40 и 202 ₽ за 1М) давал на тех же токенах 374 ₽ — занижение ровно вдвое.

Врал при этом не провайдер, а наш собственный отчёт: цифра, по которой
принимали решения об экономии. По ней же была отвергнута верная оценка
холодного прогона (~310 ₽) в пользу «замеренных» 149 ₽.

Тест держит две вещи разом: арифметику estimate_cost и сами ставки. Ставки
живут в дефолтах конфига, а конфиг читает .env — поэтому проверяем именно
дефолты, а расхождение боевого .env ловится только глазами (о чём написано
в самом .env.example).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llm import estimate_cost  # noqa: E402

# Итоги суток из выгрузки. Только оплаченные вызовы: строки с нулевой
# стоимостью — это отказы провайдера (402), токенов в них нет.
CALLS = 758
INPUT_TOKENS = 3_007_752
OUTPUT_TOKENS = 1_256_144
# Размышления уже внутри OUTPUT_TOKENS и тарифицируются по цене выхода.
REASONING_TOKENS = 733_508
BILLED_RUB = 782.27


class _Tariff:
    """Ставки как их видит estimate_cost — из дефолтов конфига."""

    def __init__(self) -> None:
        from config import Settings
        fields = Settings.model_fields
        self.llm_price_input = fields["llm_price_input"].default
        self.llm_price_output = fields["llm_price_output"].default
        self.llm_price_cache_read = fields["llm_price_cache_read"].default


def test_the_estimate_reproduces_the_bill_to_the_kopeck():
    cost = estimate_cost(
        {
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "cached_tokens": 0,
        },
        _Tariff(),
    )
    assert abs(cost - BILLED_RUB) < 0.01, (
        f"прогон насчитал {cost:.2f} ₽, провайдер списал {BILLED_RUB} ₽"
    )


def test_the_old_tariff_would_halve_the_bill():
    """Тот самый разрыв — чтобы возврат старых чисел был виден как ошибка."""

    class _Old:
        llm_price_input = 40.0
        llm_price_output = 202.0
        llm_price_cache_read = 4.04

    cost = estimate_cost(
        {
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "cached_tokens": 0,
        },
        _Old(),
    )
    assert BILLED_RUB / cost > 2.0


def test_reasoning_is_inside_the_output_and_not_billed_twice():
    """RouterAI начисляет размышления отдельной строкой, но по цене выхода.

    Сумма обеих строк равна output_tokens × цена выхода — значит своего
    слагаемого в estimate_cost у них быть не должно. Проверяем от обратного:
    добавь их отдельно, и счёт разойдётся с выгруженным.
    """
    tariff = _Tariff()
    honest = estimate_cost(
        {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        tariff,
    )
    extra = REASONING_TOKENS * tariff.llm_price_output / 1_000_000
    assert abs(honest - BILLED_RUB) < 0.01
    # 309 ₽ из 782 — столько стоили размышления 01.09. Посчитай их дважды,
    # и счёт вырастет на 39 % против выгруженного.
    assert 300 < extra < 315
    assert (honest + extra) / BILLED_RUB > 1.35


def test_the_cache_read_is_the_cheap_line():
    """Ради чего вообще стоит чинить кэш: перечитывание вдесятеро дешевле."""
    tariff = _Tariff()
    assert tariff.llm_price_cache_read < tariff.llm_price_input / 9
