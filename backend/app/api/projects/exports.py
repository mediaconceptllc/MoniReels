"""Render, list and delete a project's videos."""
from __future__ import annotations

import hashlib
import json

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import quotas, r2
from app.api.projects.common import project_404, projects_router, require_project
from app.api.projects.guards import require_translation_ready, require_voice_ready
from app.db import get_db
from app.dbmodels import Output
from app.export.ideas import idea_ranges, pick_ideas
from app.jobs import queue
from app.languages import voice_over_on
from app.schemas import ExportSelectionIn, OutputOut
from app.security import Principal, current_user
from app.store import get_row
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = projects_router()


@router.post("/{project_id}/export-all")
def export_all(
    project_id: str,
    body: ExportSelectionIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Render the model's ideas — all of them, or the ones named.

    The body is optional and an omitted one still means everything, so a
    client that predates choosing keeps working unchanged.

    An id that is not in the CURRENT suggestions is refused here rather than
    dropped later: the producer is standing at the button, and "2 of the 3 you
    picked" discovered twenty minutes into a render is the worst place to find
    out.
    """
    project = require_project(db, project_id, principal)
    if project.suggestions is None or not (project.suggestions.shorts or project.suggestions.youtube):
        raise HTTPException(status_code=400, detail="Экспортлох санал алга.")

    pick: dict = {}
    if body and body.shorts is not None:
        known = {s.id for s in project.suggestions.shorts}
        missing = [i for i in body.shorts if i not in known]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"Ийм богино видео алга: {', '.join(missing)}"
            )
        pick["shorts"] = body.shorts
    if body and body.youtube is not None:
        plans = project.suggestions.youtube
        out_of_range = [i for i in body.youtube if not 0 <= i < len(plans)]
        if out_of_range:
            raise HTTPException(
                status_code=422,
                detail=f"YouTube хураангуйн дугаар буруу: {out_of_range}",
            )
        # The title travels with the index so the worker can tell whether the
        # plan it is about to render is still the one that was chosen.
        pick["youtube"] = [{"i": i, "title": plans[i].title} for i in body.youtube]

    if pick and not (pick.get("shorts") or pick.get("youtube")):
        raise HTTPException(status_code=400, detail="Нэг ч санал сонгогдоогүй байна.")

    # The worker's own selection (export.ideas.pick_ideas), so the lines asked
    # about here are exactly the lines it will render.
    wanted, _ = pick_ideas(project.suggestions, {"pick": pick} if pick else {})
    require_translation_ready(project, idea_ranges(wanted))
    require_voice_ready(db, project)

    # The selection is part of the job's identity. Without it, exporting one
    # short and then another while the first is queued would hand back the
    # FIRST job's id and render the wrong thing under the right progress bar.
    digest = hashlib.sha256(
        json.dumps(pick, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12] if pick else "all"
    key = f"export_all:{project_id}:{digest}"
    # A voice-over buys speech; without one an export bills nothing outside.
    quotas.admit_job(db, principal, "export_all", paid=voice_over_on(project), dedupe_key=key)
    return {
        "job_id": queue.enqueue(
            "export_all",
            project_id=project_id,
            payload={"pick": pick} if pick else None,
            dedupe_key=key,
            db=db,
        )
    }


@router.post("/{project_id}/export")
def export_timeline(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = require_project(db, project_id, principal)
    if not project.clips:
        raise HTTPException(status_code=400, detail="Timeline дээр клип алга.")
    require_translation_ready(project, [(c.start, c.end) for c in project.clips])
    require_voice_ready(db, project)
    # Keyed by what the render reads, not by the project alone: re-exporting
    # after changing the clips, the settings or the text is a normal thing to
    # want and gets its own job, while a second click on the SAME render gets
    # the first one back. There was no key at all, and 25 identical exports
    # queued behind one another (SEC-4).
    render = project.model_dump(
        mode="json",
        include={"language", "clips", "transition", "subtitle_style", "export", "transcript"},
    )
    digest = hashlib.sha256(
        json.dumps(render, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    key = f"export:{project_id}:{digest}"
    quotas.admit_job(db, principal, "export", paid=voice_over_on(project), dedupe_key=key)
    return {"job_id": queue.enqueue("export", project_id=project_id, dedupe_key=key, db=db)}


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@router.get("/{project_id}/outputs", response_model=list[OutputOut])
def list_outputs(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[OutputOut]:
    with project_404():
        # Ownership and the outputs; the document stays in Postgres. The
        # project page asks for this beside the project itself, and loading
        # the document twice per page view doubled its cost.
        row = get_row(db, project_id, principal.id, doc=False)

    counts: dict[str, int] = {}
    result: list[OutputOut] = []
    for out in sorted(row.outputs, key=lambda o: o.created_at):
        counts[out.kind] = counts.get(out.kind, 0) + 1
        name = r2.download_name(row.name, out.kind, counts[out.kind], "mp4")
        result.append(
            OutputOut(
                id=out.id,
                kind=out.kind,
                title=out.title,
                duration_sec=out.duration_sec,
                size_bytes=out.size_bytes,
                created_at=out.created_at,
                # Separate URLs: the download one carries a Content-Disposition
                # that makes a browser save instead of play.
                play_url=r2.presign_get(out.r2_key),
                download_url=r2.presign_get(out.r2_key, filename=name),
                srt_url=r2.presign_get(out.srt_key) if out.srt_key else None,
            )
        )
    return result


@router.delete("/{project_id}/outputs/{output_id}")
def delete_output(
    project_id: str,
    output_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with project_404():
        get_row(db, project_id, principal.id, doc=False)
    out = db.get(Output, output_id)
    if out is None or out.project_id != project_id:
        raise HTTPException(status_code=404, detail="Гаралт олдсонгүй.")

    keys = [out.r2_key] + ([out.srt_key] if out.srt_key else [])
    db.delete(out)
    db.commit()
    # Outputs rendered before keys carried the job id could share a key — a
    # later export wrote over the earlier file. Such a key now holds the file
    # of a row that is still listed; removing it would delete that video.
    still_used = set(db.scalars(select(Output.r2_key).where(Output.r2_key.in_(keys)))) | set(
        db.scalars(select(Output.srt_key).where(Output.srt_key.in_(keys)))
    )
    if r2.enabled():
        for key in keys:
            if key in still_used:
                continue
            try:
                r2.delete(key)
            except Exception:  # noqa: BLE001 - the row is gone; storage is best effort
                logger.exception("Failed to delete %s", key)
    return {"deleted": True}
