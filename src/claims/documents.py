"""Turns a raw email into stored documents. Attachment types come from the file's bytes, never from what the email claims."""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from claims.config import Settings
from claims.models import reason
from claims.redaction import redact

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's limit per image

_SIGNATURES = [
    (b"%PDF-", "application/pdf", "pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "image", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "image", "jpg"),
    (b"GIF8", "image/gif", "image", "gif"),
]


@dataclass
class ParsedEmail:
    claim_id: str
    message_id: str
    sender: str
    subject: str
    documents: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _sniff(data: bytes) -> tuple[str, str, str] | None:
    for signature, media_type, kind, extension in _SIGNATURES:
        if data.startswith(signature):
            return media_type, kind, extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image", "webp"
    return None


def _body_text(message: email.message.EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        parser = _TextOnly()
        parser.feed(text)
        text = " ".join(parser.parts)
    return text


def claim_id_for(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    key = (message.get("Message-ID") or "").strip().encode() or raw
    return "CLM-" + hashlib.sha256(key).hexdigest()[:12].upper()


def parse_email(raw: bytes, settings: Settings) -> ParsedEmail:
    """Stores the body and supported attachments. Anything it can't pass on becomes an issue, which sends the claim to a person."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    claim_id = claim_id_for(raw)
    folder = settings.data_dir / "attachments" / claim_id
    folder.mkdir(parents=True, exist_ok=True)

    subject = str(message.get("Subject", ""))
    parsed = ParsedEmail(
        claim_id=claim_id,
        message_id=str(message.get("Message-ID", "")).strip(),
        sender=parseaddr(str(message.get("From", "")))[1].lower(),
        subject=subject,
    )

    body_path = folder / "email.txt"
    body_path.write_text(redact(f"Subject: {subject}\n\n{_body_text(message)}"))
    parsed.documents.append({
        "doc_id": "email", "kind": "email", "media_type": "text/plain", "path": str(body_path),
        "filename": "email body", "sha256": hashlib.sha256(body_path.read_bytes()).hexdigest(),
    })

    for number, part in enumerate(message.iter_attachments(), start=1):
        filename = re.sub(r"[^\w.\- ]", "_", part.get_filename() or f"attachment-{number}")[:100]
        data = part.get_payload(decode=True) or b""
        problem = None
        sniffed = _sniff(data)
        if number > settings.max_attachments:
            parsed.issues.append(reason("too_many_attachments", f"{filename} was not read: over the limit of {settings.max_attachments}."))
            continue
        if sniffed is None:
            problem = ("unsupported_attachment", f"{filename} isn't a PDF or image, so it was not read.")
        elif len(data) > settings.max_attachment_bytes or (sniffed[1] == "image" and len(data) > MAX_IMAGE_BYTES):
            problem = ("attachment_too_large", f"{filename} is too large to read ({len(data)} bytes).")
        if problem:
            parsed.issues.append(reason(*problem))
            continue
        media_type, kind, extension = sniffed
        doc_id = f"attachment-{number}"
        path = folder / f"{doc_id}.{extension}"
        path.write_bytes(data)
        parsed.documents.append({
            "doc_id": doc_id, "kind": kind, "media_type": media_type, "path": str(path),
            "filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
        })
    return parsed


def content_block(document: dict[str, Any]) -> dict[str, Any]:
    if document["kind"] == "email":
        text = Path(document["path"]).read_text().replace("</untrusted_document>", "")
        return {"type": "text", "text": f"<untrusted_document>\n{text}\n</untrusted_document>"}
    data = base64.standard_b64encode(Path(document["path"]).read_bytes()).decode()
    block_type = "document" if document["kind"] == "pdf" else "image"
    return {"type": block_type, "source": {"type": "base64", "media_type": document["media_type"], "data": data}}
