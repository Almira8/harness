from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def now():
    return datetime.now(timezone.utc)


def new_id():
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str] = mapped_column(Text)
    questions: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    script: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    edit: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    render_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    publication: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reference(Base):
    __tablename__ = "references"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[str] = mapped_column(Text, default="")
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    duration: Mapped[float] = mapped_column(default=0.0)
    transcript: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


Index("one_active_job_per_project", Job.project_id, unique=True,
      sqlite_where=Job.status.in_(["queued", "running", "waiting", "attention"]),
      postgresql_where=Job.status.in_(["queued", "running", "waiting", "attention"]))


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30}
    if settings.database_url.startswith("sqlite") else {},
    pool_pre_ping=True,
)
Session = sessionmaker(engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)


@contextmanager
def session():
    with Session.begin() as db:
        yield db


def event(db, project_id, kind, **detail):
    db.add(Event(project_id=project_id, kind=kind, detail=detail))
