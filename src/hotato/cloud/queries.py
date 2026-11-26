"""Read models for the pages. Every function takes a project id that the caller
already proved reachable through ``deps.project_ctx``, and filters on it."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import db as m

DAYS = 14


def overview(db: DbSession, pid: str, now: Optional[dt.datetime] = None) -> dict:
    now = now or m.utcnow()
    d1, d7 = now - dt.timedelta(hours=24), now - dt.timedelta(days=7)

    def count_since(t):
        return db.scalar(select(func.count()).select_from(m.Call)
                         .where(m.Call.project_id == pid, m.Call.ts >= t)) or 0

    avg7 = db.scalar(select(func.avg(m.Call.score))
                     .where(m.Call.project_id == pid, m.Call.ts >= d7))
    types: Counter = Counter()
    rows = db.execute(select(m.Call.incidents).where(
        m.Call.project_id == pid, m.Call.ts >= d7, m.Call.n_incidents > 0).limit(20000)).all()
    for (incs,) in rows:
        for i in incs or []:
            types[i.get("type", "?")] += 1

    start = (now - dt.timedelta(days=DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    per_day = [[0, 0.0, 0] for _ in range(DAYS)]  # count, score sum, score n
    for ts, score in db.execute(select(m.Call.ts, m.Call.score).where(
            m.Call.project_id == pid, m.Call.ts >= start).limit(100000)):
        idx = (ts - start).days
        if 0 <= idx < DAYS:
            per_day[idx][0] += 1
            if score is not None:
                per_day[idx][1] += score
                per_day[idx][2] += 1
    runs = db.scalars(select(m.TestRun).where(m.TestRun.project_id == pid)
                      .order_by(m.TestRun.started_at.desc()).limit(20)).all()
    runs.reverse()
    return {
        "calls_24h": count_since(d1), "calls_7d": count_since(d7),
        "avg_score_7d": avg7,
        "incidents_by_type": types.most_common(),
        "calls_per_day": [d[0] for d in per_day],
        "score_per_day": [(d[1] / d[2]) if d[2] else None for d in per_day],
        "latest_run": runs[-1] if runs else None,
        "run_trend": [r.pass_rate for r in runs],
        "total_calls": db.scalar(select(func.count()).select_from(m.Call)
                                 .where(m.Call.project_id == pid)) or 0,
    }


SINCE = {"24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7),
         "30d": dt.timedelta(days=30), "all": None}
PAGE = 50


def calls_page(db: DbSession, pid: str, *, agent=None, since="7d", incident_type=None,
               only_incidents=False, score_below: Optional[float] = None, page=1):
    q = select(m.Call).where(m.Call.project_id == pid)
    span = SINCE.get(since, SINCE["7d"])
    if span is not None:
        q = q.where(m.Call.ts >= m.utcnow() - span)
    if agent:
        q = q.where(m.Call.agent_id == agent)
    if only_incidents:
        q = q.where(m.Call.n_incidents > 0)
    if incident_type:
        q = q.where(m.Call.incident_types.contains(f",{incident_type},", autoescape=True))
    if score_below is not None:
        q = q.where(m.Call.score < score_below)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(m.Call.ts.desc()).offset((page - 1) * PAGE).limit(PAGE)).all()
    return rows, total


def call_filters(db: DbSession, pid: str):
    agents = [a for (a,) in db.execute(select(m.Call.agent_id).where(
        m.Call.project_id == pid, m.Call.agent_id.is_not(None)).distinct().limit(200))]
    return sorted(agents)
