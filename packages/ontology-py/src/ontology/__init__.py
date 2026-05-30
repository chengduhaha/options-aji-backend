"""OptionsAji Ontology - 渐进式加载器"""

from .loader import OntologyLoader
from .models import OntologyObject, OntologyPattern, OntologyRelation

__all__ = ["OntologyLoader", "OntologyObject", "OntologyPattern", "OntologyRelation"]
