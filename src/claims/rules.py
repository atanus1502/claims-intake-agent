"""Loads the fast-track rules from YAML. Which rules apply lives in the file; how each check works stays in code.

The file is checked strictly: a typo or a value out of range raises RulesError, which stops processing. Quietly ignoring a
mistyped setting could turn a rule off, and that's worse than a delay.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from claims.models import FieldName

POLICE_REPORT = "police_report"
NO_INJURY_STATEMENT = "no_injury_statement"
_REQUIREMENTS = {POLICE_REPORT, NO_INJURY_STATEMENT}
_FIELD_NAMES = {field.value for field in FieldName}
_TOP_LEVEL_KEYS = {"version", "confidence_threshold", "amount_tolerance", "confidence_checked", "recognized_loss_types", "opus", "defaults", "lines"}
_OPUS_KEYS = {"complex_amount", "coverage_after_reread", "redo_unsure_coverage"}
_REVIEWABLE_FLAGS = {"injury_reported", "third_party_involved"}
_LINE_KEYS = {
    "fast_track_limit", "late_report_days", "new_policy_days", "max_prior_claims", "prior_claims_window_days",
    "active_policy_statuses", "review_if_reported", "required_fields", "loss_types",
}


class RulesError(Exception):
    pass


@dataclass(frozen=True)
class LineRules:
    fast_track_limit: Decimal
    late_report_days: int
    new_policy_days: int
    max_prior_claims: int
    prior_claims_window_days: int
    active_policy_statuses: tuple[str, ...]
    review_if_reported: tuple[str, ...]
    required_fields: tuple[str, ...]
    loss_types: dict[str, frozenset[str]]

    def requires(self, loss_type: str | None) -> frozenset[str]:
        return self.loss_types.get(loss_type, frozenset())


@dataclass(frozen=True)
class RuleSet:
    version: str
    digest: str
    confidence_threshold: float
    amount_tolerance: Decimal
    confidence_checked: tuple[str, ...]
    recognized_loss_types: tuple[str, ...]
    complex_amount: Decimal
    coverage_after_reread: bool
    redo_unsure_coverage: bool
    defaults: LineRules
    lines: dict[str, LineRules]

    def for_line(self, line: str | None) -> LineRules:
        """The rules for a line of business. Before a policy is matched, or for an unknown line, these are the defaults."""
        return self.lines.get(line, self.defaults)


def _number(where: str, value: Any, minimum: float, maximum: float | None = None) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesError(f"{where} must be a number, not {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        raise RulesError(f"{where} is out of range: {value}")
    return value


def _whole_number(where: str, value: Any) -> int:
    if not isinstance(_number(where, value, 0), int):
        raise RulesError(f"{where} must be a whole number, not {value!r}")
    return value


def _field_list(where: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RulesError(f"{where} must be a list of field names")
    unknown = sorted(str(name) for name in value if name not in _FIELD_NAMES)
    if unknown:
        raise RulesError(f"{where} names unknown fields: {', '.join(unknown)}")
    return tuple(value)


def _mapping(where: str, value: Any, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RulesError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise RulesError(f"{where} has unknown settings: {', '.join(unknown)}")
    return value


def _text_list(where: str, value: Any, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        raise RulesError(f"{where} must be a list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise RulesError(f"{where} must contain only text")
    return tuple(value)


def _flag(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise RulesError(f"{where} must be true or false, not {value!r}")
    return value


def _loss_types(where: str, value: Any, recognized: set[str]) -> dict[str, frozenset[str]]:
    result = {}
    for loss_type, rule in _mapping(where, value, recognized).items():
        requires = _mapping(f"{where}.{loss_type}", rule, {"requires"}).get("requires")
        if not isinstance(requires, list):
            raise RulesError(f"{where}.{loss_type} needs a list of requirements, which can be empty")
        unknown = sorted(str(item) for item in requires if item not in _REQUIREMENTS)
        if unknown:
            raise RulesError(f"{where}.{loss_type} has unknown requirements: {', '.join(unknown)}")
        result[loss_type] = frozenset(requires)
    return result


def _line_rules(where: str, values: dict[str, Any], base: LineRules | None, recognized: set[str]) -> LineRules:
    """Builds one line's rules. Defaults must set everything, and a line overrides only what it names."""
    def setting(name: str) -> Any:
        if name in values:
            return values[name]
        if base is None:
            raise RulesError(f"{where} is missing {name}")
        return getattr(base, name)

    limit = setting("fast_track_limit")
    if not isinstance(limit, Decimal):
        limit = Decimal(str(_number(f"{where}.fast_track_limit", limit, 0)))
    required = setting("required_fields")
    loss_types = dict(base.loss_types) if base else {}
    if base is None and "loss_types" not in values:
        raise RulesError(f"{where} is missing loss_types")
    loss_types.update(_loss_types(f"{where}.loss_types", values.get("loss_types"), recognized))
    review = _text_list(f"{where}.review_if_reported", setting("review_if_reported"), allow_empty=True)
    unknown = sorted(set(review) - _REVIEWABLE_FLAGS)
    if unknown:
        raise RulesError(f"{where}.review_if_reported has unknown flags: {', '.join(unknown)}")
    return LineRules(
        fast_track_limit=limit,
        late_report_days=_whole_number(f"{where}.late_report_days", setting("late_report_days")),
        new_policy_days=_whole_number(f"{where}.new_policy_days", setting("new_policy_days")),
        max_prior_claims=_whole_number(f"{where}.max_prior_claims", setting("max_prior_claims")),
        prior_claims_window_days=_whole_number(f"{where}.prior_claims_window_days", setting("prior_claims_window_days")),
        active_policy_statuses=_text_list(f"{where}.active_policy_statuses", setting("active_policy_statuses")),
        review_if_reported=review,
        required_fields=_field_list(f"{where}.required_fields", list(required)),
        loss_types=loss_types,
    )


