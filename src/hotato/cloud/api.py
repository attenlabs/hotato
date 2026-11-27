"""Machine API: authenticated by a per-project bearer token, no cookies, so no
CSRF surface. Ingest only reads/writes the token's own project."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from . import db as m
from . import schemas
from .deps import get_db, token_project

router = APIRouter(prefix="/api/v1/projects/{pid}")


async def _json_body(request: Request):
    limit = request.app.state.settings.max_body_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"body larger than {limit} bytes")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise HTTPException(413, f"body larger than {limit} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "body is not valid JSON")


def _bad(exc: schemas.SchemaError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": "invalid_artifact", "detail": str(exc)},
                        status_code=422)


@router.post("/testruns", status_code=201)
async def ingest_testrun(pid: str, request: Request, db: DbSession = Depends(get_db)):
    project = token_project(request, pid, db)  # auth before reading the body
    body = await _json_body(request)
    try:
        run = schemas.validate_testrun(body)
    except schemas.SchemaError as exc:
        return _bad(exc)
    old = db.scalar(select(m.TestRun).where(
        m.TestRun.project_id == project.id, m.TestRun.run_id == run["run_id"]))
    if old is not None:
        db.execute(delete(m.TestCaseRow).where(m.TestCaseRow.test_run_pk == old.id))
        db.delete(old)
        db.flush()
    tr = m.TestRun(
        project_id=project.id, run_id=run["run_id"], started_at=run["started_at"],
        finished_at=run["finished_at"], agent_stack=run["agent"]["stack"],
        agent_id=run["agent"]["id"], judge_provider=run["judge"]["provider"],
        judge_model=run["judge"]["model"], n_cases=run["summary"]["cases"],
        pass_rate=run["summary"]["pass_rate"], n_pass_all=run["summary"]["pass_all"])
    db.add(tr)
    db.flush()
    for c in run["cases"]:
        db.add(m.TestCaseRow(
            project_id=project.id, test_run_pk=tr.id, case_id=c["case_id"], name=c["name"],
            persona=c["persona"], goal=c["goal"], repeats=c["repeats"], passed=c["passed"],
            pass_rate=c["pass_rate"], pass_all=c["pass_all"], criteria=c["criteria"],
            runs=c["runs"]))
    db.commit()
    return {"ok": True, "id": tr.id, "run_id": tr.run_id, "replaced": old is not None,
            "cases": len(run["cases"])}


@router.post("/events", status_code=201)
async def ingest_events(pid: str, request: Request, db: DbSession = Depends(get_db)):
    project = token_project(request, pid, db)
    body = await _json_body(request)
    try:
        events = schemas.validate_events(body)
    except schemas.SchemaError as exc:
        return _bad(exc)
    created = updated = 0
    for ev in events:  # all validated above: a bad event never leaves a partial write
        row = db.scalar(select(m.Call).where(
            m.Call.project_id == project.id, m.Call.call_id == ev["call_id"]))
        if row is None:
            row = m.Call(project_id=project.id, call_id=ev["call_id"])
            db.add(row)
            created += 1
        else:
            updated += 1
        row.stack, row.agent_id = ev["stack"], ev["agent_id"]
        row.started_at, row.scored_at = ev["started_at"], ev["scored_at"]
        row.ts = ev["started_at"] or ev["scored_at"]
        row.duration_s, row.score = ev["duration_s"], ev["score"]
        row.incidents, row.alerts_fired = ev["incidents"], ev["alerts_fired"]
        row.report_path = ev["report_path"]
        row.n_incidents = len(ev["incidents"])
        types = sorted({i["type"] for i in ev["incidents"]})
        row.incident_types = "," + ",".join(types) + "," if types else ","
        db.flush()
    db.commit()
    return {"ok": True, "created": created, "updated": updated}


@router.get("/rules")
def get_rules(pid: str, request: Request, db: DbSession = Depends(get_db)):
    project = token_project(request, pid, db)
    row = db.get(m.ProjectRules, project.id)
    return JSONResponse(row.rules if row else [], headers={"Cache-Control": "no-store"})
