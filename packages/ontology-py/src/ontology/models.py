"""Pydantic models for ontology documents."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class OntologyAttribute(BaseModel):
    type: str
    primary_key: bool = False
    pattern: Optional[str] = None
    description: Optional[str] = None
    references: Optional[str] = None
    default: Optional[bool | float | str] = None
    values: Optional[List[str]] = None
    range: Optional[List[float]] = None


class OntologyComputedField(BaseModel):
    type: Optional[str] = None
    description: Optional[str] = None
    rule: Optional[str] = None
    values: Optional[List[str]] = None


class OntologyObject(BaseModel):
    object_type: str
    display_name_zh: Optional[str] = None
    description: Optional[str] = None
    llm_description: Optional[str] = None
    attributes: Dict[str, OntologyAttribute] = Field(default_factory=dict)
    computed: Dict[str, OntologyComputedField] = Field(default_factory=dict)
    bindings: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)


class OntologyRelationItem(BaseModel):
    source: str
    target: str
    cardinality: Optional[str] = None
    semantic: Optional[str] = None
    metadata: Optional[List[str]] = None
    condition: Optional[str] = None


class OntologyRelation(BaseModel):
    relations: Dict[str, OntologyRelationItem] = Field(default_factory=dict)


class OntologyPattern(BaseModel):
    pattern_id: str
    pattern_type: str
    display_name_zh: Optional[str] = None
    time_budget_seconds: Optional[int] = None
    description_for_llm: Optional[str] = None
    triggers: List[Dict[str, object]] = Field(default_factory=list)
    reasoning_chain: List[Dict[str, object]] = Field(default_factory=list)
    output_template: Dict[str, object] = Field(default_factory=dict)
