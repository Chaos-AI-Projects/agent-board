"""Persistence for the agent board: models, engine setup and migrations.

This is the only module that knows which database engine it is on. The
schema is design section 2 of brain `product-specs/agent-issue-board-design.md`,
kept engine-neutral (no JSONB, no arrays) so one set of Alembic migrations
runs on both SQLite and PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    BigInteger,
    select,
    type_coerce,
    func,
    Boolean,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
    false,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

DATABASE_URL_ENV = "BOARD_DATABASE_URL"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class ConfigError(RuntimeError):
    pass


# --- engine -----------------------------------------------------------------


def normalize_url(url: str | URL) -> URL:
    """Point a bare PostgreSQL URL at psycopg 3, the driver we install.

    SQLAlchemy reads `postgresql://` as psycopg2, and hosted consoles such as
    Cloud SQL hand out that form or `postgres://`.
    """
    url = make_url(url)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg")
    return url


def make_engine(url: str | URL | None = None, **kwargs) -> Engine:
    """Build the engine from `url`, or from BOARD_DATABASE_URL when omitted."""
    if url is None:
        url = os.environ.get(DATABASE_URL_ENV)
        if not url:
            raise ConfigError(f"{DATABASE_URL_ENV} is not set")
    url = normalize_url(url)
    engine = create_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _sqlite_connect)
        event.listen(engine, "begin", _sqlite_begin_immediate)
    return engine


# SQLite is the production engine (MS-626): board-web and an agent's CLI or
# MCP server share one file. Every transaction here opens BEGIN IMMEDIATE,
# reads included, so each one waits up to this long for the lock instead of
# failing "database is locked". pysqlite's own default is 5 s.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _sqlite_connect(dbapi_conn, _record):
    # Hand transaction control to SQLAlchemy, so the "begin" hook below
    # decides how each transaction opens instead of pysqlite.
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    # WAL keeps a reader outside SQLAlchemy, such as the sqlite3 shell or a
    # backup, from blocking a writer. It does not let SQLAlchemy reads skip the
    # queue. The mode is stored in the file, so after the first connect this
    # is a no-op.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def _sqlite_begin_immediate(conn):
    # Take the write lock up front (design section 4). A deferred
    # transaction would read, then fail to upgrade its lock under a
    # concurrent writer. This serialises claims on SQLite, which is why a
    # concurrency test passing here says nothing about PostgreSQL.
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def session(engine: Engine) -> Session:
    """A session whose objects stay readable after commit."""
    return Session(engine, expire_on_commit=False)


def db_now(s: Session) -> datetime:
    """The database clock, in UTC. Callers' clocks may be skewed."""
    return s.scalar(select(type_coerce(func.current_timestamp(), Timestamp())))


# --- migrations -------------------------------------------------------------


