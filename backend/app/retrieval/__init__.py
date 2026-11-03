"""Retrieval (SPEC.md §9): vector search, PostgreSQL full-text search, and RRF.

Two modes are kept separate so they can be compared: `vector` (pgvector
cosine only) and `hybrid` (vector + full-text, fused with Reciprocal Rank
Fusion). Every query is scoped to one workspace.
"""
