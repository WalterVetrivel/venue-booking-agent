"""The RAG index over circulars and reports: text extraction, chunking and hybrid search.

Search combines full-text (Postgres tsvector) and vector similarity (pgvector) with reciprocal rank
fusion, so exact terms like a circular number and paraphrases like "orientation for first years" both
find the right chunks.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.embeddings import Embedder
from backend.models import DocumentChunk, SourceDocument

CHUNK_CHARS = 1200
OVERLAP_CHARS = 200
DOC_NUMBER = re.compile(r"(Circular|Report) No\.?\s*(\S+)\s+(\d{2})\.(\d{2})\.(\d{4})", re.I)


def md5_of(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def pdf_text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()


def doc_date(text_: str) -> date | None:
    """The issue date printed next to the circular or report number."""
    m = DOC_NUMBER.search(text_)
    if not m:
        return None
    try:
        return date(int(m[5]), int(m[4]), int(m[3]))
    except ValueError:
        return None


def doc_title(path: Path) -> str:
    """'CIRCULAR NO.157 - AWARENESS_SESSION_ON_AWS.pdf' -> 'CIRCULAR NO.157 - AWARENESS SESSION ON AWS'."""
    return re.sub(r"_+", " ", path.stem).strip()


def chunk(text_: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split on line boundaries into pieces of about `size` characters, overlapping by about `overlap`."""
    lines = [ln.strip() for ln in text_.splitlines() if ln.strip()]
    chunks, current = [], []
    for line in lines:
        if current and sum(len(x) + 1 for x in current) + len(line) > size:
            chunks.append("\n".join(current))
            kept = []
            while current and sum(len(x) + 1 for x in kept) < overlap:
                kept.insert(0, current.pop())
            current = kept
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def upsert_document(db: Session, path: Path, kind: str, drive_file_id: str | None = None,
                    modified_at: datetime | None = None) -> tuple[SourceDocument, bool]:
    """The source_document row for a file, and whether its content changed since it was last processed."""
    md5 = md5_of(path)
    q = select(SourceDocument).where(SourceDocument.drive_file_id == drive_file_id) if drive_file_id else \
        select(SourceDocument).where(SourceDocument.name == path.name, SourceDocument.kind == kind,
                                     SourceDocument.drive_file_id.is_(None))
    doc = db.scalar(q)
    if doc is None:
        doc = SourceDocument(name=path.name, kind=kind, drive_file_id=drive_file_id)
        db.add(doc)
    changed = doc.md5 != md5 or doc.processed_at is None
    doc.name, doc.md5, doc.modified_at = path.name, md5, modified_at or doc.modified_at
    db.flush()
    return doc, changed


def index_document(db: Session, doc: SourceDocument, text_: str, title: str, embedder: Embedder,
                   category: str | None = None) -> int:
    """Replace the document's chunks. Each chunk starts with the title so it reads well on its own."""
    db.execute(delete(DocumentChunk).where(DocumentChunk.source_document_id == doc.id))
    pieces = [f"{title}\n{c}" for c in chunk(text_)]
    vectors = embedder.passages(pieces) if pieces else []
    issued = doc_date(text_)
    for i, (piece, vec) in enumerate(zip(pieces, vectors)):
        db.add(DocumentChunk(source_document_id=doc.id, chunk_index=i, text=piece, embedding=vec,
                             doc_date=issued, category=category, meta={"title": title, "kind": doc.kind}))
    doc.processed_at, doc.error = datetime.now(timezone.utc), None
    db.flush()
    return len(pieces)


@dataclass
class Hit:
    chunk_id: int
    document: str
    kind: str
    doc_date: date | None
    text: str
    score: float


def search(db: Session, query: str, embedder: Embedder, k: int = 8, kind: str | None = None,
           since: date | None = None) -> list[Hit]:
    """Hybrid search: the best `k` chunks by reciprocal rank fusion of full-text and vector rankings."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    vec = "[" + ",".join(f"{x:.6f}" for x in embedder.query(query)) + "]"
    filters, params = "", {"q": vec, "k": k, "pool": max(50, k * 5)}
    if kind:
        filters += " AND d.kind = :kind"
        params["kind"] = kind
    if since:
        filters += " AND c.doc_date >= :since"
        params["since"] = since
    full_text = ""
    if words:
        params["tsq"] = " | ".join(words)
        full_text = f"""
        UNION ALL
        SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM (
          SELECT c.id, ts_rank_cd(c.tsv, to_tsquery('english', :tsq)) AS rank
          FROM document_chunk c JOIN source_document d ON d.id = c.source_document_id
          WHERE c.tsv @@ to_tsquery('english', :tsq) {filters}
          ORDER BY rank DESC LIMIT :pool) t"""
    rows = db.execute(text(f"""
        WITH ranked AS (
          SELECT id, row_number() OVER (ORDER BY dist) AS r FROM (
            SELECT c.id, c.embedding <=> CAST(:q AS vector) AS dist
            FROM document_chunk c JOIN source_document d ON d.id = c.source_document_id
            WHERE c.embedding IS NOT NULL {filters}
            ORDER BY dist LIMIT :pool) v
          {full_text}
        )
        SELECT c.id, d.name, d.kind, c.doc_date, c.text, s.score
        FROM (SELECT id, sum(1.0 / (60 + r)) AS score FROM ranked GROUP BY id) s
        JOIN document_chunk c ON c.id = s.id JOIN source_document d ON d.id = c.source_document_id
        ORDER BY s.score DESC LIMIT :k"""), params)
    return [Hit(*row) for row in rows]