def parse_rules(data: Any, digest: str = "") -> RuleSet:
    top = _mapping("the rules file", data, _TOP_LEVEL_KEYS)
    missing = sorted(_TOP_LEVEL_KEYS - top.keys())
    if missing:
        raise RulesError(f"the rules file is missing: {', '.join(missing)}")
    if not isinstance(top["version"], str) or not top["version"].strip():
        raise RulesError("version must be text")

    loss_type_list = _text_list("recognized_loss_types", top["recognized_loss_types"])
    if "other" not in loss_type_list:
        raise RulesError("recognized_loss_types must include other")
    recognized = set(loss_type_list)
    opus = _mapping("opus", top["opus"], _OPUS_KEYS)
    missing_opus = sorted(_OPUS_KEYS - opus.keys())
    if missing_opus:
        raise RulesError(f"opus is missing: {', '.join(missing_opus)}")

    defaults = _line_rules("defaults", _mapping("defaults", top["defaults"], _LINE_KEYS), None, recognized)
    raw_lines = top["lines"]
    if not isinstance(raw_lines, dict) or not raw_lines:
        raise RulesError("lines must name at least one line of business")
    lines = {
        str(name): _line_rules(f"lines.{name}", _mapping(f"lines.{name}", values, _LINE_KEYS), defaults, recognized)
        for name, values in raw_lines.items()
    }

    checked = _field_list("confidence_checked", top["confidence_checked"])
    for where, rules in [("defaults", defaults), *((f"lines.{name}", rules) for name, rules in lines.items())]:
        unchecked = sorted(set(rules.required_fields) - set(checked))
        if unchecked:
            raise RulesError(f"{where}.required_fields must also be in confidence_checked: {', '.join(unchecked)}")

    return RuleSet(
        version=top["version"],
        digest=digest,
        confidence_threshold=float(_number("confidence_threshold", top["confidence_threshold"], 0, 1)),
        amount_tolerance=Decimal(str(_number("amount_tolerance", top["amount_tolerance"], 0, 1))),
        confidence_checked=checked,
        recognized_loss_types=loss_type_list,
        complex_amount=Decimal(str(_number("opus.complex_amount", opus["complex_amount"], 0))),
        coverage_after_reread=_flag("opus.coverage_after_reread", opus["coverage_after_reread"]),
        redo_unsure_coverage=_flag("opus.redo_unsure_coverage", opus["redo_unsure_coverage"]),
        defaults=defaults,
        lines=lines,
    )


def load_rules(path: Path) -> RuleSet:
    try:
        raw = Path(path).read_bytes()
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"can't read the rules file {path}: {exc}") from exc
    return parse_rules(data, digest=hashlib.sha256(raw).hexdigest()[:12])
