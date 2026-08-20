"""Mask contact PII in CRM card text before sending to an LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tools import _as_list, _clean_str

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)


@dataclass
class MaskMap:
    """Bidirectional map: sensitive fragments ↔ stable placeholders."""

    placeholder_to_value: dict[str, str] = field(default_factory=dict)
    _patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)

    def apply(self, text: str) -> str:
        if not text or not self._patterns:
            return text
        out = text
        for pattern, placeholder in self._patterns:
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
    if len(word) < 3:
        return forms
    lower = word.lower()
    if lower.endswith("а"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("ы", "е", "у", "ой", "е"))
    elif lower.endswith("я"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("и", "е", "ю", "ей", "и"))
    elif lower.endswith("ий"):
        stem = word[:-2]
        forms.update(stem + ending for ending in ("ия", "ию", "ием", "ии"))
    elif lower.endswith("ый"):
        stem = word[:-2]
        forms.update(stem + ending for ending in ("ого", "ому", "ым", "ом"))
    elif lower.endswith("ь"):
        stem = word[:-1]
        forms.update(stem + ending for ending in ("и", "ью", "и"))
    return {f for f in forms if len(f) >= 3}


def _name_variants(full_name: str) -> list[str]:
    name = _clean_str(full_name)
    if not name:
        return []
    variants: set[str] = {name}
    for part in name.split():
        if len(part) < 3:
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
        last10 = digits[-10:]
        variants.update(
            {
                last10,
                "8" + last10,
                "7" + last10,
                "+7" + last10,
                f"+7{last10}",
                f"8{last10}",
                f"+7 ({last10[:3]}) {last10[3:6]}-{last10[6:8]}-{last10[8:10]}",
                f"8 ({last10[:3]}) {last10[3:6]}-{last10[6:8]}-{last10[8:10]}",
                f"+7-{last10[:3]}-{last10[3:6]}-{last10[6:8]}-{last10[8:10]}",
                f"8-{last10[:3]}-{last10[3:6]}-{last10[6:8]}-{last10[8:10]}",
            },
        )
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

    contact_list = contacts if contacts is not None else _as_list(deal.get("contacts"))
    for contact in contact_list:
        if not isinstance(contact, dict):
            continue
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

    # Dedupe fragments: longest match wins at apply time.
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for fragment, placeholder in sorted(fragments, key=lambda x: len(x[0]), reverse=True):
        key = (fragment.casefold(), placeholder)
        if key in seen or len(fragment) < 3:
            continue
        seen.add(key)
        ordered.append((fragment, placeholder))

    mask._patterns = [
        (re.compile(re.escape(fragment), re.IGNORECASE), placeholder)
        for fragment, placeholder in ordered
    ]
    # Also mask emails discovered in free text when they match known addresses.
    for placeholder, email in list(mask.placeholder_to_value.items()):
        if placeholder.startswith("ПОЧТА_"):
            mask._patterns.append(
                (re.compile(re.escape(email), re.IGNORECASE), placeholder),
            )
    return mask


def apply_mask(text: str, mask_map: MaskMap) -> str:
    """Replace known contact fragments with placeholders."""
    return mask_map.apply(text or "")


def unmask(text: str, mask_map: MaskMap) -> str:
    """Restore original contact fragments in report text."""
    return mask_map.unmask(text or "")


def mask_emails_in_text(text: str, mask_map: MaskMap) -> str:
    """Mask stray email addresses that match known contact emails."""
    if not text:
        return text
    out = text
    for placeholder, email in mask_map.placeholder_to_value.items():
        if not placeholder.startswith("ПОЧТА_"):
            continue
        out = re.sub(re.escape(email), placeholder, out, flags=re.IGNORECASE)
    return out
