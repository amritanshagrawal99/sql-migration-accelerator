"""Migration readiness reports (Markdown for humans, JSON for pipelines)."""
from __future__ import annotations

import json
from collections import Counter

from .models import FileResult, Status


def summary(results: list[FileResult]) -> dict:
    statuses: Counter = Counter()
    codes: Counter = Counter()
    for f in results:
        for st in f.statements:
            statuses[st.status.value] += 1
            codes.update(i.code for i in st.issues)
    total = sum(statuses.values())
    auto = statuses[Status.CONVERTED.value] + statuses[Status.CONVERTED_WITH_WARNINGS.value] + statuses[Status.DROPPED.value]
    return {
        "files": len(results),
        "statements": total,
        "by_status": {s.value: statuses[s.value] for s in Status},
        "automation_rate": round(auto / total, 4) if total else 1.0,
        "issues_by_code": dict(codes.most_common()),
    }


def to_json(results: list[FileResult]) -> str:
    return json.dumps(
        {"summary": summary(results),
         "files": [{"path": f.path, "statements": [s.to_dict() for s in f.statements]} for f in results]},
        indent=2,
    )


def to_markdown(results: list[FileResult], source: str, target: str) -> str:
    s = summary(results)
    lines = [
        f"# Migration readiness report: {source} -> {target}",
        "",
        f"- Files: **{s['files']}**",
        f"- Statements: **{s['statements']}**",
        f"- Automation rate: **{s['automation_rate']:.0%}** (converted, converted with warnings, or safely dropped)",
        "",
        "| Status | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in s["by_status"].items()],
        "",
    ]
    review = [(f, st) for f in results for st in f.statements
              if st.status in (Status.MANUAL_REVIEW, Status.CONVERTED_WITH_WARNINGS)]
    if review:
        lines += ["## Needs attention", "", "| File | Line | Status | Issue |", "|---|---|---|---|"]
        for f, st in review:
            for i in st.issues:
                if i.severity.value != "info":
                    msg = i.message.replace("|", "\\|")
                    lines.append(f"| {f.path} | {st.line} | {st.status.value} | `{i.code}` {msg} |")
        lines.append("")
    if s["issues_by_code"]:
        lines += ["## Rules applied", "", "| Code | Occurrences |", "|---|---|",
                  *[f"| `{k}` | {v} |" for k, v in s["issues_by_code"].items()], ""]
    return "\n".join(lines)
