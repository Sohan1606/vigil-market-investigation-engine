"""Document-store semantics the pipeline depends on."""
from __future__ import annotations

from vigil.storage.docstore import DocumentStore


def test_drop_actually_empties_a_collection(cfg):
    """Regression: mongita's drop() silently kept documents, duplicating them on every run."""
    s = DocumentStore(cfg)
    before = s.count("human_votes")
    s.insert_many("human_votes", [{"forecast_id": "TEST-DROP", "symbol": "X", "as_of": "2020-01-01",
                                   "human_call": "NO ACTION", "vigil_verdict": "NO ACTION",
                                   "agreement": True}])
    assert s.count("human_votes") == before + 1
    s.drop("human_votes")
    assert s.count("human_votes") == 0


def test_repeated_generation_does_not_duplicate_documents(cfg):
    from vigil.intelligence.patterns import build_pattern_library
    s = DocumentStore(cfg)
    first = len(build_pattern_library(cfg))
    assert s.count("patterns") == first
    second = len(build_pattern_library(cfg))
    assert s.count("patterns") == second == first, "pattern library duplicated on a second run"


def test_reads_degrade_instead_of_raising(cfg, monkeypatch):
    s = DocumentStore(cfg)

    class Boom:
        def find(self, *_a, **_k):
            raise RuntimeError("backend exploded")

    monkeypatch.setattr(s, "_db", {"cases": Boom()})
    assert s.find("cases") == []
    assert "failed on the" in s.detail
