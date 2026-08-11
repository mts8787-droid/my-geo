from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValidationFinding:
    code: str
    message: str


class FeedValidator:
    """Channel-limit and evidence-reference checks independent of generation."""

    def validate(self, feed: Any, valid_evidence_ids: set[str], title_limit: int, body_limit: int) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        if len(feed.brand_title) > title_limit:
            findings.append(ValidationFinding("TITLE_LIMIT", f"Title이 {title_limit}자를 초과했습니다."))
        if len(feed.brand_body_copy) > body_limit:
            findings.append(ValidationFinding("BODY_LIMIT", f"Body copy가 {body_limit}자를 초과했습니다."))
        cited = {item.strip() for item in str(feed.evidence_ids).split(",") if item.strip()}
        if not cited:
            findings.append(ValidationFinding("EVIDENCE_REQUIRED", "근거 ID가 없습니다."))
        elif not cited.issubset(valid_evidence_ids):
            findings.append(ValidationFinding("UNKNOWN_EVIDENCE", "존재하지 않는 근거 ID가 포함되어 있습니다."))
        return findings
