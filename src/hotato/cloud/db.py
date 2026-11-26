"""SQLAlchemy 2 models. Every tenant-owned row hangs off a Project, which hangs
off an Organisation; queries reach data only through ``access`` helpers that
join the caller's membership."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .security import new_id


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    pw_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)
    id: Mapped[str] = _pk()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # owner | member


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = _pk()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(10))
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = _pk()
    key: Mapped[str] = mapped_column(String(80), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = _pk()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(16))  # first characters, for recognition
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("project_id", "provider"),)
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # vapi | retell
    secret_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_test_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_detail: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class TestRun(Base):
    __test__ = False
    __tablename__ = "test_runs"
    __table_args__ = (UniqueConstraint("project_id", "run_id"),
                      Index("ix_test_runs_project_started", "project_id", "started_at"))
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(String(200))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    finished_at: Mapped[dt.datetime] = mapped_column(DateTime)
    agent_stack: Mapped[str] = mapped_column(String(100))
    agent_id: Mapped[str] = mapped_column(String(200))
    judge_provider: Mapped[str] = mapped_column(String(100))
    judge_model: Mapped[str] = mapped_column(String(200))
    n_cases: Mapped[int] = mapped_column(Integer)
    pass_rate: Mapped[float] = mapped_column(Float)
    n_pass_all: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class TestCaseRow(Base):
    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("test_run_pk", "case_id"),)
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    test_run_pk: Mapped[str] = mapped_column(ForeignKey("test_runs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    persona: Mapped[str] = mapped_column(Text)
    goal: Mapped[str] = mapped_column(Text)
    repeats: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)
    pass_rate: Mapped[float] = mapped_column(Float)
    pass_all: Mapped[bool] = mapped_column(Boolean)
    criteria: Mapped[Any] = mapped_column(JSON)
    runs: Mapped[Any] = mapped_column(JSON)


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (UniqueConstraint("project_id", "call_id"),
                      Index("ix_calls_project_ts", "project_id", "ts"))
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    call_id: Mapped[str] = mapped_column(String(200))
    stack: Mapped[str] = mapped_column(String(100))
    agent_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)  # started_at, else scored_at
    scored_at: Mapped[dt.datetime] = mapped_column(DateTime)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    incidents: Mapped[Any] = mapped_column(JSON)
    alerts_fired: Mapped[Any] = mapped_column(JSON)
    report_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    n_incidents: Mapped[int] = mapped_column(Integer, default=0)
    incident_types: Mapped[str] = mapped_column(Text, default=",")  # ",a,b," for filtering


class ProjectRules(Base):
    __tablename__ = "project_rules"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    rules: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


def make_engine(url: str):
    kw: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool
    engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


def make_session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)
