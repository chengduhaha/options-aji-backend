"""Constants and JSON schema for the supply-chain graph."""
from __future__ import annotations

NODE_TYPES = ("company", "segment", "industry", "product")
REL_TYPES = (
    "supplies_to",
    "mutual_supply",
    "invests_in",
    "parent_of",
    "has_segment",
    "joint_development",
    "partnership",
    "competitor",
    "licenses_to",
    "manufactures_for",
    "thematic_link",
)
DIRECTIONS = ("directed", "bidirectional", "undirected")
MOAT_TIERS = ("exclusive", "primary", "dominant", "scarce", "normal")
CONFIDENCE_LEVELS = ("confirmed", "inferred")

GRAPH_INGEST_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["nodes", "edges"],
    "additionalProperties": False,
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "node_type", "name_zh"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "node_type": {"type": "string", "enum": list(NODE_TYPES)},
                    "ticker": {"type": ["string", "null"]},
                    "market": {"type": ["string", "null"]},
                    "name_zh": {"type": "string", "minLength": 1},
                    "name_en": {"type": ["string", "null"]},
                    "sector": {"type": ["string", "null"]},
                    "is_listed": {"type": "boolean"},
                    "logo_url": {"type": ["string", "null"]},
                    "attrs": {"type": "object"},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "target", "rel_type", "label", "confidence", "as_of_date"],
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "target": {"type": "string", "minLength": 1},
                    "rel_type": {"type": "string", "enum": list(REL_TYPES)},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "label": {"type": ["string", "null"]},
                    "semantic": {"type": ["string", "null"]},
                    "moat_tier": {"type": ["string", "null"], "enum": [None, *MOAT_TIERS]},
                    "weight": {"type": ["number", "null"]},
                    "attrs": {"type": "object"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
                    "evidence": {"type": ["string", "null"]},
                    "source_url": {"type": ["string", "null"]},
                    "as_of_date": {"type": "string", "format": "date"},
                },
            },
        },
        "views": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["slug", "title", "perspective"],
                "additionalProperties": False,
                "properties": {
                    "slug": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "perspective": {"type": "string", "minLength": 1},
                    "focus": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "config": {"type": "object"},
                },
            },
        },
    },
}

