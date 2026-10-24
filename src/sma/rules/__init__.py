"""Source-dialect rule sets. Each source registers text rules and AST rules."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlglot import exp

from ..models import Issue


@dataclass
class TextResult:
    sql: str
    issues: list[Issue] = field(default_factory=list)
    drop: bool = False               # statement is a no-op on the target
    parse_with_target: bool = False  # text was rewritten into target syntax


TextRule = Callable[[str], TextResult]
AstRule = Callable[[exp.Expression, str], list[Issue]]


def _registry() -> dict[str, tuple[TextRule, AstRule]]:
    from . import teradata
    return {"teradata": (teradata.text_rules, teradata.ast_rules)}


def rules_for(source: str) -> tuple[TextRule | None, AstRule | None]:
    return _registry().get(source.lower(), (None, None))


def supported_sources() -> list[str]:
    return sorted(_registry())