def alembic_config(engine: Engine | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if engine is not None:
        cfg.attributes["engine"] = engine
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    command.upgrade(alembic_config(engine), revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    command.downgrade(alembic_config(engine), revision)


# --- models -----------------------------------------------------------------

# Named constraints, so a later migration can drop one by name on either engine.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# SQLite only auto-increments a column declared exactly INTEGER PRIMARY KEY.
EventId = BigInteger().with_variant(Integer(), "sqlite")


class Timestamp(TypeDecorator):
    """An aware datetime, stored and returned in UTC on every engine.

    SQLite has no timezone type: it drops the offset on write and returns a
    naive value, where PostgreSQL returns an aware one. Lease expiry is
    compared against the clock, so both engines must agree. A naive value
    is refused rather than guessed at.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"naive datetime {value!r}: a timezone is required")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Project(Base):
    """One id prefix, e.g. MS, with its own counter."""

    __tablename__ = "project"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    next_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # One of core.BUCKETS hue buckets, fixed at creation (MS-655).
    colour_bucket: Mapped[int | None] = mapped_column(Integer, nullable=True)

    issues: Mapped[list[Issue]] = relationship(back_populates="project")


class Issue(Base):
    __tablename__ = "issue"
    __table_args__ = (
        # A workflow step is a position; two issues cannot share one.
        UniqueConstraint("workflow_id", "position"),
        Index("ix_issue_state_rank", "state", "rank"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_key: Mapped[str] = mapped_column(ForeignKey("project.key"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The state set is configuration (design section 3), so no CHECK here.
    state: Mapped[str] = mapped_column(String(32), default="backlog", server_default="backlog")
    rank: Mapped[int] = mapped_column(Integer)
    assignee: Mapped[str | None] = mapped_column(String(200))
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflow.id"))
    position: Mapped[int | None] = mapped_column(Integer)
    lease_holder: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    # Null under a live lease means a human holds it: no expiry (design section 11).
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp())
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(Timestamp())
    updated_at: Mapped[datetime] = mapped_column(Timestamp())

    project: Mapped[Project] = relationship(back_populates="issues")
    workflow: Mapped[Workflow | None] = relationship(back_populates="steps",
                                                     foreign_keys=[workflow_id])
    # The workflow this issue was broken into, if any (MS-644).
    plan: Mapped[Workflow | None] = relationship(
        foreign_keys="Workflow.origin_issue_id", viewonly=True
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="issue", order_by="Event.id"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="issue", order_by="Artifact.id", cascade="all, delete-orphan"
    )
    labels: Mapped[list[Label]] = relationship(
        back_populates="issue", order_by="Label.name", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="issue", order_by="Attachment.id", cascade="all, delete-orphan"
    )


class Event(Base):
    """Append-only history. Every operation writes at least one."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(EventId, primary_key=True, autoincrement=True)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), index=True)
    at: Mapped[datetime] = mapped_column(Timestamp())
    actor: Mapped[str] = mapped_column(String(200))
    actor_kind: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    # SHA-256 of the call's arguments, beside a caller-supplied key: a request
    # id names one call, so the same id with other arguments is a Conflict.
    request_hash: Mapped[str | None] = mapped_column(String(64))

    issue: Mapped[Issue] = relationship(back_populates="events")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(1000))
    closes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    added_at: Mapped[datetime] = mapped_column(Timestamp())
    added_by: Mapped[str] = mapped_column(String(200))

    issue: Mapped[Issue] = relationship(back_populates="artifacts")


class Attachment(Base):
    """A file's metadata (MS-643). The bytes are on disk, named by `sha256`."""

    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), index=True)
    # Set when the file came with a note, so history shows it under that note.
    event_id: Mapped[int | None] = mapped_column(EventId, ForeignKey("event.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    added_at: Mapped[datetime] = mapped_column(Timestamp())
    added_by: Mapped[str] = mapped_column(String(200))

    issue: Mapped[Issue] = relationship(back_populates="attachments")


class Label(Base):
    __tablename__ = "label"

    issue_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), primary_key=True)

    issue: Mapped[Issue] = relationship(back_populates="labels")


class Dependency(Base):
    """`issue_id` waits until `depends_on_id` is done (MS-646)."""

    __tablename__ = "dependency"

    issue_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), primary_key=True)
    depends_on_id: Mapped[str] = mapped_column(ForeignKey("issue.id"), primary_key=True,
                                               index=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp())
    created_by: Mapped[str] = mapped_column(String(200))


class Workflow(Base):
    """An ordered group of issues. Its state is computed from the steps."""

    __tablename__ = "workflow"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("template.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp())
    archived_at: Mapped[datetime | None] = mapped_column(Timestamp())
    # Set when the workflow is the plan for one issue (MS-644). `use_alter`
    # because issue.workflow_id already points the other way.
    origin_issue_id: Mapped[str | None] = mapped_column(
        ForeignKey("issue.id", use_alter=True), unique=True
    )

    template: Mapped[Template | None] = relationship()
    steps: Mapped[list[Issue]] = relationship(
        back_populates="workflow", order_by="Issue.position",
        foreign_keys="Issue.workflow_id"
    )


class Template(Base):
    __tablename__ = "template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(500))

    steps: Mapped[list[TemplateStep]] = relationship(
        back_populates="template",
        order_by="TemplateStep.position",
        cascade="all, delete-orphan",
    )


class TemplateStep(Base):
    __tablename__ = "template_step"

    template_id: Mapped[int] = mapped_column(ForeignKey("template.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")

    template: Mapped[Template] = relationship(back_populates="steps")


class OAuthClient(Base):
    """A client registered for remote MCP (MS-649), its metadata as JSON."""

    __tablename__ = "oauth_client"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    info: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp())


class OAuthCode(Base):
    """An authorization code, stored as its SHA-256 and deleted when used."""

    __tablename__ = "oauth_code"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_client.client_id"), index=True)
    email: Mapped[str] = mapped_column(String(200))
    scopes: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    redirect_uri: Mapped[str] = mapped_column(Text)
    redirect_uri_explicit: Mapped[bool] = mapped_column(Boolean)
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(Timestamp())


class OAuthToken(Base):
    """An access or refresh token, stored as its SHA-256.

    One grant is the pair a code or a refresh produced; revoking either
    token revokes the grant.
    """

    __tablename__ = "oauth_token"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    grant_id: Mapped[str] = mapped_column(String(64), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_client.client_id"), index=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    scopes: Mapped[str] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(Timestamp())
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp())
