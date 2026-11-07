"""Schemas Claude must answer in. Code decides what happens next; nothing here is a decision to pay."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class DocType(str, Enum):
    email = "email"
    invoice = "invoice"
    police_report = "police_report"
    photo = "photo"
    other = "other"


class FieldName(str, Enum):
    policy_number = "policy_number"
    claimant_name = "claimant_name"
    postal_code = "postal_code"
    date_of_loss = "date_of_loss"
    loss_type = "loss_type"
    loss_description = "loss_description"
    loss_location = "loss_location"
    amount_claimed = "amount_claimed"
    invoice_number = "invoice_number"
    invoice_total = "invoice_total"
    police_report_number = "police_report_number"
    injury_reported = "injury_reported"
    third_party_involved = "third_party_involved"


class FieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName
    value: str
    confidence: float
    evidence: str


class DocumentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DocType
    readable: bool
    fields: list[FieldValue]
    concerns: list[str]
    embedded_instructions: bool


class CoverageAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_number: str | None
    exclusion_applies: bool
    exclusion_ids: list[str]
    rationale: str
    confidence: float


def reason(code: str, detail: str) -> dict[str, str]:
    """One reason a claim needs a person. The code is for rules and metrics, the detail for the reviewer."""
    return {"code": code, "detail": detail}
