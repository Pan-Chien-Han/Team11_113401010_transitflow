"""
TransitFlow — pgvector Policy Document Seeder
Run once after starting Docker:
    python skeleton/seed_vectors.py

This script:
  1. Loads policy documents directly from train-mock-data/ JSON files
  2. Embeds each document using the configured LLM provider
  3. Stores the text + vector in PostgreSQL (policy_documents table)

Students: To extend the assistant's knowledge, add entries to the JSON files in
train-mock-data/ and re-run this script.
"""

import json
import os
import sys
import time

sys.path.insert(0, ".")

from skeleton.llm_provider import llm
from databases.relational.queries import store_policy_document

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def _text(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_documents():
    docs = []

    # ── refund_policy.json — one document per refund/compensation policy ──
    for policy in _load("refund_policy.json"):
        docs.append({
            "title": policy["label"],
            "category": "refund",
            "source_file": "refund_policy.json",
            "content": _text(policy),
        })

    # ── ticket_types.json — one document per ticket type ──
    for tt in _load("ticket_types.json"):
        docs.append({
            "title": f"Ticket Type — {tt['display_name']}",
            "category": "ticket_type",
            "source_file": "ticket_types.json",
            "content": _text(tt),
        })

    # ── booking_rules.json — split into one document per network/topic ──
    br = _load("booking_rules.json")

    for network in ("national_rail", "metro"):
        if network in br:
            for topic, content in br[network].items():
                docs.append({
                    "title": f"Booking Rule — {network.replace('_', ' ').title()} — {topic.replace('_', ' ').title()}",
                    "category": topic,
                    "source_file": "booking_rules.json",
                    "content": _text({
                        "network": network,
                        "topic": topic,
                        topic: content,
                    }),
                })

    if "general_rules" in br:
        for topic, content in br["general_rules"].items():
            docs.append({
                "title": f"Booking Rule — General — {topic.replace('_', ' ').title()}",
                "category": topic,
                "source_file": "booking_rules.json",
                "content": _text({
                    "section": "general_rules",
                    "topic": topic,
                    topic: content,
                }),
            })

    # ── travel_policies.json — split into one document per network/topic ──
    # This is important for queries like:
    # "I lost my wallet", "Can I bring a pet?", "Can I bring luggage?"
    # If we store the whole metro/national_rail section as one huge document,
    # vector search may retrieve unrelated policies.
    tp = _load("travel_policies.json")

    for network in ("metro", "national_rail"):
        if network in tp:
            for topic, content in tp[network].items():
                docs.append({
                    "title": f"Travel Policy — {network.replace('_', ' ').title()} — {topic.replace('_', ' ').title()}",
                    "category": topic,
                    "source_file": "travel_policies.json",
                    "content": _text({
                        "network": network,
                        "topic": topic,
                        topic: content,
                    }),
                })

    return docs


def seed():
    documents = build_documents()
    print(f"📄 Embedding {len(documents)} policy documents using {llm.chat_provider}...\n")

    for i, doc in enumerate(documents):
        print(f"  [{i + 1}/{len(documents)}] Embedding: {doc['title']}")

        try:
            embedding = llm.embed(doc["content"])

            if len(embedding) != llm.embed_dim:
                print(f"    ⚠️  Unexpected embedding dim: {len(embedding)} (expected {llm.embed_dim})")
                print("    Update GEMINI_EMBED_DIM or OLLAMA_EMBED_DIM in skeleton/config.py")
                sys.exit(1)

            doc_id = store_policy_document(
                title=doc["title"],
                category=doc["category"],
                content=doc["content"],
                embedding=embedding,
                source_file=doc.get("source_file", ""),
            )
            print(f"    ✓ Stored as document id={doc_id}")

        except Exception as e:
            print(f"    ✗ Failed: {e}")
            raise

        if llm.chat_provider == "gemini" and i < len(documents) - 1:
            time.sleep(0.5)

    print(f"\n✅ All {len(documents)} policy documents embedded and stored.")
    print("   Test with a similarity search:")
    print("   >>> from skeleton.llm_provider import llm")
    print("   >>> from databases.relational.queries import query_policy_vector_search")
    print("   >>> results = query_policy_vector_search(llm.embed('I lost my wallet, what should I do?'))")
    print("   >>> print(results[0]['title'])")


if __name__ == "__main__":
    seed()
