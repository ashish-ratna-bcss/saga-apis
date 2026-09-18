"""Investigation graph assembly -- JSON nodes/edges, no rendering (see
ITERATION 8: no UI, Swagger/OpenAPI is the documentation)."""
from sqlalchemy.orm import Session

from osint_app.models import Entity, EntityRelationship, Investigation


def build_graph(db: Session, investigation: Investigation) -> dict:
    entities = db.query(Entity).filter_by(investigation_id=investigation.id).all()
    relationships = db.query(EntityRelationship).filter_by(investigation_id=investigation.id).all()

    nodes = [
        {
            "id": e.id,
            "type": e.entity_type,
            "label": e.value,
            "claim_type": e.claim_type,
            "confidence": e.confidence,
            "is_root": e.normalized_value == investigation.normalized_identifier.lower(),
        }
        for e in entities
    ]
    edges = [
        {
            "id": r.id,
            "source": r.source_entity_id,
            "target": r.target_entity_id,
            "type": r.relationship_type,
            "confidence": r.confidence,
        }
        for r in relationships
    ]
    return {"investigation_id": investigation.id, "nodes": nodes, "edges": edges}
