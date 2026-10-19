"""What an unfinished transcription already bought, kept for the next run.

See app.dbmodels.SttChunk for why. The speech client asks here before it
sends a chunk and records the answer the moment one arrives
(app.stt.duudlaga_client.transcribe_chunk_text), so a run that stops after
31 of 40 chunks leaves 31 answers behind, and the run after it pays for 9.

Only the chunk-by-chunk client uses it. ElevenLabs Scribe is one request
for the whole file: there is no part of it to keep.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import session_scope
from app.dbmodels import SttChunk


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ChunkCache:
    """One project's paid chunks, for one recogniser (`variant`)."""

    def __init__(self, project_id: str, variant: str) -> None:
        self.project_id = project_id
        self.variant = variant[:200]
        self.hits = 0

    def get(self, digest: str) -> str | None:
        with session_scope() as db:
            text = db.scalar(
                select(SttChunk.text).where(
                    SttChunk.project_id == self.project_id,
                    SttChunk.variant == self.variant,
                    SttChunk.digest == digest,
                )
            )
        if text is not None:
            self.hits += 1
        return text

    def put(self, digest: str, text: str) -> None:
        # ON CONFLICT: two identical chunks (two stretches of the same jingle)
        # cut in one run are the same row, and the first answer stands.
        with session_scope() as db:
            db.execute(
                insert(SttChunk)
                .values(project_id=self.project_id, variant=self.variant, digest=digest, text=text)
                .on_conflict_do_nothing()
            )


def clear(db: Session, project_id: str) -> int:
    """Forget a project's chunks — in the transaction that saves the
    transcript holding their text, so neither can land without the other."""
    result = db.execute(delete(SttChunk).where(SttChunk.project_id == project_id))
    return result.rowcount or 0
