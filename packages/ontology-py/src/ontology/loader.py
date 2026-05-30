"""Ontology 加载器 - 按需加载,避免一次性塞满 LLM 上下文"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import yaml

from .models import OntologyObject, OntologyPattern, OntologyRelation


class OntologyLoader:
    """渐进式加载 Ontology 定义。"""

    def __init__(self, ontology_root: Path):
        self.root = ontology_root
        self._object_index: Dict[str, Path] = {}
        self._relation_index: Dict[str, Path] = {}
        self._pattern_index: Dict[str, Path] = {}
        self._scan_index()

    def _scan_index(self) -> None:
        objects_dir = self.root / "objects"
        relations_dir = self.root / "relations"
        patterns_dir = self.root / "patterns"

        if objects_dir.exists():
            for file_path in objects_dir.glob("*.yaml"):
                self._object_index[file_path.stem] = file_path
        if relations_dir.exists():
            for file_path in relations_dir.glob("*.yaml"):
                self._relation_index[file_path.stem] = file_path
        if patterns_dir.exists():
            for file_path in patterns_dir.rglob("*.yaml"):
                self._pattern_index[file_path.stem] = file_path

    @lru_cache(maxsize=128)
    def load_object(self, name: str) -> OntologyObject:
        if not name:
            raise KeyError("Object name is required")
        file_path = self._object_index.get(name)
        if file_path is None:
            raise KeyError(f"Object not found: {name}")
        with file_path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return OntologyObject.model_validate(payload)

    @lru_cache(maxsize=128)
    def load_relation(self, name: str) -> OntologyRelation:
        if not name:
            raise KeyError("Relation name is required")
        file_path = self._relation_index.get(name)
        if file_path is None:
            raise KeyError(f"Relation not found: {name}")
        with file_path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return OntologyRelation.model_validate(payload)

    @lru_cache(maxsize=128)
    def load_pattern(self, pattern_id: str) -> OntologyPattern:
        if not pattern_id:
            raise KeyError("Pattern id is required")
        file_path = self._pattern_index.get(pattern_id)
        if file_path is None:
            raise KeyError(f"Pattern not found: {pattern_id}")
        with file_path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return OntologyPattern.model_validate(payload)

    def list_objects(self) -> list[str]:
        return sorted(self._object_index.keys())

    def list_relations(self) -> list[str]:
        return sorted(self._relation_index.keys())

    def list_patterns(self, pattern_type: Optional[str] = None) -> list[str]:
        if pattern_type is None:
            return sorted(self._pattern_index.keys())
        return sorted(
            pattern_id
            for pattern_id, path in self._pattern_index.items()
            if pattern_type in str(path.parent)
        )

    def match_pattern_for_trigger(self, trigger_context: Dict[str, str]) -> Optional[str]:
        content = trigger_context.get("content", "").lower()
        kol_handle = trigger_context.get("kol_handle", "")

        for pattern_id in self.list_patterns():
            pattern = self.load_pattern(pattern_id)
            for trigger in pattern.triggers:
                if "news_keywords" in trigger:
                    keywords = trigger["news_keywords"]
                    if isinstance(keywords, list) and any(
                        str(keyword).lower() in content for keyword in keywords
                    ):
                        return pattern_id
                if "kol_handles" in trigger:
                    handles = trigger["kol_handles"]
                    if isinstance(handles, list) and kol_handle in handles:
                        return pattern_id
        return None

    def get_llm_context_for_object(self, name: str) -> str:
        obj = self.load_object(name)
        return (
            f"Object: {obj.object_type}\n"
            f"Description: {obj.llm_description or obj.description or ''}\n"
            f"Attributes: {list(obj.attributes.keys())}\n"
            f"Computed: {list(obj.computed.keys())}"
        ).strip()
