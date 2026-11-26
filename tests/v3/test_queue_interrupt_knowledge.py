"""The settings assistant can explain queueing and interrupting a running chat."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure


def test_queue_and_send_now_guidance_is_searchable(monkeypatch):
    ensure()
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    found = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "queue or interrupt messages send now while Butler mode thread answering"
        )
    )
    assert "Send now" in found
    assert "interrupt" in found
    assert "Butler mode" in found
