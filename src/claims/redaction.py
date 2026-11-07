"""Removes identifiers a claim doesn't need before email text reaches Claude or the audit log.

Claimant names, policy numbers and contact details stay, because the claim can't be processed without them.
Pattern matching only sees text: it can't remove anything from inside a PDF or image, so those reach Claude as sent.
Have your security team test this on a sample of real data before relying on it.
"""
from __future__ import annotations

import re

_SECRET_PAIR = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s,;&]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_ACCOUNT = re.compile(r"(?i)\b(account|acct|a/c)((?:[ _-]?(?:no|num|number|id))?\s*[:=#]?\s*)(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,34}\b")
_ROUTING = re.compile(r"(?i)\b(routing|sort code|aba)((?:[ _-]?(?:no|num|number|code))?\s*[:=#]?\s*)[\d-]{6,12}\b")
_CARD = re.compile(r"(?<![\w-])[2-6](?:[ -]?\d){12,18}(?![\w-])")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[REDACTED-CARD]" if _luhn_valid(digits) else match.group(0)


def redact(text: str) -> str:
    text = _SECRET_PAIR.sub(r"\1\2[REDACTED-SECRET]", text)
    text = _SSN.sub("[REDACTED-SSN]", text)
    text = _IBAN.sub("[REDACTED-ACCOUNT]", text)
    text = _ACCOUNT.sub(r"\1\2[REDACTED-ACCOUNT]", text)
    text = _ROUTING.sub(r"\1\2[REDACTED-ACCOUNT]", text)
    return _CARD.sub(_card, text)
