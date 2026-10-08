from __future__ import annotations

from collections import defaultdict

from .schemas import Claim, Entity, make_id
from .store import insert_edge


def materialise_edges(claims: list[Claim], entities: list[Entity]) -> int:
    """Create simple queryable graph edges from owner claims to extracted entities.

    The graph deliberately stores provenance on the edge. It does not assert that every extracted
    entity is medically/legal true; it says this claim mentions/connects the owner to that entity with
    the claim's source basis, confidence and sensitivity still available on the claim row.
    """
    ents_by_record: dict[str, list[Entity]] = defaultdict(list)
    for ent in entities:
        if ent.source_record_key:
            ents_by_record[ent.source_record_key].append(ent)
    count = 0
    for claim in claims:
        # Owner -> claim object, useful even if no entity resolver match.
        object_node = make_id("literal", claim.object.lower().strip())
        insert_edge(
            make_id("edge", "owner", claim.predicate, object_node, claim.claim_id),
            "owner",
            claim.predicate,
            object_node,
            claim_id=claim.claim_id,
            confidence=claim.confidence,
            sensitivity=str(claim.sensitivity),
        )
        count += 1
        for ent in ents_by_record.get(claim.source_record_key, []):
            insert_edge(
                make_id("edge", "owner", claim.predicate, ent.entity_id, claim.claim_id),
                "owner",
                claim.predicate,
                ent.entity_id,
                claim_id=claim.claim_id,
                confidence=min(claim.confidence, ent.confidence),
                sensitivity=str(claim.sensitivity),
            )
            insert_edge(
                make_id("edge", ent.entity_id, "mentioned_in_claim", claim.claim_id),
                ent.entity_id,
                "mentioned_in_claim",
                claim.claim_id,
                claim_id=claim.claim_id,
                confidence=min(claim.confidence, ent.confidence),
                sensitivity=str(claim.sensitivity),
            )
            count += 2
    return count
