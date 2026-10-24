"""Result types shared by the converter, reports and CLI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Severity(str, Enum):
    INFO = "info"        # semantic-preserving rewrite, nothing to review
    WARNING = "warning"  # converted, but behaviour may differ - review
    ERROR = "error"      # could not convert - manual work required


class Status(str, Enum):
    CONVERTED = "converted"
    CONVERTED_WITH_WARNINGS = "converted_with_warnings"
    MANUAL_REVIEW = "manual_review"
    DROPPED = "dropped"  # statement has no meaning on the target (e.g. COLLECT STATISTICS)


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass
class StatementResult:
    index: int
    line: int
    source_sql: str
    target_sql: str | None
    status: Status
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        for i in d["issues"]:
            i["severity"] = i["severity"].value
        return d


@dataclass
class FileResult:
    path: str
    statements: list[StatementResult]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Status}
        for st in self.statements:
            out[st.status.value] += 1
        return out
