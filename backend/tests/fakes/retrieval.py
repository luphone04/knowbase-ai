"""Retrieval test data: chunks with hand-built vectors, inserted directly.

Distances are then exact: e0 is 0 from a query of e0, (e0+e1)/sqrt(2) about
0.29, and e1 exactly 1.
"""

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text

from app.db.session import get_engine
from app.providers.interfaces import QUERY_TASK, EmbeddingResult

MODEL = "gemini-embedding-001"
DIMS = 768


def basis(*weights: tuple[int, float]) -> list[float]:
    vector = [0.0] * DIMS
    for index, weight in weights:
        vector[index] = weight
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


def add_source(
    workspace_id: uuid.UUID,
    name: str,
    chunks: list[tuple[str, list[float]]],
    *,
    status: str = "ready",
    model: str = MODEL,
    page: int | None = None,
) -> list[uuid.UUID]:
    """One file source with one document and the given (content, vector) chunks."""
    source_id, document_id = uuid.uuid4(), uuid.uuid4()
    chunk_ids = [uuid.uuid4() for _ in chunks]
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources (id, workspace_id, kind, display_name, original_filename,"
                " file_type, status) VALUES (:id, :ws, 'file', :name, :name, 'md', :status)"
            ),
            {"id": source_id, "ws": workspace_id, "name": name, "status": status},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, workspace_id, source_id, title, content_hash,"
                " text_content) VALUES (:id, :ws, :src, :title, 'h', 't')"
            ),
            {"id": document_id, "ws": workspace_id, "src": source_id, "title": f"{name} title"},
        )
        for index, ((content, vector), chunk_id) in enumerate(zip(chunks, chunk_ids, strict=True)):
            connection.execute(
                text(
                    "INSERT INTO chunks (id, workspace_id, document_id, chunk_index, content,"
                    " content_hash, embedding, embedding_model, metadata) VALUES (:id, :ws,"
                    " :doc, :i, :content, :hash, CAST(:vec AS vector), :model,"
                    " CAST(:meta AS jsonb))"
                ),
                {
                    "id": chunk_id,
                    "ws": workspace_id,
                    "doc": document_id,
                    "i": index,
                    "content": content,
                    "hash": uuid.uuid4().hex,
                    "vec": literal(vector),
                    "model": model,
                    "meta": (
                        f'{{"heading": "Section {index}", "page_start": {page or "null"},'
                        f' "page_end": {page or "null"}}}'
                    ),
                },
            )
    return chunk_ids


@dataclass
class FixedQueryProvider:
    """Answers every query embedding with `vector`; records the queries it saw."""

    vector: list[float]
    model: str = MODEL
    dimensions: int = DIMS
    queries: list[str] = field(default_factory=list)
    failure: Exception | None = None
    on_call: Callable[[], None] | None = None  # runs inside the "Gemini call"

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        raise AssertionError("retrieval must not embed documents")

    def embed_query(self, text: str) -> EmbeddingResult:
        if self.on_call is not None:
            self.on_call()
        if self.failure is not None:
            raise self.failure
        self.queries.append(text)
        return EmbeddingResult(
            vectors=[self.vector],
            model=self.model,
            dimensions=self.dimensions,
            task_type=QUERY_TASK,
            latency_ms=0,
            requests=1,
            attempts=1,
        )
