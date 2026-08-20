"""Mask contact PII in CRM card text before sending to an LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tools import _as_list, _clean_str, _coerce_int

# Границы слова с учётом кириллицы. Без них «Ира» совпадает внутри «кварт-ира»,
# «Лиза» — внутри «ана-лиза»: для базы недвижимости это калечит почти каждую
# карточку и меняет смысл текста, который уходит модели.
_LEFT_BOUNDARY = r"(?<!\w)"
_RIGHT_BOUNDARY = r"(?!\w)"

MIN_FRAGMENT_LEN = 3


@dataclass
class MaskMap:
    """Bidirectional map: sensitive fragments ↔ stable placeholders."""

    placeholder_to_value: dict[str, str] = field(default_factory=dict)
    patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)
    _collapse: list[tuple[re.Pattern[str], str]] = field(default_factory=list)

    def apply(self, text: str) -> str:
        if not text or not self.patterns:
            return text
        out = text
        for pattern, placeholder in self.patterns:
            out = pattern.sub(placeholder, out)
        # «Ириной Логутиной» → «КЛИЕНТ_1 КЛИЕНТ_1» → «КЛИЕНТ_1». Один человек
        # должен выглядеть как один участник, иначе модель посчитает их двумя,
        # а обратная подстановка задвоит имя.
        for pattern, placeholder in self._collapse:
            out = pattern.sub(placeholder, out)
        return out

    def unmask(self, text: str) -> str:
        if not text or not self.placeholder_to_value:
            return text
        out = text
        for placeholder, original in sorted(
            self.placeholder_to_value.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            out = out.replace(placeholder, original)
        return out


def _inflect_russian_name(word: str) -> set[str]:
    """Common spoken/written case forms for a single name token."""
    forms: set[str] = set()
    if len(word) < MIN_FRAGMENT_LEN:
        return forms
    lower = word.lower()
    if lower.endswith("а"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("ы", "е", "у", "ой"))
    elif lower.endswith("я"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("и", "е", "ю", "ей"))
    elif lower.endswith("ий"):
        stem = word[:-2]
        forms.update(stem + ending for ending in ("ия", "ию", "ием", "ии"))
    elif lower.endswith("ый"):
        stem = word[:-2]
        forms.update(stem + ending for ending in ("ого", "ому", "ым", "ом"))
    elif lower.endswith("ь"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("и", "ью"))
    return {f for f in forms if len(f) >= MIN_FRAGMENT_LEN}


def _name_variants(full_name: str) -> list[str]:
    name = _clean_str(full_name)
    if not name:
        return []
    variants: set[str] = {name}
    for part in name.split():
        if len(part) < MIN_FRAGMENT_LEN:
            continue
        variants.add(part)
        variants.update(_inflect_russian_name(part))
    return sorted(variants, key=len, reverse=True)


def _digits_only(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits


def _phone_variants(phone: str) -> list[str]:
    raw = _clean_str(phone)
    if not raw:
        return []
    digits = _digits_only(raw)
    variants: set[str] = {raw}
    if len(digits) >= 10:
        head, mid, tail1, tail2 = (
            digits[-10:][:3],
            digits[-10:][3:6],
            digits[-10:][6:8],
            digits[-10:][8:10],
        )
        last10 = digits[-10:]
        variants.update({
            last10,
            "8" + last10,
            "7" + last10,
            "+7" + last10,
            f"+7 ({head}) {mid}-{tail1}-{tail2}",
            f"8 ({head}) {mid}-{tail1}-{tail2}",
            f"+7-{head}-{mid}-{tail1}-{tail2}",
            f"8-{head}-{mid}-{tail1}-{tail2}",
        })
    return sorted(variants, key=len, reverse=True)


def _multi_field_values(raw: Any) -> list[str]:
    values: list[str] = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            text = _clean_str(item.get("VALUE") or item.get("value"))
        else:
            text = _clean_str(item)
        if text:
            values.append(text)
    return values


def _contact_display_name(contact: dict[str, Any]) -> str:
    parts = [
        _clean_str(contact.get("LAST_NAME") or contact.get("last_name")),
        _clean_str(contact.get("NAME") or contact.get("name")),
        _clean_str(contact.get("SECOND_NAME") or contact.get("second_name")),
    ]
    full = " ".join(p for p in parts if p).strip()
    if full:
        return full
    return _clean_str(contact.get("TITLE") or contact.get("title"))


def _ordered_contacts(contacts: list[Any]) -> list[dict[str, Any]]:
    """Deterministic order so placeholders mean the same person across runs.

    Masked state is persisted between runs; if КЛИЕНТ_1 pointed at a different
    contact next time, the stored history would silently describe someone else.
    """
    rows = [c for c in contacts if isinstance(c, dict)]
    return sorted(rows, key=lambda c: _coerce_int(c.get("ID") or c.get("id")))


def build_mask_map(
    deal: dict[str, Any],
    contacts: list[dict[str, Any]] | None = None,
) -> MaskMap:
    """Collect FIO / phones / emails from deal contacts into placeholders."""
    mask = MaskMap()
    client_idx = 0
    phone_idx = 0
    email_idx = 0
    fragments: list[tuple[str, str]] = []

    source = contacts if contacts is not None else _as_list(deal.get("contacts"))
    for contact in _ordered_contacts(list(source)):
        display = _contact_display_name(contact)
        if display:
            client_idx += 1
            placeholder = f"КЛИЕНТ_{client_idx}"
            mask.placeholder_to_value[placeholder] = display
            for variant in _name_variants(display):
                fragments.append((variant, placeholder))
        for phone in _multi_field_values(contact.get("PHONE") or contact.get("phone")):
            phone_idx += 1
            placeholder = f"ТЕЛЕФОН_{phone_idx}"
            mask.placeholder_to_value[placeholder] = phone
            for variant in _phone_variants(phone):
                fragments.append((variant, placeholder))
        for email in _multi_field_values(contact.get("EMAIL") or contact.get("email")):
            email_idx += 1
            placeholder = f"ПОЧТА_{email_idx}"
            mask.placeholder_to_value[placeholder] = email
            fragments.append((email, placeholder))

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for fragment, placeholder in sorted(
        fragments, key=lambda x: len(x[0]), reverse=True,
    ):
        key = (fragment.casefold(), placeholder)
        if key in seen or len(fragment) < MIN_FRAGMENT_LEN:
            continue
        seen.add(key)
        ordered.append((fragment, placeholder))

    mask.patterns = [
        (
            re.compile(
                _LEFT_BOUNDARY + re.escape(fragment) + _RIGHT_BOUNDARY,
                re.IGNORECASE,
            ),
            placeholder,
        )
        for fragment, placeholder in ordered
    ]
    mask._collapse = [
        (re.compile(rf"{re.escape(ph)}(?:[\s,]+{re.escape(ph)})+"), ph)
        for ph in mask.placeholder_to_value
    ]
    return mask


def apply_mask(text: str, mask_map: MaskMap) -> str:
    """Replace known contact fragments with placeholders."""
    return mask_map.apply(text or "")


def unmask(text: str, mask_map: MaskMap) -> str:
    """Restore original contact fragments in report text."""
    return mask_map.unmask(text or "")
