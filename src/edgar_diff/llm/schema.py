from typing import Literal

from pydantic import BaseModel

TopicLabel = Literal[
    "financial_results", "risk_factor", "legal_regulatory",
    "business_operations", "governance_management", "accounting_policy",
]
Label = Literal[TopicLabel, "non_material", "uncertain"]  # type: ignore[valid-type]


class Classification(BaseModel):
    """What the model returns for one changed paragraph. Every field is required."""
    label: Label
    secondary_label: TopicLabel | None
    is_material: bool
    confidence: Literal["high", "medium", "low"]
    rationale: str
    evidence_quotes: list[str]
    uncertainty_reason: str | None


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema with additionalProperties=false on every object, for structured outputs."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema
