"""Habitor — a social habit tracker with verified check-ins, friends and friendly competition.

One module on purpose: config, database models, auth/security, game
orchestration (the rules themselves live in game.py), HTTP routes, the
background scheduler and the demo seeder.

Run:  python main.py            (creates DB, uploads dir and secret key)
      python main.py --seed     (demo players, party, boss battle…)
"""

import argparse
import base64
import io
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import sys
import threading
import time as _time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, available_timezones

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, pass_context, select_autoescape
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field as PField, NaiveDatetime, ValidationError, field_validator
from sqlalchemy import Index, UniqueConstraint, event, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, col, create_engine, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

import game

# ===========================================================================
# Configuration (env vars with safe defaults)
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_bool("DEBUG")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///habitor.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5").strip()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@habitor.local")
SECRET_KEY_FILE = Path(os.getenv("SECRET_KEY_FILE", ".habitor_secret"))
DISABLE_SCHEDULER = _env_bool("DISABLE_SCHEDULER")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PHOTO_EDGE = 1280
SESSION_COOKIE = "habitor_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
TIMER_MAX_GAP_S = 12 * 60
MAX_PARTY_SIZE = 20
CHILL_DAYS_PER_MONTH = 14
TZ_CHANGE_COOLDOWN = timedelta(days=7)       # one timezone change per week
TZ_CHANGE_GUARD = timedelta(hours=48)        # right after a change…
MIN_GAP_AFTER_TZ_CHANGE = timedelta(hours=20)  # …the same quest needs ~a real day between check-ins
AI_CONFIDENCE_MIN = 0.6
STARTING_COINS = 100

Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard


def _load_secret_key() -> str:
    """SECRET_KEY env wins; otherwise create a key file once (race-safe across workers)."""
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    try:
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):
            key = SECRET_KEY_FILE.read_text().strip()
            if key:
                return key
            _time.sleep(0.05)
        raise RuntimeError(f"Secret key file {SECRET_KEY_FILE} is empty")
    key = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


SECRET_KEY = _load_secret_key()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ===========================================================================
# Structured logging
# ===========================================================================


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, handlers=[_handler], force=True)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
for _name in ("httpx", "httpx2", "asyncio", "multipart", "PIL"):
    logging.getLogger(_name).setLevel(logging.WARNING)
log = logging.getLogger("habitor")


_SECRET_PATH = re.compile(r"/(qr|nfc|invite|party/join)/[^/?#\s]+")


def redact_path(path: str) -> str:
    """Hide QR/NFC checkpoint tokens and invite codes — they are bearer secrets and must not land in logs."""
    return _SECRET_PATH.sub(lambda m: f"/{m.group(1)}/[redacted]", path)


class _RedactAccessLog(logging.Filter):
    """uvicorn's access log passes (client, method, path, http_version, status) as args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = redact_path(args[2])
            record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactAccessLog())


def log_event(msg: str, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, msg, extra={"fields": fields})


# ===========================================================================
# Database
# ===========================================================================


def _sqlalchemy_url(url: str) -> str:
    """Accept Heroku/Render-style postgres URLs and route them to psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


IS_SQLITE = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    _sqlalchemy_url(DATABASE_URL),
    connect_args={"check_same_thread": False, "timeout": 30} if IS_SQLITE else {},
    pool_pre_ping=True,
)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


def utcnow() -> datetime:
    """Naive UTC timestamp — every datetime column stores UTC (fields are typed NaiveDatetime)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(max_length=20, unique=True, index=True)
    email: str = Field(max_length=254, unique=True, index=True)
    password_hash: str
    bio: str = Field(default="", max_length=160)
    avatar_color: str = game.DEFAULT_AVATAR_COLOR
    avatar_hat: str = "none"
    xp: int = 0
    coins: int = STARTING_COINS
    shields: int = 0
    tz: str = "UTC"
    chill_on: bool = False
    chill_month: str = ""
    chill_days_used: int = 0
    global_opt_in: bool = False
    invite_code: str = Field(unique=True, index=True)
    prove_it_date: Optional[date] = None
    prove_it_until: Optional[NaiveDatetime] = None
    last_rollover: Optional[date] = None
    tz_changed_at: Optional[NaiveDatetime] = None  # timezone changes are rate-limited (anti-cheat)
    push_subscription: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Friendship(SQLModel, table=True):
    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("requester_id", "addressee_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    requester_id: int = Field(foreign_key="users.id", index=True)
    addressee_id: int = Field(foreign_key="users.id", index=True)
    status: str = Field(default="pending", index=True)  # pending | accepted | declined
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Party(SQLModel, table=True):
    __tablename__ = "parties"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=30)
    owner_id: int = Field(foreign_key="users.id")
    invite_code: str = Field(unique=True, index=True)
    tier: str = "Bronze"
    pet_name: str = "Pixel"
    pet_points: int = 0
    pet_health: int = 80
    pet_last_day: Optional[date] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class PartyMember(SQLModel, table=True):
    __tablename__ = "party_members"
    __table_args__ = (UniqueConstraint("party_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    party_id: int = Field(foreign_key="parties.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    joined_at: NaiveDatetime = Field(default_factory=utcnow)


class Habit(SQLModel, table=True):
    __tablename__ = "habits"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=40)
    category: str = "Custom"
    icon: str = "⭐"
    frequency: str = "daily"
    times_per_week: int = 1
    days_csv: str = ""
    difficulty: str = "normal"
    reminder_time: str = ""
    proof_methods: str = "photo"
    min_minutes: int = 10
    tiny_version: str = ""
    stack_after_id: Optional[int] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    gps_label: str = ""
    qr_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(8))
    current_streak: int = 0
    best_streak: int = 0
    archived: bool = Field(default=False, index=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow)

    @property
    def methods(self) -> list[str]:
        return [m for m in self.proof_methods.split(",") if m in game.PROOF_METHODS]

    @property
    def schedule(self) -> game.Schedule:
        return game.Schedule.from_fields(self.frequency, self.times_per_week, self.days_csv)

    @property
    def has_location(self) -> bool:
        return game.valid_coords(self.gps_lat, self.gps_lon)


class CheckIn(SQLModel, table=True):
    __tablename__ = "checkins"
    # One check-in per habit per scheduled slot: the DB enforces idempotency.
    __table_args__ = (UniqueConstraint("habit_id", "slot"), Index("ix_checkins_user_day", "user_id", "local_date"))
    id: Optional[int] = Field(default=None, primary_key=True)
    habit_id: int = Field(foreign_key="habits.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    slot: str
    local_date: date = Field(index=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    status: str = Field(default="pending", index=True)  # pending | verified | flagged | rejected
    methods: str = ""
    verified_by: str = ""
    photo_path: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None
    ai_match: Optional[bool] = None
    ai_confidence: Optional[float] = None
    ai_reason: str = ""
    timer_minutes: Optional[int] = None
    streak: int = 0
    points: int = 0
    prove_it: bool = False
    clapback: bool = False
    mood: Optional[int] = None
    energy: Optional[int] = None
    note: str = Field(default="", max_length=200)
    reprove_deadline: Optional[NaiveDatetime] = None


class PointEvent(SQLModel, table=True):
    """Ledger of every point change. ``ref`` is unique, so a scoring action can never apply twice."""
    __tablename__ = "point_events"
    __table_args__ = (Index("ix_points_user_day", "user_id", "local_date"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    amount: int
    kind: str
    ref: str = Field(unique=True, index=True)
    local_date: date = Field(index=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Reaction(SQLModel, table=True):
    __tablename__ = "reactions"
    __table_args__ = (UniqueConstraint("checkin_id", "user_id", "emoji"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    checkin_id: int = Field(foreign_key="checkins.id", index=True)
    user_id: int = Field(foreign_key="users.id")
    emoji: str


class Vote(SQLModel, table=True):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("checkin_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    checkin_id: int = Field(foreign_key="checkins.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    value: str  # legit | sus
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Taunt(SQLModel, table=True):
    __tablename__ = "taunts"
    __table_args__ = (Index("ix_taunts_sender_day", "sender_id", "sender_day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    sender_id: int = Field(foreign_key="users.id")
    target_id: int = Field(foreign_key="users.id", index=True)
    key: str
    sender_day: date
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    seen: bool = False
    resolved: bool = Field(default=False, index=True)
    clapped: bool = False


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    kind: str
    text: str
    link: str = "/"
    read: bool = Field(default=False, index=True)
    announced: bool = False
    dedupe: Optional[str] = Field(default=None, unique=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)


class UserBadge(SQLModel, table=True):
    __tablename__ = "user_badges"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    key: str
    earned_at: NaiveDatetime = Field(default_factory=utcnow)
    expires_at: Optional[NaiveDatetime] = None


class DayMark(SQLModel, table=True):
    """What happened on a scheduled day without a check-in: shield | chill | miss."""
    __tablename__ = "day_marks"
    __table_args__ = (UniqueConstraint("habit_id", "day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    habit_id: int = Field(foreign_key="habits.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    day: date = Field(index=True)
    kind: str
    streak_lost: int = 0


class ChillDay(SQLModel, table=True):
    __tablename__ = "chill_days"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    day: date


class Duel(SQLModel, table=True):
    __tablename__ = "duels"
    id: Optional[int] = Field(default=None, primary_key=True)
    challenger_id: int = Field(foreign_key="users.id", index=True)
    opponent_id: int = Field(foreign_key="users.id", index=True)
    days: int = 7
    status: str = Field(default="pending", index=True)  # pending | active | finished | declined
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    winner_id: Optional[int] = None
    dare_key: str = ""
    dare_done: bool = False
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class BossBattle(SQLModel, table=True):
    __tablename__ = "boss_battles"
    __table_args__ = (UniqueConstraint("party_id", "week_start"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    party_id: int = Field(foreign_key="parties.id", index=True)
    week_start: date
    boss_key: str
    hp_max: int
    hp: int
    status: str = Field(default="active", index=True)  # active | defeated | failed
    defeated_at: Optional[NaiveDatetime] = None


class BossHit(SQLModel, table=True):
    __tablename__ = "boss_hits"
    __table_args__ = (UniqueConstraint("boss_id", "checkin_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    boss_id: int = Field(foreign_key="boss_battles.id", index=True)
    user_id: int = Field(foreign_key="users.id")
    checkin_id: int = Field(foreign_key="checkins.id", index=True)
    damage: int
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class DareClaim(SQLModel, table=True):
    __tablename__ = "dare_claims"
    __table_args__ = (UniqueConstraint("party_id", "user_id", "day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    party_id: int = Field(foreign_key="parties.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    day: date
    dare: str
    photo_path: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Challenge(SQLModel, table=True):
    """Coin Stake Pot: virtual coins only — never real money."""
    __tablename__ = "challenges"
    id: Optional[int] = Field(default=None, primary_key=True)
    party_id: int = Field(foreign_key="parties.id", index=True)
    creator_id: int = Field(foreign_key="users.id")
    name: str = Field(max_length=40)
    start_date: date
    end_date: date
    stake: int = 0
    required: int = 1
    pot: int = 0
    status: str = Field(default="open", index=True)  # open | active | settled
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class ChallengeMember(SQLModel, table=True):
    __tablename__ = "challenge_members"
    __table_args__ = (UniqueConstraint("challenge_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    challenge_id: int = Field(foreign_key="challenges.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    finished: bool = False
    payout: int = 0


class Tile(SQLModel, table=True):
    __tablename__ = "tiles"
    __table_args__ = (UniqueConstraint("tx", "ty"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tx: int
    ty: int
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    captured_at: NaiveDatetime = Field(default_factory=utcnow)


class TileCount(SQLModel, table=True):
    __tablename__ = "tile_counts"
    __table_args__ = (UniqueConstraint("tx", "ty", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tx: int
    ty: int
    user_id: int = Field(foreign_key="users.id", index=True)
    count: int = 0


class SeasonResult(SQLModel, table=True):
    __tablename__ = "season_results"
    __table_args__ = (UniqueConstraint("season_key", "party_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    season_key: str = Field(index=True)
    party_id: int = Field(foreign_key="parties.id")
    score: float = 0
    old_tier: str
    new_tier: str


class FeedEvent(SQLModel, table=True):
    __tablename__ = "feed_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    party_id: Optional[int] = Field(default=None, foreign_key="parties.id", index=True)
    kind: str
    icon: str = "📣"
    text: str
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)


class TimerSession(SQLModel, table=True):
    __tablename__ = "timer_sessions"
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    habit_id: int = Field(foreign_key="habits.id")
    started_at: NaiveDatetime = Field(default_factory=utcnow)
    last_ping_at: NaiveDatetime = Field(default_factory=utcnow)
    max_gap_s: int = 0
    prompts: int = 0
    used: bool = False


class CoachTip(SQLModel, table=True):
    __tablename__ = "coach_tips"
    __table_args__ = (UniqueConstraint("user_id", "week_start"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    week_start: date
    text: str
    source: str = "rules"  # rules | ai


class JobLock(SQLModel, table=True):
    """Leader lock so only one uvicorn worker runs the scheduler jobs at a time."""
    __tablename__ = "job_locks"
    name: str = Field(primary_key=True)
    holder: str
    expires_at: NaiveDatetime


def init_db() -> None:
    """Create tables and indexes. Retries because several workers may race on first boot."""
    from sqlalchemy.exc import OperationalError, ProgrammingError

    for attempt in range(5):
        try:
            SQLModel.metadata.create_all(engine)
            return
        except (OperationalError, ProgrammingError, IntegrityError):
            if attempt == 4:
                raise
            _time.sleep(0.3 * (attempt + 1))


def migrate_palette() -> None:
    """Move avatar colours saved under the old neon palette onto the matte palette (idempotent)."""
    with Session(engine) as db:
        users = db.exec(select(User).where(col(User.avatar_color).in_(list(game.LEGACY_COLORS)))).all()
        for u in users:
            u.avatar_color = game.LEGACY_COLORS[u.avatar_color]
            db.add(u)
        if users:
            try:
                db.commit()
            except Exception:  # another worker got there first
                db.rollback()


def migrate_columns() -> None:
    """Add columns introduced after a database was created (create_all never alters existing tables)."""
    from sqlalchemy import inspect, text

    wanted = {("users", "tz_changed_at"): "TIMESTAMP"}
    try:
        inspector = inspect(engine)
        for (table, column), sql_type in wanted.items():
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
    except Exception:  # another worker may have added it first
        log.warning("column migration skipped", exc_info=True)


init_db()
migrate_columns()
migrate_palette()

# ===========================================================================
# Security: passwords, sessions, CSRF, rate limiting, headers
# ===========================================================================

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


session_signer = URLSafeTimedSerializer(SECRET_KEY, salt="habitor-session")
qr_signer = URLSafeSerializer(SECRET_KEY, salt="habitor-qr")
nfc_signer = URLSafeSerializer(SECRET_KEY, salt="habitor-nfc")
CHECKPOINT_SIGNERS = {"qr": qr_signer, "nfc": nfc_signer}


class RateLimiter:
    """Sliding-window in-memory limiter (per process — fine for small deployments)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = _time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()

CDNS = "https://unpkg.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com"
CSP = "; ".join([
    "default-src 'self'",
    f"script-src 'self' {CDNS}",
    f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com {CDNS}",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob: https://*.tile.openstreetmap.org",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class CoreMiddleware(BaseHTTPMiddleware):
    """Signed-cookie session, request logging and security headers in one pass."""

    async def dispatch(self, request: Request, call_next):
        started = _time.perf_counter()
        data: dict[str, Any] = {}
        raw = request.cookies.get(SESSION_COOKIE)
        if raw:
            try:
                data = session_signer.loads(raw, max_age=SESSION_MAX_AGE)
            except BadSignature:
                data = {}
        dirty = False
        if "csrf" not in data:
            data["csrf"] = secrets.token_urlsafe(32)
            dirty = True
        request.state.session = data
        request.state.session_dirty = dirty
        response = await call_next(request)
        if request.state.session_dirty:
            response.set_cookie(SESSION_COOKIE, session_signer.dumps(request.state.session), max_age=SESSION_MAX_AGE,
                                httponly=True, samesite="lax", secure=not DEBUG, path="/")
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
        if not DEBUG and request.url.scheme == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path not in ("/health",) and not request.url.path.startswith("/static"):
            log_event("request", method=request.method, path=redact_path(request.url.path), status=response.status_code,
                      ms=round((_time.perf_counter() - started) * 1000, 1))
        return response


def session(request: Request) -> dict[str, Any]:
    return request.state.session


def touch_session(request: Request) -> None:
    request.state.session_dirty = True


def csrf_token(request: Request) -> str:
    return session(request).get("csrf", "")


async def csrf_protect(request: Request) -> None:
    """Global dependency: every state-changing request must echo the session's CSRF token."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    expected = csrf_token(request)
    token = request.headers.get("x-csrf-token")
    if not token and "form" in request.headers.get("content-type", ""):
        token = (await request.form()).get("csrf_token")
    if not expected or not isinstance(token, str) or not secrets.compare_digest(token, expected):
        raise StarletteHTTPException(403, "CSRF token missing or invalid — refresh and try again")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(key: str, limit: int, window_s: float) -> None:
    if not limiter.allow(key, limit, window_s):
        raise GameError("Too many attempts. Please wait a moment and try again.", 429)


# ===========================================================================
# Errors and effects
# ===========================================================================


class GameError(Exception):
    """A rule violation shown to the player as an error toast."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class LoginRequired(Exception):
    pass


class Fx:
    """Client-side effects (toasts, sounds, confetti…) delivered via HX-Trigger or the next page load."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.toasts: list[dict[str, str]] = []

    def toast(self, text: str, kind: str = "good") -> "Fx":
        self.toasts.append({"text": text, "kind": kind})
        return self

    def sfx(self, name: str) -> "Fx":
        self.data["sfx"] = name
        return self

    def set(self, key: str, value: Any = True) -> "Fx":
        self.data[key] = value
        return self

    def merge(self, other: Optional[dict[str, Any]]) -> "Fx":
        if other:
            self.toasts.extend(other.get("toasts", []))
            self.data.update({k: v for k, v in other.items() if k != "toasts"})
        return self

    def payload(self) -> dict[str, Any]:
        out = dict(self.data)
        if self.toasts:
            out["toasts"] = self.toasts
        return out


def is_hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def deliver(request: Request, response: Response, fx: Optional[Fx]) -> Response:
    """Attach effects: HX-Trigger for htmx requests, the session for full page loads."""
    if fx and fx.payload():
        if is_hx(request):
            response.headers["HX-Trigger"] = json.dumps({"habitor": fx.payload()}, ensure_ascii=True)
        else:
            pending = Fx().merge(session(request).get("fx")).merge(fx.payload())
            session(request)["fx"] = pending.payload()
            touch_session(request)
    return response


def safe_back(request: Request, fallback: str = "/") -> str:
    ref = request.headers.get("referer", "")
    if ref:
        parsed = urlparse(ref)
        if parsed.netloc == request.url.netloc and parsed.path.startswith("/"):
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return fallback


def safe_next(value: Optional[str]) -> str:
    """Only allow plain same-site paths after login (blocks //evil.com, /\\evil.com, schemes, control chars)."""
    v = (value or "").strip()
    if (not v.startswith("/") or v.startswith("//") or "\\" in v
            or any(ord(c) < 32 or ord(c) == 127 for c in v)):
        return "/"
    parsed = urlparse(v)
    if parsed.scheme or parsed.netloc:
        return "/"
    return v


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ===========================================================================
# Templates
# ===========================================================================

jinja = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"], default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _static_version() -> str:
    stamp = 0.0
    for name in ("style.css", "app.js", "sw.js"):
        p = BASE_DIR / "static" / name
        if p.exists():
            stamp = max(stamp, p.stat().st_mtime)
    return str(int(stamp))


STATIC_V = _static_version()


def avatar_url(user: User) -> str:
    return f"/avatar/{quote(user.username)}.svg?v={quote(user.avatar_color + user.avatar_hat)}"


def _ago(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    secs = max(0, int((utcnow() - dt).total_seconds()))
    if secs < 60:
        return "Just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


@pass_context
def _localtime(ctx, dt: Optional[datetime], fmt: str = "%H:%M") -> str:
    if not dt:
        return ""
    me = ctx.get("me")
    return to_local(dt, me).strftime(fmt) if me else dt.strftime(fmt)


jinja.filters.update(ago=_ago, localtime=_localtime, num=lambda n: f"{int(n or 0):,}")
jinja.globals.update(
    game=game, avatar_url=avatar_url, BASE_URL=BASE_URL, VAPID_PUBLIC_KEY=VAPID_PUBLIC_KEY,
    AI_ENABLED=bool(ANTHROPIC_API_KEY), STATIC_V=STATIC_V,
)
_partials: dict[str, Any] = {}


def render_partial(request: Request, name: str, **kw: Any) -> str:
    """Render one macro from partials.html (with the request context: me, csrf…)."""
    if not name.isidentifier():
        raise ValueError(name)
    tpl = _partials.get(name)
    if tpl is None or DEBUG:
        tpl = jinja.from_string("{% import 'partials.html' as p with context %}{{ p." + name + "(**kw) }}")
        _partials[name] = tpl
    return tpl.render(request=request, me=getattr(request.state, "user", None), csrf=csrf_token(request), kw=kw)


# Colour themes: key → (display name, description, browser-bar colour, swatch colours for the picker).
THEMES: dict[str, tuple[str, str, str, tuple[str, str, str]]] = {
    "trail": ("Trail", "Brown and off-white with mustard", "#f3ece0", ("#5a3d2b", "#e0a526", "#fffaf2")),
    "blaze": ("Blaze", "Brick red and yellow", "#f7efe9", ("#b8402c", "#e9a91f", "#fffaf6")),
    "court": ("Court", "Clean white with yellow", "#f6f5f0", ("#2b2b28", "#f0c419", "#ffffff")),
}
DEFAULT_THEME = "trail"
THEME_COOKIE = "habitor_theme"


def current_theme(request: Request) -> str:
    value = request.cookies.get(THEME_COOKIE, "")
    return value if value in THEMES else DEFAULT_THEME


def render(request: Request, page: str, *, status_code: int = 200, fx: Optional[Fx] = None, **ctx: Any) -> HTMLResponse:
    me: Optional[User] = getattr(request.state, "user", None)
    db: Optional[Session] = getattr(request.state, "db", None)
    pending = Fx().merge(session(request).pop("fx", None))
    if pending.payload():
        touch_session(request)
    if fx:
        pending.merge(fx.payload())
    base: dict[str, Any] = {
        "request": request, "page": page, "me": me, "csrf": csrf_token(request),
        "needs": ctx.pop("needs", []), "fx": pending.payload() or None,
        "hud": hud_data(db, me) if (me and db) else None,
        "theme": current_theme(request), "themes": THEMES,
    }
    html = jinja.get_template("pages.html").render(**base, **ctx)
    return HTMLResponse(html, status_code=status_code)


# ===========================================================================
# Request dependencies
# ===========================================================================


def get_db(request: Request):
    with Session(engine) as db:
        request.state.db = db
        yield db


DB = Annotated[Session, Depends(get_db)]


def optional_user(request: Request, db: DB) -> Optional[User]:
    uid = session(request).get("uid")
    user = db.get(User, uid) if isinstance(uid, int) else None
    request.state.user = user
    return user


def require_user(user: Annotated[Optional[User], Depends(optional_user)]) -> User:
    if user is None:
        raise LoginRequired()
    return user


Me = Annotated[User, Depends(require_user)]
MaybeMe = Annotated[Optional[User], Depends(optional_user)]

# ===========================================================================
# Time helpers
# ===========================================================================


def zone(user: Optional[User]) -> ZoneInfo:
    try:
        return ZoneInfo(user.tz if user else "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_now(user: User) -> datetime:
    return datetime.now(timezone.utc).astimezone(zone(user))


def local_today(user: User) -> date:
    return local_now(user).date()


def to_local(dt: datetime, user: Optional[User]) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(zone(user))


def local_date_of(dt: datetime, user: User) -> date:
    return to_local(dt, user).date()


_TIMEZONES = sorted(available_timezones())

# ===========================================================================
# Social helpers
# ===========================================================================


def friend_ids(db: Session, uid: int) -> set[int]:
    rows = db.exec(select(Friendship).where(Friendship.status == "accepted", or_(
        Friendship.requester_id == uid, Friendship.addressee_id == uid))).all()
    return {r.addressee_id if r.requester_id == uid else r.requester_id for r in rows}


def friendship_between(db: Session, a: int, b: int) -> Optional[Friendship]:
    return db.exec(select(Friendship).where(or_(
        (Friendship.requester_id == a) & (Friendship.addressee_id == b),
        (Friendship.requester_id == b) & (Friendship.addressee_id == a)))).first()


def are_friends(db: Session, a: int, b: int) -> bool:
    f = friendship_between(db, a, b)
    return bool(f and f.status == "accepted")


def make_friends(db: Session, a: User, b: User) -> None:
    f = friendship_between(db, a.id, b.id)
    if f is None:
        db.add(Friendship(requester_id=a.id, addressee_id=b.id, status="accepted"))
    else:
        f.status = "accepted"
        db.add(f)


def user_party_ids(db: Session, uid: int) -> list[int]:
    return list(db.exec(select(PartyMember.party_id).where(PartyMember.user_id == uid)).all())


def party_member_ids(db: Session, pid: int) -> list[int]:
    return list(db.exec(select(PartyMember.user_id).where(PartyMember.party_id == pid)).all())


def users_by_ids(db: Session, ids) -> dict[int, User]:
    ids = list(ids)
    if not ids:
        return {}
    return {u.id: u for u in db.exec(select(User).where(col(User.id).in_(ids))).all()}


def can_see(db: Session, viewer: User, owner_id: int) -> bool:
    if viewer.id == owner_id or are_friends(db, viewer.id, owner_id):
        return True
    return bool(set(user_party_ids(db, viewer.id)) & set(user_party_ids(db, owner_id)))


def get_user_by_name(db: Session, username: str) -> User:
    user = db.exec(select(User).where(func.lower(User.username) == username.lower())).first()
    if not user:
        raise StarletteHTTPException(404, "Player not found")
    return user


def new_code(n: int = 6) -> str:
    return secrets.token_urlsafe(n).replace("-", "x").replace("_", "y")


# ===========================================================================
# Notifications and web push
# ===========================================================================

try:  # pywebpush is optional
    from pywebpush import WebPushException, webpush
    PUSH_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install
    PUSH_AVAILABLE = False
PUSH_ENABLED = PUSH_AVAILABLE and bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


# Browsers' official push services. Subscriptions pointing anywhere else are refused, so the server can't be
# tricked into making requests to internal hosts (SSRF).
PUSH_SERVICE_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
                      "web.push.apple.com", "notify.windows.com")


def allowed_push_endpoint(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (parsed.scheme == "https" and port in (None, 443) and not parsed.username and not parsed.password
            and any(host == h or host.endswith("." + h) for h in PUSH_SERVICE_HOSTS))


def _send_push(user_id: int, subscription: str, payload: str) -> None:
    try:
        if not allowed_push_endpoint(json.loads(subscription).get("endpoint", "")):
            log_event("push_endpoint_refused", logging.WARNING, user_id=user_id)
            return
        webpush(subscription_info=json.loads(subscription), data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT}, ttl=3600)
    except WebPushException as exc:  # expired subscriptions are dropped
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            with Session(engine) as db:
                u = db.get(User, user_id)
                if u:
                    u.push_subscription = None
                    db.add(u)
                    db.commit()
        log_event("push_failed", logging.WARNING, user_id=user_id, status=status)
    except Exception:
        log.exception("push error")


def notify(db: Session, user_id: int, kind: str, text: str, link: str = "/",
           dedupe: Optional[str] = None, push: bool = True, announced: bool = False) -> bool:
    """Add an in-app notification (deduplicated by key) and optionally a web push."""
    if dedupe and db.exec(select(Notification.id).where(Notification.dedupe == dedupe)).first():
        return False
    db.add(Notification(user_id=user_id, kind=kind, text=text, link=link, dedupe=dedupe, announced=announced))
    if push and PUSH_ENABLED:
        user = db.get(User, user_id)
        if user and user.push_subscription:
            payload = json.dumps({"title": "Habitor", "body": text, "url": link})
            threading.Thread(target=_send_push, args=(user_id, user.push_subscription, payload), daemon=True).start()
    return True


def feed_event(db: Session, kind: str, icon: str, text: str, user_id: Optional[int] = None, party_id: Optional[int] = None) -> None:
    db.add(FeedEvent(kind=kind, icon=icon, text=text, user_id=user_id, party_id=party_id))


# ===========================================================================
# Points, levels, badges
# ===========================================================================


def award(db: Session, user: User, amount: int, kind: str, ref: str, day: Optional[date] = None) -> bool:
    """Apply a point change exactly once (keyed by ``ref``). XP mirrors points and never drops below 0."""
    if amount == 0 or db.exec(select(PointEvent.id).where(PointEvent.ref == ref)).first():
        return False
    db.add(PointEvent(user_id=user.id, amount=amount, kind=kind, ref=ref, local_date=day or local_today(user)))
    user.xp = max(0, user.xp + amount)
    db.add(user)
    return True


def grant_badge(db: Session, user: User, key: str, fx: Optional[Fx] = None, expires_at: Optional[datetime] = None) -> bool:
    existing = db.exec(select(UserBadge).where(UserBadge.user_id == user.id, UserBadge.key == key)).first()
    if existing:
        if expires_at:
            existing.expires_at = expires_at
            db.add(existing)
        return False
    badge = game.BADGE_MAP[key]
    db.add(UserBadge(user_id=user.id, key=key, expires_at=expires_at))
    if not badge.temporary:
        notify(db, user.id, "badge", f"🏅 Badge unlocked: {badge.icon} {badge.name}", f"/u/{user.username}", announced=fx is not None)
        if fx:
            fx.toast(f"Badge unlocked: {badge.name}", "gold")
    return True


def player_stats(db: Session, user: User) -> dict[str, int]:
    cis = db.exec(select(CheckIn).where(CheckIn.user_id == user.id, CheckIn.status != "rejected")).all()
    hours = [to_local(c.created_at, user).hour for c in cis]
    habits = db.exec(select(Habit).where(Habit.user_id == user.id)).all()
    return {
        "checkins": len(cis),
        "early_checkins": sum(1 for h in hours if 3 <= h < 6),
        "late_checkins": sum(1 for h in hours if h >= 23),
        "best_streak": max([h.best_streak for h in habits] + [0]),
        "photo_checkins": sum(1 for c in cis if "photo" in c.methods),
        "gps_checkins": sum(1 for c in cis if "gps" in c.methods),
        "qr_checkins": sum(1 for c in cis if "qr" in c.methods or "nfc" in c.methods),
        "timer_checkins": sum(1 for c in cis if "timer" in c.methods),
        "reflections": sum(1 for c in cis if c.mood is not None),
        "prove_it_checkins": sum(1 for c in cis if c.prove_it),
        "friends": len(friend_ids(db, user.id)),
        "votes_cast": len(db.exec(select(Vote.id).where(Vote.user_id == user.id)).all()),
        "taunts_sent": len(db.exec(select(Taunt.id).where(Taunt.sender_id == user.id)).all()),
        "tiles_owned": len(db.exec(select(Tile.id).where(Tile.owner_id == user.id)).all()),
        "dares_done": len(db.exec(select(DareClaim.id).where(DareClaim.user_id == user.id)).all()),
        "level": game.level_for_xp(user.xp),
    }


def check_badges(db: Session, user: User, fx: Optional[Fx] = None) -> None:
    db.flush()
    owned = set(db.exec(select(UserBadge.key).where(UserBadge.user_id == user.id)).all())
    for key in sorted(game.earned_badges(player_stats(db, user)) - owned):
        grant_badge(db, user, key, fx)


def active_badges(db: Session, user: User) -> list[UserBadge]:
    now = utcnow()
    return [b for b in db.exec(select(UserBadge).where(UserBadge.user_id == user.id)).all()
            if b.expires_at is None or b.expires_at > now]


def hud_data(db: Session, user: User) -> dict[str, Any]:
    prog = game.level_progress(user.xp)
    streak = db.exec(select(func.max(Habit.current_streak)).where(Habit.user_id == user.id, Habit.archived == False)).one()  # noqa: E712
    unread = db.exec(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.read == False)).one()  # noqa: E712
    return {"level": prog.level, "pct": prog.pct, "into": prog.into, "span": prog.span, "xp": user.xp,
            "coins": user.coins, "streak": streak or 0, "shields": user.shields, "unread": unread or 0,
            "segments": game.segments(prog.pct, 10), "chill": user.chill_on}


# ===========================================================================
# Habits and streaks
# ===========================================================================


def active_habits(db: Session, user: User) -> list[Habit]:
    return list(db.exec(select(Habit).where(Habit.user_id == user.id, Habit.archived == False)  # noqa: E712
                        .order_by(Habit.id)).all())


def owned_habit(db: Session, user: User, habit_id: int) -> Habit:
    habit = db.get(Habit, habit_id)
    if not habit or habit.user_id != user.id:
        raise StarletteHTTPException(404, "Quest not found")
    return habit


def habit_start_day(habit: Habit, user: User) -> date:
    return local_date_of(habit.created_at, user)


def habit_done_days(db: Session, habit: Habit) -> set[date]:
    return set(db.exec(select(CheckIn.local_date).where(CheckIn.habit_id == habit.id, CheckIn.status != "rejected")).all())


def habit_protected_days(db: Session, habit: Habit) -> set[date]:
    return set(db.exec(select(DayMark.day).where(DayMark.habit_id == habit.id, col(DayMark.kind).in_(("shield", "chill")))).all())


def refresh_streak(db: Session, habit: Habit, user: User, today: Optional[date] = None) -> int:
    db.flush()
    today = today or local_today(user)
    streak = game.compute_streak(habit.schedule, habit_done_days(db, habit), habit_protected_days(db, habit),
                                 today, habit_start_day(habit, user))
    habit.current_streak = streak
    habit.best_streak = max(habit.best_streak, streak)
    db.add(habit)
    return streak


def today_checkin(db: Session, habit: Habit, today: date) -> Optional[CheckIn]:
    return db.exec(select(CheckIn).where(CheckIn.habit_id == habit.id, CheckIn.slot == today.isoformat())).first()


def week_count(db: Session, habit: Habit, today: date) -> int:
    ws = game.week_start(today)
    return len(db.exec(select(CheckIn.id).where(CheckIn.habit_id == habit.id, CheckIn.local_date >= ws,
                                                CheckIn.local_date <= today, CheckIn.status != "rejected")).all())


def is_due_today(db: Session, habit: Habit, today: date) -> bool:
    if habit.frequency == "weekly":
        return week_count(db, habit, today) < habit.times_per_week or today_checkin(db, habit, today) is not None
    return game.is_scheduled_day(habit.schedule, today)


# ===========================================================================
# Core game actions
# ===========================================================================


def perform_checkin(db: Session, user: User, habit: Habit, *, methods: list[str], status: str, verified_by: str,
                    photo_path: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None,
                    distance: Optional[float] = None, timer_minutes: Optional[int] = None,
                    prove_it: bool = False) -> tuple[CheckIn, Fx]:
    """Create a check-in and apply every consequence (points, boss, pet, tiles, taunts, badges) in one transaction."""
    fx = Fx()
    now_local = local_now(user)
    today = now_local.date()
    if today_checkin(db, habit, today):
        raise GameError("Quest already complete today.", 409)
    if habit.frequency == "weekly" and week_count(db, habit, today) >= habit.times_per_week:
        raise GameError("You've already hit this week's target.", 409)
    if user.tz_changed_at and utcnow() - user.tz_changed_at < TZ_CHANGE_GUARD:
        # Switching timezone shifts the local date; don't let that turn into two check-ins in one real day.
        last = db.exec(select(CheckIn.created_at).where(CheckIn.habit_id == habit.id, CheckIn.status != "rejected")
                       .order_by(col(CheckIn.created_at).desc())).first()
        if last and utcnow() - last < MIN_GAP_AFTER_TZ_CHANGE:
            wait = MIN_GAP_AFTER_TZ_CHANGE - (utcnow() - last)
            raise GameError(f"You changed timezone recently. This quest can be checked in again in "
                            f"{int(wait.total_seconds() // 3600) + 1} h.", 429)
    level_before = game.level_for_xp(user.xp)
    had_shield_yesterday = db.exec(select(DayMark.id).where(DayMark.habit_id == habit.id, DayMark.kind == "shield",
                                                            DayMark.day == today - timedelta(days=1))).first()
    ci = CheckIn(habit_id=habit.id, user_id=user.id, slot=today.isoformat(), local_date=today, status=status,
                 methods=",".join(methods), verified_by=verified_by, photo_path=photo_path, lat=lat, lon=lon,
                 distance_m=distance, timer_minutes=timer_minutes, prove_it=prove_it)
    db.add(ci)
    db.flush()  # the unique (habit_id, slot) constraint fires here on a double submit
    streak = refresh_streak(db, habit, user, today)
    bonus_eligible = verified_by in game.INSTANT_VERIFY_METHODS
    points = game.checkin_points(streak, bonus_eligible, habit.difficulty, prove_it)
    ci.streak, ci.points = streak, points
    award(db, user, points, "checkin", f"checkin:{ci.id}", today)
    user.coins += game.coins_for_points(points)
    fx.toast(f"Check-in complete · +{points} XP", "good").sfx("checkin")
    if prove_it:
        fx.toast("⚡ Prove It Now — double points!", "gold")
    if game.shield_earned(streak):
        if user.shields < game.MAX_SHIELDS and notify(db, user.id, "shield", f"🛡️ STREAK SHIELD EARNED ({habit.name}, {streak} days)",
                                                      "/", dedupe=f"shield:{habit.id}:{today}", announced=True):
            user.shields += 1
            fx.toast("🛡️ Streak Shield earned!", "gold")
    if had_shield_yesterday and streak > 1:
        fx.set("stamp", "Streak saved")
    elif streak >= 2 and now_local.hour >= 20:
        fx.set("stamp", "Streak saved")
    elif streak >= 3:
        fx.set("stamp", f"Streak x{streak}")
    _resolve_taunts(db, user, ci, fx)
    for pid in user_party_ids(db, user.id):
        party = db.get(Party, pid)
        if not party:
            continue
        party.pet_points += 1
        db.add(party)
        boss = ensure_boss(db, party)
        if boss.status == "active":
            dmg = game.boss_damage(points)
            boss.hp = max(0, boss.hp - dmg)
            db.add(BossHit(boss_id=boss.id, user_id=user.id, checkin_id=ci.id, damage=dmg))
            db.add(boss)
            fx.set("bossHit", dmg)
            if boss.hp == 0:
                defeat_boss(db, boss, party, fx)
    if "gps" in methods and verified_by == "gps" and game.valid_coords(lat, lon):
        capture_tile(db, user, lat, lon, fx)  # type: ignore[arg-type]
    user.prove_it_until = None if prove_it else user.prove_it_until
    db.add(user)
    check_badges(db, user, fx)
    level_after = game.level_for_xp(user.xp)
    if level_after > level_before:
        fx.set("levelUp", level_after).sfx("levelup")
        notify(db, user.id, "level", f"⭐ LEVEL UP! You reached level {level_after}", "/settings", announced=True)
    log_event("checkin", user=user.id, habit=habit.id, points=points, status=status, methods=ci.methods)
    return ci, fx


def _resolve_taunts(db: Session, user: User, ci: CheckIn, fx: Fx) -> None:
    """Checking in clears taunt stickers; within 24h it's a CLAP BACK."""
    now = utcnow()
    for t in db.exec(select(Taunt).where(Taunt.target_id == user.id, Taunt.resolved == False)).all():  # noqa: E712
        t.resolved = True
        hours = (now - t.created_at).total_seconds() / 3600
        if not t.clapped and game.is_clapback(hours):
            t.clapped = True
            ci.clapback = True
            award(db, user, game.CLAPBACK_BONUS, "clapback", f"clapback:{t.id}")
            sender = db.get(User, t.sender_id)
            if sender:
                award(db, sender, -game.TAUNTER_PENALTY, "clapped", f"clapped:{t.id}")
                notify(db, sender.id, "comeback", f"🔁 COMEBACK! {user.username} clapped back at your taunt. -{game.TAUNTER_PENALTY} pts",
                       f"/u/{user.username}")
            fx.toast(f"🔁 Comeback! Clap back +{game.CLAPBACK_BONUS}", "gold")
            grant_badge(db, user, "comeback_kid", fx)
        db.add(t)


def verify_checkin(db: Session, ci: CheckIn, source: str) -> None:
    """Promote a pending check-in. Votes and AI earn the verified bonus; the 12h timeout doesn't."""
    if ci.status in ("verified", "rejected"):
        return
    owner = db.get(User, ci.user_id)
    habit = db.get(Habit, ci.habit_id)
    ci.status = "verified"
    ci.verified_by = source
    ci.reprove_deadline = None
    if source in ("votes", "ai") and owner and habit:
        bonus = game.late_verified_bonus(ci.streak, habit.difficulty, ci.prove_it)
        if award(db, owner, bonus, "verified", f"verify:{ci.id}", ci.local_date):
            ci.points += bonus
    db.add(ci)
    if owner and habit:
        label = {"votes": "approved by your friends", "ai": "AI verified", "timeout": "no objections within 12 hours",
                 "reprove": "re-verification accepted"}.get(source, source.upper())
        notify(db, owner.id, "verified", f"✅ Proof verified — {habit.icon} {habit.name}: {label}", "/feed")


def reject_checkin(db: Session, ci: CheckIn) -> None:
    """Re-prove deadline missed: revoke the check-in, -15, lose the streak day, Caught Cappin' for 7 days."""
    if ci.status == "rejected":
        return
    owner = db.get(User, ci.user_id)
    habit = db.get(Habit, ci.habit_id)
    ci.status = "rejected"
    db.add(ci)
    if not owner or not habit:
        return
    award(db, owner, -ci.points, "revoked", f"revoke:{ci.id}", ci.local_date)
    if not owner.chill_on:
        award(db, owner, -game.REJECT_PENALTY, "rejected", f"reject:{ci.id}", ci.local_date)
    for hit in db.exec(select(BossHit).where(BossHit.checkin_id == ci.id)).all():
        boss = db.get(BossBattle, hit.boss_id)
        if boss and boss.status == "active":
            boss.hp = min(boss.hp_max, boss.hp + hit.damage)
            db.add(boss)
        db.delete(hit)
    refresh_streak(db, habit, owner)
    grant_badge(db, owner, "caught_cappin", expires_at=utcnow() + timedelta(days=game.CAUGHT_CAPPIN_DAYS))
    notify(db, owner.id, "rejected", f"💀 GAME OVER… proof rejected for {habit.name}. -{game.REJECT_PENALTY} pts & Caught Cappin' 🧢", "/feed")
    feed_event(db, "shame", "🧢", f"{owner.username} got Caught Cappin' on {habit.name}!", user_id=owner.id)


def cast_vote(db: Session, voter: User, ci: CheckIn, value: str) -> Fx:
    fx = Fx()
    if value not in ("legit", "sus"):
        raise GameError("Invalid vote.")
    if ci.user_id == voter.id:
        raise GameError("You can't vote on your own proof.", 403)
    if not are_friends(db, voter.id, ci.user_id):
        raise GameError("Only friends can vote.", 403)
    if ci.status != "pending":
        raise GameError("Voting is closed for this proof.", 409)
    if db.exec(select(Vote.id).where(Vote.checkin_id == ci.id, Vote.user_id == voter.id)).first():
        raise GameError("One vote per proof.", 409)
    db.add(Vote(checkin_id=ci.id, user_id=voter.id, value=value))
    db.flush()
    votes = db.exec(select(Vote.value).where(Vote.checkin_id == ci.id)).all()
    outcome = game.vote_outcome(votes.count("legit"), votes.count("sus"))
    owner = db.get(User, ci.user_id)
    if value == "sus":
        notify(db, ci.user_id, "vote", f"🤨 {voter.username} voted SUS on your proof", "/feed")
    else:
        notify(db, ci.user_id, "vote", f"👍 {voter.username} says your proof is LEGIT", "/feed", push=False)
    if outcome == "flagged":
        ci.status = "flagged"
        ci.reprove_deadline = utcnow() + timedelta(hours=game.REPROVE_WINDOW_HOURS)
        db.add(ci)
        if owner:
            notify(db, owner.id, "flagged", "🚨 YOUR PROOF LOOKS SUS! RE-PROVE within 12h with live photo + GPS or QR", "/")
    elif outcome == "verified":
        verify_checkin(db, ci, "votes")
    check_badges(db, voter)
    fx.sfx("vote").toast("Vote recorded: " + ("legit" if value == "legit" else "sus"), "good" if value == "legit" else "pink")
    return fx


# --- Taunts ----------------------------------------------------------------


def taunt_eligible(db: Session, target: User) -> bool:
    """True when the target recently missed a scheduled habit/broke a streak, or is late on one tonight."""
    if target.chill_on:
        return False
    today = local_today(target)
    marks = db.exec(select(DayMark).where(DayMark.user_id == target.id, col(DayMark.kind).in_(("miss", "shield")),
                                          DayMark.day >= today - timedelta(days=2))).all()
    for m in marks:
        recovered = db.exec(select(CheckIn.id).where(CheckIn.user_id == target.id, CheckIn.local_date > m.day,
                                                     CheckIn.status != "rejected")).first()
        if not recovered:
            return True
    if local_now(target).hour >= 20:
        for h in active_habits(db, target):
            if is_due_today(db, h, today) and not today_checkin(db, h, today):
                return True
    return False


def taunt_state(db: Session, sender: User, target: User) -> tuple[bool, str]:
    day = local_today(sender)
    sent_today = db.exec(select(func.count(Taunt.id)).where(Taunt.sender_id == sender.id, Taunt.sender_day == day)).one()
    to_target = db.exec(select(func.count(Taunt.id)).where(Taunt.sender_id == sender.id, Taunt.target_id == target.id,
                                                           Taunt.sender_day == day)).one()
    return game.can_taunt(is_self=sender.id == target.id, is_friend=are_friends(db, sender.id, target.id),
                          target_chill=target.chill_on, target_eligible=taunt_eligible(db, target),
                          sent_today=sent_today, sent_to_target_today=to_target)


def send_taunt(db: Session, sender: User, target: User, key: str) -> Fx:
    taunt = game.TAUNTS.get(key)
    if not taunt:
        raise GameError("Preset taunts only.")
    ok, reason = taunt_state(db, sender, target)
    if not ok:
        raise GameError(reason, 403 if "friend" in reason.lower() or "chill" in reason.lower() else 429)
    db.add(Taunt(sender_id=sender.id, target_id=target.id, key=key, sender_day=local_today(sender)))
    notify(db, target.id, "taunt", f"😈 Player {sender.username} has taunted you: {taunt.sticker} {taunt.text}",
           f"/u/{target.username}", announced=True)
    check_badges(db, sender)
    return Fx().sfx("taunt").toast(f"Taunt sent {taunt.sticker} → {target.username}", "pink")


# --- Boss battles, pets, tiles --------------------------------------------


def party_today(db: Session, party: Party) -> date:
    owner = db.get(User, party.owner_id)
    return local_today(owner) if owner else utcnow().date()


def ensure_boss(db: Session, party: Party) -> BossBattle:
    ws = game.week_start(party_today(db, party))
    boss = db.exec(select(BossBattle).where(BossBattle.party_id == party.id, BossBattle.week_start == ws)).first()
    if boss:
        return boss
    b = game.boss_for_week(party.id, ws)
    hp = game.boss_hp(len(party_member_ids(db, party.id)))
    boss = BossBattle(party_id=party.id, week_start=ws, boss_key=b.key, hp_max=hp, hp=hp)
    db.add(boss)
    db.flush()
    feed_event(db, "boss", b.sprite, f"A wild {b.name.upper()} appears before {party.name}! HP {hp}", party_id=party.id)
    for uid in party_member_ids(db, party.id):
        notify(db, uid, "boss", f"{b.sprite} BOSS BATTLE! {b.name} ({hp} HP) attacks {party.name}", f"/boss?party={party.id}",
               dedupe=f"bossspawn:{boss.id}:{uid}")
    return boss


def defeat_boss(db: Session, boss: BossBattle, party: Party, fx: Optional[Fx] = None) -> None:
    b = game.BOSS_MAP[boss.boss_key]
    boss.status = "defeated"
    boss.defeated_at = utcnow()
    db.add(boss)
    feed_event(db, "boss", "🗡️", f"{party.name} DEFEATED {b.name.upper()}! +{game.BOSS_LOOT_COINS} 🪙 loot each", party_id=party.id)
    for member in users_by_ids(db, party_member_ids(db, party.id)).values():
        member.coins += game.BOSS_LOOT_COINS
        db.add(member)
        grant_badge(db, member, "boss_slayer")
        notify(db, member.id, "boss", f"🗡️ VICTORY! {b.name} defeated. +{game.BOSS_LOOT_COINS} 🪙", f"/boss?party={party.id}",
               dedupe=f"bosswin:{boss.id}:{member.id}")
    if fx:
        fx.toast(f"🗡️ Boss defeated! +{game.BOSS_LOOT_COINS} 🪙", "gold").set("confetti")


def capture_tile(db: Session, user: User, lat: float, lon: float, fx: Optional[Fx] = None) -> None:
    tx, ty = game.tile_for(lat, lon)
    tc = db.exec(select(TileCount).where(TileCount.tx == tx, TileCount.ty == ty, TileCount.user_id == user.id)).first()
    if not tc:
        tc = TileCount(tx=tx, ty=ty, user_id=user.id, count=0)
    tc.count += 1
    db.add(tc)
    tile = db.exec(select(Tile).where(Tile.tx == tx, Tile.ty == ty)).first()
    if not tile:
        tile = Tile(tx=tx, ty=ty, owner_id=None)
    db.flush()
    counts = {c.user_id: c.count for c in db.exec(select(TileCount).where(TileCount.tx == tx, TileCount.ty == ty)).all()}
    new_owner = game.tile_new_owner(tile.owner_id, counts)
    if new_owner != tile.owner_id:
        previous = tile.owner_id
        tile.owner_id = new_owner
        tile.captured_at = utcnow()
        if new_owner == user.id and fx:
            fx.toast("🚩 Tile captured!" if previous is None else "🚩 Tile stolen!", "gold")
        if previous:
            notify(db, previous, "tile", f"🚩 {user.username} stole one of your map tiles!", "/map")
    db.add(tile)


# --- Duels and stake pots ---------------------------------------------------


def active_days(db: Session, uid: int, start: date, end: date) -> set[date]:
    return set(db.exec(select(CheckIn.local_date).where(CheckIn.user_id == uid, CheckIn.status != "rejected",
                                                        CheckIn.local_date >= start, CheckIn.local_date <= end)).all())


def duel_runs(db: Session, duel: Duel, today: date) -> tuple[int, int]:
    if not duel.start_date or not duel.end_date:
        return 0, 0
    end = min(duel.end_date, today)
    return (game.longest_daily_run(active_days(db, duel.challenger_id, duel.start_date, end), duel.start_date, end),
            game.longest_daily_run(active_days(db, duel.opponent_id, duel.start_date, end), duel.start_date, end))


def finish_duel(db: Session, duel: Duel) -> None:
    a, b = duel_runs(db, duel, duel.end_date or utcnow().date())
    duel.status = "finished"
    duel.winner_id = duel.challenger_id if a > b else duel.opponent_id if b > a else None
    db.add(duel)
    users = users_by_ids(db, [duel.challenger_id, duel.opponent_id])
    for uid in (duel.challenger_id, duel.opponent_id):
        if duel.winner_id is None:
            text = f"⚔️ DUEL DRAW! {a} vs {b} days. Nobody does a dare."
        elif uid == duel.winner_id:
            text = "⚔️ YOU WON THE STREAK DUEL! Pick the loser's dare."
        else:
            text = f"⚔️ DUEL LOST ({a} vs {b}). Await your dare…"
        notify(db, uid, "duel", text, "/challenges", dedupe=f"duelend:{duel.id}:{uid}")
    if duel.winner_id and duel.winner_id in users:
        grant_badge(db, users[duel.winner_id], "duelist")


def challenge_progress(db: Session, ch: Challenge, uid: int) -> int:
    return len(db.exec(select(CheckIn.id).where(CheckIn.user_id == uid, CheckIn.status != "rejected",
                                                CheckIn.local_date >= ch.start_date, CheckIn.local_date <= ch.end_date)).all())


def settle_challenge(db: Session, ch: Challenge) -> None:
    members = db.exec(select(ChallengeMember).where(ChallengeMember.challenge_id == ch.id).order_by(ChallengeMember.id)).all()
    finishers = [m for m in members if challenge_progress(db, ch, m.user_id) >= ch.required]
    users = users_by_ids(db, [m.user_id for m in members])
    if finishers:
        for m, share in zip(finishers, game.split_pot(ch.pot, len(finishers))):
            m.finished, m.payout = True, share
    else:  # nobody finished: refund the stakes
        for m in members:
            m.payout = ch.stake
    for m in members:
        u = users.get(m.user_id)
        if not u:
            continue
        u.coins += m.payout
        db.add(u)
        db.add(m)
        if m.finished and m.payout > ch.stake:
            grant_badge(db, u, "high_roller")
        msg = (f"🎰 {ch.name}: you finished! +{m.payout} 🪙 from the pot" if m.finished
               else f"🎰 {ch.name}: nobody finished — stake refunded" if not finishers
               else f"🎰 {ch.name}: you didn't finish. Stake lost.")
        notify(db, u.id, "challenge", msg, "/challenges", dedupe=f"settle:{ch.id}:{u.id}")
    ch.status = "settled"
    db.add(ch)


# ===========================================================================
# Leaderboards
# ===========================================================================

SCOPES = {"friends": "FRIENDS", "party": "PARTY", "challenge": "CHALLENGE", "global": "GLOBAL"}
PERIODS = {"today": "TODAY", "week": "This week", "all": "ALL-TIME"}
MODES = {"points": "POINTS", "consistency": "Consistency %"}
EPOCH = date(2000, 1, 1)


def point_totals(db: Session, ids: list[int], start: date, end: date) -> dict[int, int]:
    totals = {uid: 0 for uid in ids}
    if not ids or end < start:
        return totals
    rows = db.exec(select(PointEvent.user_id, func.sum(PointEvent.amount)).where(
        col(PointEvent.user_id).in_(ids), PointEvent.local_date >= start, PointEvent.local_date <= end
    ).group_by(PointEvent.user_id)).all()
    totals.update({uid: int(total or 0) for uid, total in rows})
    return totals


def consistency_scores(db: Session, users: dict[int, User], start: date, end: date, today: date) -> dict[int, int]:
    """completed / scheduled. Today only counts as scheduled once it's done (the day is still open)."""
    out: dict[int, int] = {}
    for uid, user in users.items():
        if end < start:
            out[uid] = 0
            continue
        completed = scheduled = 0
        for h in active_habits(db, user):
            first = max(start, habit_start_day(h, user))
            done = active_days_for_habit(db, h, first, end)
            completed += len(done)
            closed_end = min(end, today - timedelta(days=1))
            scheduled += game.scheduled_count(h.schedule, first, closed_end)
            if end >= today and today in done:
                scheduled += 1
        out[uid] = game.consistency_pct(completed, scheduled)
    return out


def active_days_for_habit(db: Session, habit: Habit, start: date, end: date) -> set[date]:
    return set(db.exec(select(CheckIn.local_date).where(CheckIn.habit_id == habit.id, CheckIn.status != "rejected",
                                                        CheckIn.local_date >= start, CheckIn.local_date <= end)).all())


def leaderboard(db: Session, viewer: User, scope: str, period: str, mode: str,
                party_id: Optional[int] = None, challenge_id: Optional[int] = None) -> dict[str, Any]:
    today = local_today(viewer)
    title = SCOPES.get(scope, "FRIENDS")
    if scope == "party":
        my_parties = user_party_ids(db, viewer.id)
        pid = party_id if party_id in my_parties else (my_parties[0] if my_parties else None)
        ids = party_member_ids(db, pid) if pid else [viewer.id]
        party_id = pid
    elif scope == "challenge":
        ch = db.get(Challenge, challenge_id) if challenge_id else None
        members = db.exec(select(ChallengeMember.user_id).where(ChallengeMember.challenge_id == ch.id)).all() if ch else []
        if ch and viewer.id not in members:
            ch, members = None, []
        ids = list(members) or [viewer.id]
    elif scope == "global":
        ids = list(db.exec(select(User.id).where(User.global_opt_in == True)).all())  # noqa: E712
        if viewer.id not in ids:
            ids.append(viewer.id)
    else:
        scope = "friends"
        ids = sorted(friend_ids(db, viewer.id) | {viewer.id})

    if period == "today":
        start, end, pstart, pend = today, today, today - timedelta(days=1), today - timedelta(days=1)
    elif period == "week":
        ws = game.week_start(today)
        start, end, pstart, pend = ws, today, ws, today - timedelta(days=1)
    else:
        period = "all"
        start, end, pstart, pend = EPOCH, today, EPOCH, today - timedelta(days=1)
    if scope == "challenge" and challenge_id:
        ch = db.get(Challenge, challenge_id)
        if ch:
            start, end = ch.start_date, min(ch.end_date, today)
            pstart, pend = ch.start_date, min(ch.end_date, today - timedelta(days=1))

    users = users_by_ids(db, ids)
    if mode == "consistency":
        scores = consistency_scores(db, users, start, end, today)
        prev = consistency_scores(db, users, pstart, pend, today) if pend >= pstart else {}
    else:
        mode = "points"
        scores = point_totals(db, ids, start, end)
        prev = point_totals(db, ids, pstart, pend) if pend >= pstart else {}
    xp = {uid: u.xp for uid, u in users.items()}
    order = game.rank_order(scores, xp)
    changes = game.rank_changes(order, game.rank_order(prev, xp)) if prev else {}
    rows = []
    for i, uid in enumerate(order, start=1):
        u = users.get(uid)
        if u:
            rows.append({"rank": i, "user": u, "score": scores[uid], "change": changes.get(uid, 0),
                         "trophy": game.TROPHIES.get(i, ""), "me": uid == viewer.id,
                         "level": game.level_for_xp(u.xp)})
    return {"rows": rows, "scope": scope, "period": period, "mode": mode, "title": title,
            "party_id": party_id, "challenge_id": challenge_id, "unit": "%" if mode == "consistency" else "PTS"}


def walls(db: Session, viewer: User) -> dict[str, list[dict[str, str]]]:
    """Wall of Fame / Wall of Shame for the viewer's friends this week (they reset every Monday)."""
    today = local_today(viewer)
    ws = game.week_start(today)
    ids = sorted(friend_ids(db, viewer.id) | {viewer.id})
    users = users_by_ids(db, ids)
    totals = point_totals(db, ids, ws, today)
    fame: list[dict[str, str]] = []
    for uid in game.rank_order(totals)[:3]:
        if totals[uid] > 0:
            fame.append({"icon": "🏆", "user": users[uid].username, "text": f"{totals[uid]} pts this week"})
    streaks = db.exec(select(Habit).where(col(Habit.user_id).in_(ids), Habit.archived == False)  # noqa: E712
                      .order_by(col(Habit.current_streak).desc()).limit(1)).first()
    if streaks and streaks.current_streak > 0:
        fame.append({"icon": "🔥", "user": users[streaks.user_id].username,
                     "text": f"{streaks.current_streak}-day {streaks.name} streak"})
    shame: list[dict[str, str]] = []
    for m in db.exec(select(DayMark).where(col(DayMark.user_id).in_(ids), DayMark.kind == "miss", DayMark.day >= ws,
                                           DayMark.streak_lost >= 3).order_by(col(DayMark.streak_lost).desc()).limit(5)).all():
        h = db.get(Habit, m.habit_id)
        shame.append({"icon": "💔", "user": users[m.user_id].username,
                      "text": f"dropped a {m.streak_lost}-day {h.name if h else ''} streak"})
    now = utcnow()
    for b in db.exec(select(UserBadge).where(col(UserBadge.user_id).in_(ids), UserBadge.key == "caught_cappin")).all():
        if b.expires_at and b.expires_at > now:
            shame.append({"icon": "🧢", "user": users[b.user_id].username, "text": "got Caught Cappin'"})
    for pid in user_party_ids(db, viewer.id):
        for boss in db.exec(select(BossBattle).where(BossBattle.party_id == pid, BossBattle.status == "failed",
                                                     BossBattle.week_start >= ws - timedelta(days=7))).all():
            party = db.get(Party, pid)
            b = game.BOSS_MAP[boss.boss_key]
            shame.append({"icon": b.sprite, "user": party.name if party else "?", "text": f"got laughed at by {b.name}"})
    return {"fame": fame, "shame": shame}


def league_table(db: Session, party: Party, today: date) -> list[dict[str, Any]]:
    """Parties in the same tier ranked by average points per member this season (month)."""
    start = today.replace(day=1)
    rows = []
    for p in db.exec(select(Party).where(Party.tier == party.tier)).all():
        members = party_member_ids(db, p.id)
        if not members:
            continue
        total = sum(point_totals(db, members, start, today).values())
        rows.append({"party": p, "score": round(total / len(members), 1), "members": len(members)})
    rows.sort(key=lambda r: (-r["score"], r["party"].id))
    promote, relegate = game.season_moves([r["party"].id for r in rows])
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r["zone"] = "up" if r["party"].id in promote and party.tier != "Diamond" else (
            "down" if r["party"].id in relegate and party.tier != "Bronze" else "")
    return rows


# ===========================================================================
# Stats, coach, recap
# ===========================================================================


def heatmap(db: Session, user: User, weeks: int) -> list[list[dict[str, Any]]]:
    today = local_today(user)
    start = game.week_start(today) - timedelta(weeks=weeks - 1)
    counts: dict[date, int] = defaultdict(int)
    for d in db.exec(select(CheckIn.local_date).where(CheckIn.user_id == user.id, CheckIn.status != "rejected",
                                                      CheckIn.local_date >= start)).all():
        counts[d] += 1
    cols = []
    for w in range(weeks):
        col_ = []
        for i in range(7):
            d = start + timedelta(days=w * 7 + i)
            n = counts.get(d, 0)
            col_.append({"date": d.isoformat(), "count": n, "level": min(4, n), "future": d > today})
        cols.append(col_)
    return cols


def habit_rates(db: Session, user: User, days: int = 30) -> list[dict[str, Any]]:
    today = local_today(user)
    out = []
    for h in active_habits(db, user):
        start = max(today - timedelta(days=days - 1), habit_start_day(h, user))
        done = active_days_for_habit(db, h, start, today)
        scheduled = game.scheduled_count(h.schedule, start, today - timedelta(days=1)) + (1 if today in done else 0)
        pct = game.consistency_pct(len(done), scheduled)
        out.append({"habit": h, "pct": pct, "done": len(done), "scheduled": scheduled, "segments": game.segments(pct, 10)})
    return out


def hour_histogram(db: Session, user: User) -> list[int]:
    hist = [0] * 24
    for dt in db.exec(select(CheckIn.created_at).where(CheckIn.user_id == user.id, CheckIn.status != "rejected")).all():
        hist[to_local(dt, user).hour] += 1
    return hist


def mood_trend(db: Session, user: User, n: int = 14) -> list[dict[str, Any]]:
    rows = db.exec(select(CheckIn).where(CheckIn.user_id == user.id, col(CheckIn.mood).is_not(None))
                   .order_by(col(CheckIn.created_at).desc()).limit(n)).all()
    return [{"date": c.local_date, "mood": c.mood, "emoji": game.MOODS[(c.mood or 3) - 1]} for c in reversed(rows)]


def coach_stats(db: Session, user: User) -> game.CoachStats:
    today = local_today(user)
    s = game.CoachStats()
    s.total_checkins = len(db.exec(select(CheckIn.id).where(CheckIn.user_id == user.id, CheckIn.status != "rejected")).all())
    for d in db.exec(select(DayMark.day).where(DayMark.user_id == user.id, col(DayMark.kind).in_(("miss", "shield")),
                                               DayMark.day >= today - timedelta(weeks=8))).all():
        s.misses_by_weekday[d.weekday()] += 1
    s.checkins_by_hour = hour_histogram(db, user)
    s.habit_rates = [(r["habit"].name, r["pct"], bool(r["habit"].tiny_version)) for r in habit_rates(db, user)]
    s.best_streak_now = max([h.current_streak for h in active_habits(db, user)] + [0])
    moods = [m["mood"] for m in mood_trend(db, user)]
    s.avg_mood = sum(moods) / len(moods) if moods else None
    return s


def get_coach_tip(db: Session, user: User, bg: Optional[BackgroundTasks] = None) -> CoachTip:
    ws = game.week_start(local_today(user))
    tip = db.exec(select(CoachTip).where(CoachTip.user_id == user.id, CoachTip.week_start == ws)).first()
    if tip:
        return tip
    tip = CoachTip(user_id=user.id, week_start=ws, text=game.coach_tip(coach_stats(db, user)))
    db.add(tip)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.exec(select(CoachTip).where(CoachTip.user_id == user.id, CoachTip.week_start == ws)).one()
    if bg and ANTHROPIC_API_KEY:
        bg.add_task(ai_coach_tip, user.id, ws)
    return tip


def recap_data(db: Session, user: User, ws: date) -> dict[str, Any]:
    we = ws + timedelta(days=6)
    cis = db.exec(select(CheckIn).where(CheckIn.user_id == user.id, CheckIn.status != "rejected",
                                        CheckIn.local_date >= ws, CheckIn.local_date <= we)).all()
    habits = {h.id: h for h in db.exec(select(Habit).where(Habit.user_id == user.id)).all()}
    by_habit: dict[int, int] = defaultdict(int)
    for c in cis:
        by_habit[c.habit_id] += 1
    top = max(by_habit.items(), key=lambda kv: kv[1]) if by_habit else None
    best_photo = None
    best_reacts = -1
    for c in cis:
        if c.photo_path:
            n = len(db.exec(select(Reaction.id).where(Reaction.checkin_id == c.id)).all())
            if n > best_reacts:
                best_photo, best_reacts = c, n
    ids = sorted(friend_ids(db, user.id) | {user.id})
    this_week = game.rank_order(point_totals(db, ids, ws, we))
    last_week = game.rank_order(point_totals(db, ids, ws - timedelta(days=7), ws - timedelta(days=1)))
    rank_now = this_week.index(user.id) + 1
    rank_before = last_week.index(user.id) + 1
    taunts_sent = len(db.exec(select(Taunt.id).where(Taunt.sender_id == user.id, Taunt.sender_day >= ws, Taunt.sender_day <= we)).all())
    start_dt = datetime.combine(ws, time.min) - timedelta(days=1)
    taunts_received = len(db.exec(select(Taunt.id).where(Taunt.target_id == user.id, Taunt.created_at >= start_dt,
                                                         Taunt.created_at <= start_dt + timedelta(days=9))).all())
    xp = sum(point_totals(db, [user.id], ws, we).values())
    return {
        "week_start": ws, "week_end": we, "checkins": len(cis), "xp": xp,
        "top_habit": habits[top[0]] if top else None, "top_count": top[1] if top else 0,
        "best_streak": max([c.streak for c in cis] + [0]),
        "best_photo": best_photo, "best_photo_reacts": max(best_reacts, 0),
        "rank_now": rank_now, "rank_before": rank_before, "rank_delta": rank_before - rank_now, "players": len(ids),
        "taunts_sent": taunts_sent, "taunts_received": taunts_received,
        "level": game.level_for_xp(user.xp),
    }


# ===========================================================================
# Photos, Claude
# ===========================================================================


def save_photo(upload: UploadFile) -> str:
    """Validate, auto-orient, resize to 1280px, strip EXIF and store as WebP. Returns the relative path."""
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise GameError("Photo too big — max 5 MB.", 413)
    if not data:
        raise GameError("Snap a live photo first.")
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise GameError("That file isn't a photo.", 415)
    if fmt not in {"JPEG", "PNG", "WEBP", "MPO", "GIF"}:
        raise GameError("Unsupported image type.", 415)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_PHOTO_EDGE, MAX_PHOTO_EDGE))
    clean = Image.new("RGB", img.size)
    clean.paste(img)  # copy pixels only: no EXIF/GPS metadata survives
    rel = f"{utcnow():%Y/%m}/{uuid.uuid4().hex}.webp"
    dest = UPLOAD_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    clean.save(dest, "WEBP", quality=82, method=4)
    return rel


def delete_photo(rel: Optional[str]) -> None:
    if rel:
        try:
            (UPLOAD_DIR / rel).unlink(missing_ok=True)
        except OSError:
            pass


_claude_client: Any = None
_claude_lock = threading.Lock()
# Models that accept output_config.effort, and the ones that take server-side refusal fallbacks.
_EFFORT_PREFIXES = ("claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
                    "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5")
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def claude_client() -> Any:
    global _claude_client
    if not ANTHROPIC_API_KEY:
        return None
    with _claude_lock:
        if _claude_client is None:
            try:
                import anthropic
            except ImportError:
                log_event("anthropic_sdk_missing", logging.WARNING)
                return None
            _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45.0, max_retries=2)
    return _claude_client


def claude_text(system: str, content: list[dict[str, Any]], max_tokens: int = 4000,
                schema: Optional[dict[str, Any]] = None) -> Optional[str]:
    """One Claude call; returns the text (JSON when ``schema`` is given) or None on any failure/refusal."""
    client = claude_client()
    if client is None:
        return None
    import anthropic

    output_config: dict[str, Any] = {}
    if CLAUDE_MODEL.startswith(_EFFORT_PREFIXES):
        output_config["effort"] = "low"  # short classification-style answers
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs: dict[str, Any] = {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system,
                              "messages": [{"role": "user", "content": content}]}
    if output_config:
        kwargs["output_config"] = output_config
    try:
        if CLAUDE_MODEL in _FALLBACK_MODELS:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.RateLimitError:
        log_event("claude_rate_limited", logging.WARNING)
        return None
    except anthropic.APIStatusError as exc:
        log_event("claude_api_error", logging.WARNING, status=exc.status_code)
        return None
    except anthropic.APIConnectionError:
        log_event("claude_connection_error", logging.WARNING)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or None


_AI_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": "boolean"}, "confidence": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["match", "confidence", "reason"],
    "additionalProperties": False,
}


def ai_verify_checkin(checkin_id: int) -> None:
    """Background task: ask Claude whether the photo plausibly shows the habit."""
    with Session(engine) as db:
        ci = db.get(CheckIn, checkin_id)
        if not ci or not ci.photo_path or ci.status != "pending":
            return
        habit = db.get(Habit, ci.habit_id)
        path = UPLOAD_DIR / ci.photo_path
        if not habit or not path.exists():
            return
        image_b64 = base64.standard_b64encode(path.read_bytes()).decode()
        habit_desc = f"{habit.name[:40]} (category: {habit.category})"
        text = claude_text(
            system="You verify habit-tracker proof photos. Be fair: accept plausible evidence, reject unrelated images.",
            content=[
                {"type": "image", "source": {"type": "base64", "media_type": "image/webp", "data": image_b64}},
                {"type": "text", "text": f'Does this image plausibly show "{habit_desc}"? Answer JSON {{match, confidence, reason}} '
                                         "with confidence between 0 and 1 and a reason under 20 words."},
            ],
            max_tokens=2000, schema=_AI_VERDICT_SCHEMA)
        if not text:
            return
        try:
            verdict = json.loads(text)
            match, confidence = bool(verdict["match"]), float(verdict["confidence"])
        except (ValueError, KeyError, TypeError):
            return
        db.refresh(ci)
        ci.ai_match, ci.ai_confidence, ci.ai_reason = match, confidence, str(verdict.get("reason", ""))[:200]
        db.add(ci)
        if match and confidence >= AI_CONFIDENCE_MIN and ci.status == "pending":
            verify_checkin(db, ci, "ai")
        db.commit()
        log_event("ai_verdict", checkin=checkin_id, match=match, confidence=confidence)


def ai_coach_tip(user_id: int, ws: date) -> None:
    """Background task: replace the rule-based weekly tip with a Claude-written one (anonymised stats only)."""
    with Session(engine) as db:
        user = db.get(User, user_id)
        tip = db.exec(select(CoachTip).where(CoachTip.user_id == user_id, CoachTip.week_start == ws)).first()
        if not user or not tip or tip.source == "ai":
            return
        s = coach_stats(db, user)
        rates = [r for r in habit_rates(db, user)]
        summary = {
            "total_checkins": s.total_checkins,
            "misses_by_weekday": dict(zip(game.WEEKDAYS, s.misses_by_weekday)),
            "checkins_by_hour": {f"{h:02d}": n for h, n in enumerate(s.checkins_by_hour) if n},
            "habits": [{"category": r["habit"].category, "completion_pct_30d": r["pct"],
                        "has_two_minute_version": bool(r["habit"].tiny_version)} for r in rates],
            "best_current_streak": s.best_streak_now,
            "avg_mood_1_to_5": round(s.avg_mood, 1) if s.avg_mood else None,
        }
        text = claude_text(
            system=("You are the coach in a social habit-tracking app. Write ONE friendly, specific tip "
                    "(max 35 words, plain text, no markdown, no greeting) based on the player's stats."),
            content=[{"type": "text", "text": json.dumps(summary)}], max_tokens=2000)
        if text:
            tip.text, tip.source = text[:300], "ai"
            db.add(tip)
            db.commit()


# ===========================================================================
# View models
# ===========================================================================


def quest_view(db: Session, user: User, habit: Habit, today: date, hour: int) -> dict[str, Any]:
    ci = today_checkin(db, habit, today)
    due = is_due_today(db, habit, today)
    stacked = db.exec(select(Habit).where(Habit.stack_after_id == habit.id, Habit.archived == False)).all()  # noqa: E712
    next_up = [h for h in stacked if not today_checkin(db, h, today)] if ci else []
    parent = db.get(Habit, habit.stack_after_id) if habit.stack_after_id else None
    return {
        "h": habit, "ci": ci, "due": due, "done": ci is not None,
        "show_tiny": bool(habit.tiny_version) and not ci and due and hour >= 18,
        "week_count": week_count(db, habit, today) if habit.frequency == "weekly" else None,
        "next_up": next_up, "parent": parent,
        "parent_done": bool(parent and today_checkin(db, parent, today)),
        "deadline_left": _deadline_left(ci),
    }


def _deadline_left(ci: Optional[CheckIn]) -> str:
    if not ci or ci.status != "flagged" or not ci.reprove_deadline:
        return ""
    secs = max(0, int((ci.reprove_deadline - utcnow()).total_seconds()))
    return f"{secs // 3600}H {secs % 3600 // 60:02d}M"


def checkin_view(db: Session, ci: CheckIn, viewer: User, users: dict[int, User], habits: dict[int, Habit],
                 taunt_cache: dict[int, tuple[bool, str]]) -> dict[str, Any]:
    owner = users.get(ci.user_id) or db.get(User, ci.user_id)
    habit = habits.get(ci.habit_id) or db.get(Habit, ci.habit_id)
    reacts = db.exec(select(Reaction).where(Reaction.checkin_id == ci.id)).all()
    counts = {e: 0 for e in game.REACTIONS}
    mine = set()
    for r in reacts:
        counts[r.emoji] = counts.get(r.emoji, 0) + 1
        if r.user_id == viewer.id:
            mine.add(r.emoji)
    votes = db.exec(select(Vote).where(Vote.checkin_id == ci.id)).all()
    my_vote = next((v.value for v in votes if v.user_id == viewer.id), None)
    if owner and owner.id != viewer.id and owner.id not in taunt_cache:
        taunt_cache[owner.id] = taunt_state(db, viewer, owner)
    return {
        "ci": ci, "owner": owner, "habit": habit, "reactions": counts, "my_reactions": mine,
        "legit": sum(1 for v in votes if v.value == "legit"), "sus": sum(1 for v in votes if v.value == "sus"),
        "my_vote": my_vote,
        "can_vote": ci.user_id != viewer.id and ci.status == "pending" and my_vote is None and "photo" in ci.methods,
        "can_taunt": bool(owner and owner.id != viewer.id and taunt_cache.get(owner.id, (False,))[0]),
        "mine": ci.user_id == viewer.id,
    }


def feed_items(db: Session, viewer: User, limit: int = 30) -> list[dict[str, Any]]:
    ids = sorted(friend_ids(db, viewer.id) | {viewer.id})
    parties = user_party_ids(db, viewer.id)
    cis = db.exec(select(CheckIn).where(col(CheckIn.user_id).in_(ids)).order_by(col(CheckIn.created_at).desc()).limit(limit)).all()
    cond = col(FeedEvent.user_id).in_(ids)
    if parties:
        cond = or_(cond, col(FeedEvent.party_id).in_(parties))
    events = db.exec(select(FeedEvent).where(cond).order_by(col(FeedEvent.created_at).desc()).limit(15)).all()
    users = users_by_ids(db, ids)
    habits = {h.id: h for h in db.exec(select(Habit).where(col(Habit.id).in_([c.habit_id for c in cis] or [0]))).all()}
    taunt_cache: dict[int, tuple[bool, str]] = {}
    items: list[dict[str, Any]] = [{"type": "checkin", "at": c.created_at, **checkin_view(db, c, viewer, users, habits, taunt_cache)}
                                   for c in cis]
    items += [{"type": "event", "at": e.created_at, "e": e} for e in events]
    items.sort(key=lambda it: it["at"], reverse=True)
    return items[:limit]


def tauntable_friends(db: Session, viewer: User) -> list[User]:
    out = []
    for u in users_by_ids(db, friend_ids(db, viewer.id)).values():
        if taunt_state(db, viewer, u)[0]:
            out.append(u)
    return sorted(out, key=lambda u: u.username.lower())


def pet_view(party: Party) -> dict[str, Any]:
    stage, mood = game.pet_stage(party.pet_points), game.pet_mood(party.pet_health)
    return {"svg": game.pet_svg(stage, mood), "stage": stage, "mood": mood, "health": party.pet_health,
            "points": party.pet_points, "next": game.pet_next_stage_at(party.pet_points),
            "segments": game.segments(party.pet_health, 10)}


def boss_view(db: Session, boss: BossBattle, viewer: User) -> dict[str, Any]:
    b = game.BOSS_MAP[boss.boss_key]
    pct = 100 * boss.hp / boss.hp_max if boss.hp_max else 0
    ends = datetime.combine(boss.week_start + timedelta(days=7), time.min)
    today = local_today(viewer)
    hits = db.exec(select(BossHit).where(BossHit.boss_id == boss.id).order_by(col(BossHit.created_at).desc()).limit(12)).all()
    users = users_by_ids(db, {h.user_id for h in hits})
    dmg_by_user: dict[int, int] = defaultdict(int)
    for h in db.exec(select(BossHit).where(BossHit.boss_id == boss.id)).all():
        dmg_by_user[h.user_id] += h.damage
    top = sorted(dmg_by_user.items(), key=lambda kv: -kv[1])[:5]
    top_users = users_by_ids(db, [uid for uid, _ in top])
    return {"boss": boss, "b": b, "pct": pct, "segments": game.segments(pct, 20),
            "days_left": max(0, (boss.week_start + timedelta(days=6) - today).days),
            "last_day": boss.week_start + timedelta(days=6) <= today,
            "hits": [{"user": users.get(h.user_id), "damage": h.damage, "at": h.created_at} for h in hits],
            "top": [{"user": top_users.get(uid), "damage": d} for uid, d in top], "ends": ends}


# ===========================================================================
# Pydantic input models
# ===========================================================================


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    field = ".".join(str(p) for p in err.get("loc", ())) or "input"
    return f"{field.replace('_', ' ')}: {err.get('msg', 'invalid')}".upper()


class SignupIn(BaseModel):
    username: str = PField(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9_]+$")
    email: str = PField(min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = PField(min_length=8, max_length=128)


class HabitIn(BaseModel):
    name: str = PField(min_length=1, max_length=40)
    category: Literal["Gym", "Study", "Music", "Reading", "Sleep", "Meditation", "Custom"] = "Custom"
    icon: str = PField(default="⭐", min_length=1, max_length=8)
    frequency: Literal["daily", "weekly", "days"] = "daily"
    times_per_week: int = PField(default=3, ge=1, le=7)
    days: list[int] = []
    difficulty: Literal["easy", "normal", "hard"] = "normal"
    reminder_time: str = PField(default="", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")
    proof_methods: list[Literal["photo", "gps", "qr", "nfc", "timer"]] = PField(min_length=1)
    min_minutes: int = PField(default=10, ge=1, le=180)
    tiny_version: str = PField(default="", max_length=80)
    stack_after_id: Optional[int] = None

    @field_validator("name", "tiny_version", "icon")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be 0-6")
        return sorted(set(v))

    def check(self) -> None:
        if self.frequency == "days" and not self.days:
            raise GameError("Pick at least one day for this quest.")
        if not self.name:
            raise GameError("Name your quest.")


class SettingsIn(BaseModel):
    bio: str = PField(default="", max_length=160)
    avatar_color: str
    avatar_hat: str
    tz: str
    global_opt_in: bool = False


class DuelIn(BaseModel):
    opponent: str = PField(min_length=3, max_length=20)
    days: int = PField(ge=3, le=30)


class ChallengeIn(BaseModel):
    party_id: int
    name: str = PField(min_length=3, max_length=40)
    days: int = PField(ge=3, le=30)
    stake: int = PField(ge=0, le=1000)
    required: int = PField(ge=1, le=30)


def validated(model: type[BaseModel], **data: Any) -> Any:
    try:
        return model(**data)
    except ValidationError as exc:
        raise GameError(_first_error(exc), 422)


def parse_float(v: Optional[str]) -> Optional[float]:
    try:
        f = float(v) if v not in (None, "") else None
    except ValueError:
        return None
    return f if f is not None and math.isfinite(f) else None


# ===========================================================================
# App, middleware, error pages
# ===========================================================================

scheduler: Any = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_scheduler()
    log_event("startup", db=DATABASE_URL.split("@")[-1], ai=bool(ANTHROPIC_API_KEY), push=PUSH_ENABLED)
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Habitor", lifespan=lifespan, dependencies=[Depends(csrf_protect)],
              docs_url="/docs" if DEBUG else None, redoc_url=None, openapi_url="/openapi.json" if DEBUG else None)
app.add_middleware(CoreMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _toast_response(request: Request, message: str, status: int) -> HTMLResponse:
    resp = HTMLResponse(render_partial(request, "toast", text=message, kind="bad"), status_code=status)
    resp.headers["HX-Retarget"] = "#toasts"
    resp.headers["HX-Reswap"] = "beforeend"
    resp.headers["HX-Trigger"] = json.dumps({"habitor": {"sfx": "error", "shake": True}})
    return resp


@app.exception_handler(GameError)
async def game_error_handler(request: Request, exc: GameError):
    if is_hx(request) or request.headers.get("accept", "").startswith("application/json"):
        return _toast_response(request, exc.message, exc.status)
    resp = redirect(safe_back(request))
    return deliver(request, resp, Fx().toast(exc.message, "bad").sfx("error").set("shake"))


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, _exc: LoginRequired):
    if is_hx(request):
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    nxt = request.url.path if request.method == "GET" else "/"
    return redirect(f"/login?next={quote(nxt)}")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if is_hx(request):
        return _toast_response(request, str(exc.detail).upper(), exc.status_code)
    return error_page(request, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = f"Invalid input: {'.'.join(str(p) for p in first.get('loc', ())[1:])} {first.get('msg', '')}".strip().upper()
    if is_hx(request):
        return _toast_response(request, msg, 422)
    return error_page(request, 422, msg)


@app.exception_handler(Exception)
async def server_error_handler(request: Request, exc: Exception):
    log.exception("unhandled error", extra={"fields": {"path": request.url.path}})
    if is_hx(request):
        return _toast_response(request, "Something went wrong. Please try again.", 500)
    return error_page(request, 500, "Something broke on our side.")


def error_page(request: Request, status: int, detail: str) -> HTMLResponse:
    if not hasattr(request.state, "session"):
        request.state.session = {}
    try:
        return render(request, "error", status_code=status, status=status, detail=detail)
    except Exception:  # pragma: no cover - last-resort fallback
        return HTMLResponse(f"<h1>GAME OVER — {status}</h1><p><a href='/'>CONTINUE?</a></p>", status_code=status)


# ===========================================================================
# Routes — system, PWA, media
# ===========================================================================


@app.get("/health")
def health() -> JSONResponse:
    try:
        with Session(engine) as db:
            db.exec(select(1)).one()
        return JSONResponse({"status": "ok", "db": True, "ai": bool(ANTHROPIC_API_KEY), "push": PUSH_ENABLED})
    except Exception:
        return JSONResponse({"status": "degraded", "db": False}, status_code=503)


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
def manifest() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
def icon_svg() -> Response:
    return Response(game.app_icon_svg(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


_icon_cache: dict[int, bytes] = {}


@app.get("/icon-{size}.png")
def icon_png(size: int) -> Response:
    if size not in (180, 192, 512):
        raise StarletteHTTPException(404, "No such icon")
    if size not in _icon_cache:
        img = Image.new("RGB", (size, size), game.ICON_BACKGROUND)  # full bleed: the OS applies its own mask
        draw = ImageDraw.Draw(img)
        for x0, y0, x1, y1, c in game.ICON_SHAPES:
            draw.rectangle([round(x0 * size), round(y0 * size), round(x1 * size) - 1, round(y1 * size) - 1], fill=c)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _icon_cache[size] = buf.getvalue()
    return Response(_icon_cache[size], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/avatar/{username}.svg")
def avatar(username: str, db: DB) -> Response:
    user = db.exec(select(User).where(func.lower(User.username) == username.lower())).first()
    svg = game.avatar_svg(user.username, user.avatar_color, user.avatar_hat) if user else game.avatar_svg(username)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=300"})


@app.get("/media/{path:path}")
def media(path: str, db: DB, me: Me) -> FileResponse:
    """Proof photos are only served to their owner, friends and party-mates."""
    full = (UPLOAD_DIR / path).resolve()
    if not full.is_relative_to(UPLOAD_DIR) or not full.is_file():
        raise StarletteHTTPException(404, "Photo not found")
    owner_id = db.exec(select(CheckIn.user_id).where(CheckIn.photo_path == path)).first() or \
        db.exec(select(DareClaim.user_id).where(DareClaim.photo_path == path)).first()
    if owner_id is None or not can_see(db, me, owner_id):
        raise StarletteHTTPException(404, "Photo not found")
    return FileResponse(full, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request) -> HTMLResponse:
    request.state.user = None
    return render(request, "offline")


# ===========================================================================
# Routes — auth
# ===========================================================================


def login_session(request: Request, user: User) -> None:
    data = session(request)
    invite = data.get("invite")
    data.clear()
    data.update({"uid": user.id, "csrf": secrets.token_urlsafe(32)})  # rotate on login
    if invite:
        data["invite"] = invite
    touch_session(request)


def consume_invite(request: Request, db: Session, user: User, fx: Fx) -> None:
    code = session(request).pop("invite", None)
    if not code:
        return
    touch_session(request)
    inviter = db.exec(select(User).where(User.invite_code == code)).first()
    if inviter and inviter.id != user.id and not are_friends(db, inviter.id, user.id):
        make_friends(db, inviter, user)
        notify(db, inviter.id, "friend", f"🤝 {user.username} joined via your invite — you're now friends!", f"/u/{user.username}")
        fx.toast(f"🤝 Now friends with {inviter.username}", "good")
        db.commit()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: DB, me: MaybeMe, bg: BackgroundTasks) -> HTMLResponse:
    if not me:
        return render(request, "auth", mode="signup")
    return home(request, db, me, bg)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, me: MaybeMe, next: str = "/") -> Response:
    if me:
        return redirect("/")
    return render(request, "auth", mode="login", next=safe_next(next))


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, me: MaybeMe) -> Response:
    if me:
        return redirect("/")
    return render(request, "auth", mode="signup")


@app.post("/login")
def login(request: Request, db: DB, username: str = Form(""), password: str = Form(""), next: str = Form("/")) -> Response:
    rate_limit(f"login:{client_ip(request)}", 10, 300)
    user = db.exec(select(User).where(or_(func.lower(User.username) == username.strip().lower(),
                                          func.lower(User.email) == username.strip().lower()))).first()
    if not user:
        verify_password(_DUMMY_HASH, password)  # equalise timing
        return render(request, "auth", mode="login", error="Wrong username or password", status_code=400, next=safe_next(next))
    if not verify_password(user.password_hash, password):
        return render(request, "auth", mode="login", error="Wrong username or password", status_code=400, next=safe_next(next))
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
    login_session(request, user)
    fx = Fx().toast(f"Welcome back, {user.username}.", "good").sfx("coin")
    consume_invite(request, db, user, fx)
    target = safe_next(next)
    return deliver(request, redirect(target), fx)


@app.post("/signup")
def signup(request: Request, db: DB, username: str = Form(""), email: str = Form(""), password: str = Form("")) -> Response:
    rate_limit(f"signup:{client_ip(request)}", 5, 3600)
    try:
        data = SignupIn(username=username.strip(), email=email.strip().lower(), password=password)
    except ValidationError as exc:
        return render(request, "auth", mode="signup", error=_first_error(exc), status_code=400)
    clash = db.exec(select(User).where(or_(func.lower(User.username) == data.username.lower(),
                                           func.lower(User.email) == data.email))).first()
    if clash:
        return render(request, "auth", mode="signup", error="Username or email already taken", status_code=400)
    user = User(username=data.username, email=data.email, password_hash=hash_password(data.password),
                invite_code=new_code(), avatar_color=random.choice(game.AVATAR_COLORS), last_rollover=None)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(request, "auth", mode="signup", error="Username or email already taken", status_code=400)
    db.refresh(user)
    login_session(request, user)
    fx = Fx().toast("Welcome to Habitor. Let's set up your first quest.", "gold").sfx("coin")
    consume_invite(request, db, user, fx)
    log_event("signup", user=user.id)
    return deliver(request, redirect("/habits?welcome=1"), fx)


@app.post("/logout")
def logout(request: Request) -> Response:
    session(request).clear()
    session(request)["csrf"] = secrets.token_urlsafe(32)
    touch_session(request)
    return redirect("/login")


@app.get("/invite/{code}")
def invite(code: str, request: Request, db: DB, me: MaybeMe) -> Response:
    """Friend code link (shared, scanned as a QR code or tapped from an NFC tag)."""
    inviter = db.exec(select(User).where(User.invite_code == code)).first()
    if not inviter:
        raise StarletteHTTPException(404, "Invite code not found")
    if me:
        if me.id == inviter.id:
            return redirect("/friends")
        return render(request, "invite_confirm", inviter=inviter, already=are_friends(db, me.id, inviter.id))
    session(request)["invite"] = code
    touch_session(request)
    return deliver(request, redirect("/signup"),
                   Fx().toast(f"{inviter.username} invited you. Create an account to connect.", "gold"))


@app.post("/friends/connect")
def friends_connect(request: Request, db: DB, me: Me, code: str = Form(...)) -> Response:
    """Connect instantly with the owner of a friend code (scanned QR, tapped NFC tag or pasted link)."""
    rate_limit(f"connect:{me.id}", 20, 60)
    code = code.strip().split("/invite/")[-1].split("?")[0].strip("/")
    inviter = db.exec(select(User).where(User.invite_code == code)).first() if code else None
    if not inviter:
        raise GameError("That friend code wasn't recognised.", 404)
    if inviter.id == me.id:
        raise GameError("That's your own friend code.")
    fx = Fx()
    if are_friends(db, me.id, inviter.id):
        fx.toast(f"You're already friends with {inviter.username}.", "good")
    else:
        make_friends(db, inviter, me)
        notify(db, inviter.id, "friend", f"🤝 {me.username} connected with you using your friend code.", f"/u/{me.username}")
        fx.toast(f"You're now friends with {inviter.username}.", "good").sfx("coin")
        db.flush()
        check_badges(db, me, fx)
        check_badges(db, inviter)
        db.commit()
    target = f"/u/{inviter.username}"
    if is_hx(request):
        session(request)["fx"] = fx.payload()
        touch_session(request)
        return Response(status_code=204, headers={"HX-Redirect": target})
    return deliver(request, redirect(target), fx)


# ===========================================================================
# Routes — home, habits, check-ins
# ===========================================================================


def home(request: Request, db: Session, me: User, bg: BackgroundTasks) -> HTMLResponse:
    now = local_now(me)
    today = now.date()
    quests = [quest_view(db, me, h, today, now.hour) for h in active_habits(db, me)]
    quests.sort(key=lambda q: (q["done"], not q["due"]))
    parties = [db.get(Party, pid) for pid in user_party_ids(db, me.id)]
    dares = []
    for p in parties:
        if p:
            claimed = db.exec(select(DareClaim.id).where(DareClaim.party_id == p.id, DareClaim.user_id == me.id,
                                                         DareClaim.day == today)).first()
            dares.append({"party": p, "dare": game.daily_dare(p.id, today), "claimed": bool(claimed)})
    first_party = parties[0] if parties and parties[0] else None
    boss = boss_view(db, ensure_boss(db, first_party), me) if first_party else None
    db.commit()
    stickers = db.exec(select(Taunt).where(Taunt.target_id == me.id, Taunt.resolved == False)  # noqa: E712
                       .order_by(col(Taunt.created_at).desc())).all()
    senders = users_by_ids(db, {t.sender_id for t in stickers})
    trials = [(ci, db.get(Habit, ci.habit_id), _deadline_left(ci))
              for ci in db.exec(select(CheckIn).where(CheckIn.user_id == me.id, CheckIn.status == "flagged",
                                                      CheckIn.local_date != today)).all()]
    prove_active = bool(me.prove_it_until and me.prove_it_until > utcnow())
    return render(request, "home", needs=["camera", "qr"], quests=quests, dares=dares, boss=boss,
                  pet=pet_view(first_party) if first_party else None, party=first_party,
                  tip=get_coach_tip(db, me, bg), stickers=[(t, senders.get(t.sender_id)) for t in stickers],
                  prove_active=prove_active, prove_seconds=int((me.prove_it_until - utcnow()).total_seconds()) if prove_active else 0,
                  today=today, hour=now.hour, targets=tauntable_friends(db, me)[:6], trials=trials)


@app.get("/habits", response_class=HTMLResponse)
def habits_page(request: Request, db: DB, me: Me, welcome: int = 0) -> HTMLResponse:
    return render(request, "habits", habits=active_habits(db, me), welcome=bool(welcome), form=None,
                  archived=db.exec(select(Habit).where(Habit.user_id == me.id, Habit.archived == True)).all())  # noqa: E712


def _habit_from_form(db: Session, me: User, data: HabitIn, habit: Optional[Habit] = None) -> Habit:
    data.check()
    habit = habit or Habit(user_id=me.id, name=data.name)
    if data.stack_after_id:
        parent = db.get(Habit, data.stack_after_id)
        if not parent or parent.user_id != me.id or (habit.id and parent.id == habit.id):
            raise GameError("Invalid habit stack.")
    habit.name, habit.category, habit.icon = data.name, data.category, data.icon
    habit.frequency, habit.times_per_week, habit.days_csv = data.frequency, data.times_per_week, ",".join(map(str, data.days))
    habit.difficulty, habit.reminder_time = data.difficulty, data.reminder_time
    habit.proof_methods = ",".join(dict.fromkeys(data.proof_methods))
    habit.min_minutes, habit.tiny_version, habit.stack_after_id = data.min_minutes, data.tiny_version, data.stack_after_id
    return habit


@app.get("/habits/new", response_class=HTMLResponse)
def habit_new(request: Request, db: DB, me: Me) -> HTMLResponse:
    return render(request, "habit_form", habit=None, others=active_habits(db, me), needs=["leaflet"])


@app.post("/habits")
def habit_create(request: Request, db: DB, me: Me, name: str = Form(""), category: str = Form("Custom"),
                 icon: str = Form("⭐"), frequency: str = Form("daily"), times_per_week: int = Form(3),
                 days: list[int] = Form([]), difficulty: str = Form("normal"), reminder_time: str = Form(""),
                 proof_methods: list[str] = Form([]), min_minutes: int = Form(10), tiny_version: str = Form(""),
                 stack_after_id: str = Form("")) -> Response:
    if len(active_habits(db, me)) >= 30:
        raise GameError("Quest log full (30 max)")
    data = validated(HabitIn, name=name, category=category, icon=icon, frequency=frequency, times_per_week=times_per_week,
                     days=days, difficulty=difficulty, reminder_time=reminder_time, proof_methods=proof_methods,
                     min_minutes=min_minutes, tiny_version=tiny_version,
                     stack_after_id=int(stack_after_id) if stack_after_id.isdigit() else None)
    habit = _habit_from_form(db, me, data)
    db.add(habit)
    db.commit()
    db.refresh(habit)
    nxt = f"/habits/{habit.id}/edit" if ("gps" in habit.methods or "qr" in habit.methods) else "/"
    return deliver(request, redirect(nxt), Fx().toast(f"New quest: {habit.icon} {habit.name}", "gold").sfx("coin"))


@app.post("/habits/template/{key}")
def habit_template(key: str, request: Request, db: DB, me: Me) -> Response:
    tpl = game.HABIT_TEMPLATES.get(key)
    if not tpl:
        raise StarletteHTTPException(404, "Template not found")
    for spec in tpl["habits"]:
        db.add(Habit(user_id=me.id, name=spec["name"], category=spec["category"], icon=spec["icon"],
                     frequency=spec["frequency"], times_per_week=spec.get("times_per_week", 1),
                     days_csv=",".join(map(str, spec.get("days", []))), difficulty=spec["difficulty"],
                     reminder_time=spec.get("reminder", ""), proof_methods=",".join(spec["proof"]),
                     min_minutes=spec.get("min_minutes", 10), tiny_version=spec.get("tiny", "")))
    db.commit()
    return deliver(request, redirect("/"), Fx().toast(f"{tpl['icon']} {tpl['title']} loaded!", "gold").sfx("coin"))


@app.get("/habits/{habit_id}/edit", response_class=HTMLResponse)
def habit_edit(habit_id: int, request: Request, db: DB, me: Me) -> HTMLResponse:
    habit = owned_habit(db, me, habit_id)
    others = [h for h in active_habits(db, me) if h.id != habit.id]
    return render(request, "habit_form", habit=habit, others=others, needs=["leaflet", "qrgen"],
                  qr_url=checkpoint_url(habit, "qr"), nfc_url=checkpoint_url(habit, "nfc"))


@app.post("/habits/{habit_id}")
def habit_update(habit_id: int, request: Request, db: DB, me: Me, name: str = Form(""), category: str = Form("Custom"),
                 icon: str = Form("⭐"), frequency: str = Form("daily"), times_per_week: int = Form(3),
                 days: list[int] = Form([]), difficulty: str = Form("normal"), reminder_time: str = Form(""),
                 proof_methods: list[str] = Form([]), min_minutes: int = Form(10), tiny_version: str = Form(""),
                 stack_after_id: str = Form("")) -> Response:
    habit = owned_habit(db, me, habit_id)
    data = validated(HabitIn, name=name, category=category, icon=icon, frequency=frequency, times_per_week=times_per_week,
                     days=days, difficulty=difficulty, reminder_time=reminder_time, proof_methods=proof_methods,
                     min_minutes=min_minutes, tiny_version=tiny_version,
                     stack_after_id=int(stack_after_id) if stack_after_id.isdigit() else None)
    _habit_from_form(db, me, data, habit)
    db.add(habit)
    refresh_streak(db, habit, me)
    db.commit()
    return deliver(request, redirect(f"/habits/{habit.id}/edit"), Fx().toast("Quest saved 💾", "good"))


@app.post("/habits/{habit_id}/location")
def habit_location(habit_id: int, request: Request, db: DB, me: Me, lat: str = Form(""), lon: str = Form(""),
                   label: str = Form(""), clear: str = Form("")) -> Response:
    habit = owned_habit(db, me, habit_id)
    if clear:
        habit.gps_lat = habit.gps_lon = None
        habit.gps_label = ""
    else:
        la, lo = parse_float(lat), parse_float(lon)
        if not game.valid_coords(la, lo):
            raise GameError("Tap the map to drop a pin first.")
        habit.gps_lat, habit.gps_lon, habit.gps_label = la, lo, label.strip()[:40]
    db.add(habit)
    db.commit()
    return deliver(request, redirect(f"/habits/{habit.id}/edit"), Fx().toast("📍 Location saved" if not clear else "Location cleared"))


@app.post("/habits/{habit_id}/archive")
def habit_archive(habit_id: int, request: Request, db: DB, me: Me) -> Response:
    habit = owned_habit(db, me, habit_id)
    habit.archived = not habit.archived
    db.add(habit)
    db.commit()
    return deliver(request, redirect("/habits"), Fx().toast("Quest archived 📦" if habit.archived else "Quest restored ♻️"))


@app.post("/habits/{habit_id}/qr/rotate")
def habit_qr_rotate(habit_id: int, request: Request, db: DB, me: Me) -> Response:
    habit = owned_habit(db, me, habit_id)
    habit.qr_secret = secrets.token_urlsafe(8)
    db.add(habit)
    db.commit()
    return deliver(request, redirect(f"/habits/{habit.id}/qr"), Fx().toast("New checkpoint codes issued. Reprint the QR and rewrite the NFC tag.", "gold"))


@app.get("/habits/{habit_id}/qr", response_class=HTMLResponse)
def habit_qr(habit_id: int, request: Request, db: DB, me: Me) -> HTMLResponse:
    habit = owned_habit(db, me, habit_id)
    return render(request, "habit_qr", habit=habit, needs=["qrgen"],
                  qr_url=checkpoint_url(habit, "qr"), nfc_url=checkpoint_url(habit, "nfc"))


@app.get("/habits/{habit_id}/timer", response_class=HTMLResponse)
def habit_timer(habit_id: int, request: Request, db: DB, me: Me) -> HTMLResponse:
    habit = owned_habit(db, me, habit_id)
    done = today_checkin(db, habit, local_today(me))
    return render(request, "timer", habit=habit, done=done)


def checkpoint_url(habit: Habit, kind: str) -> str:
    """Signed URL for a quest checkpoint: printed as a QR code or written to an NFC tag where the habit happens."""
    return f"{BASE_URL}/{kind}/{CHECKPOINT_SIGNERS[kind].dumps([habit.id, habit.qr_secret])}"


def _checkpoint_habit(db: Session, me: User, token: str, kind: str = "qr") -> Habit:
    """Resolve a scanned QR / tapped NFC token to the player's own quest, or explain why not."""
    label = kind.upper()
    raw = token.strip().split(f"/{kind}/")[-1].split("?")[0]
    try:
        hid, secret = CHECKPOINT_SIGNERS[kind].loads(raw)
    except (BadSignature, ValueError, TypeError):
        raise GameError(f"That isn't a valid Habitor {label} checkpoint.", 400)
    habit = db.get(Habit, hid)
    if not habit or habit.user_id != me.id or not secrets.compare_digest(str(secret), habit.qr_secret):
        raise GameError(f"This {label} checkpoint belongs to a different quest.", 403)
    if kind not in habit.methods:
        raise GameError(f"{label} verification isn't enabled for this quest.")
    return habit


def _commit_checkin(db: Session, photo_path: Optional[str]) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        delete_photo(photo_path)
        raise GameError("Quest already complete today.", 409)


def _checkin_response(request: Request, db: Session, me: User, habit: Habit, fx: Fx) -> Response:
    if is_hx(request):
        now = local_now(me)
        html = render_partial(request, "quest_card", q=quest_view(db, me, habit, now.date(), now.hour))
        html += render_partial(request, "hud", hud=hud_data(db, me), oob=True)
        return deliver(request, HTMLResponse(html), fx)
    return deliver(request, redirect("/"), fx)


@app.post("/habits/{habit_id}/checkin")
def checkin(habit_id: int, request: Request, bg: BackgroundTasks, db: DB, me: Me, method: str = Form(...),
            photo: Optional[UploadFile] = File(None), lat: str = Form(""), lon: str = Form(""),
            qr_token: str = Form(""), nfc_token: str = Form(""), timer_sid: str = Form(""),
            prove_it: str = Form("")) -> Response:
    rate_limit(f"checkin:{me.id}", 20, 60)
    habit = owned_habit(db, me, habit_id)
    if habit.archived:
        raise GameError("This quest is archived.")
    if method not in habit.methods:
        raise GameError("That proof method isn't enabled for this quest.")
    prove = prove_it == "1"
    if prove and (method != "photo" or not me.prove_it_until or utcnow() > me.prove_it_until + timedelta(seconds=15)):
        raise GameError("The Prove It Now window has closed.", 410)
    if today_checkin(db, habit, local_today(me)):
        raise GameError("Quest already complete today.", 409)
    la, lo = parse_float(lat), parse_float(lon)
    methods, status, verified_by = [method], "pending", ""
    distance: Optional[float] = None
    photo_path: Optional[str] = None
    timer_minutes: Optional[int] = None
    if method == "photo":
        if photo is None or not photo.filename:
            raise GameError("Snap a live photo first.")
        photo_path = save_photo(photo)
        if habit.has_location and game.valid_coords(la, lo):
            distance = game.haversine_m(la, lo, habit.gps_lat, habit.gps_lon)  # type: ignore[arg-type]
            if distance <= game.GPS_RADIUS_M:
                methods.append("gps")
                status, verified_by = "verified", "gps"
    elif method == "gps":
        if not habit.has_location:
            raise GameError("Set this quest's location first.")
        if not game.valid_coords(la, lo):
            raise GameError("Couldn't get your location — allow GPS.")
        distance = game.haversine_m(la, lo, habit.gps_lat, habit.gps_lon)  # type: ignore[arg-type]
        if distance > game.GPS_RADIUS_M:
            raise GameError(f"Too far! You're {distance:,.0f} m away (max {game.GPS_RADIUS_M} m)")
        status, verified_by = "verified", "gps"
    elif method in ("qr", "nfc"):
        token = qr_token if method == "qr" else nfc_token
        if _checkpoint_habit(db, me, token, method).id != habit.id:
            raise GameError("That checkpoint belongs to a different quest.")
        status, verified_by = "verified", method
    elif method == "timer":
        ts = db.get(TimerSession, timer_sid) if timer_sid else None
        if not ts or ts.user_id != me.id or ts.habit_id != habit.id or ts.used:
            raise GameError("Start a focus session first.")
        now = utcnow()
        elapsed = (now - ts.started_at).total_seconds()
        gap = max(ts.max_gap_s, int((now - ts.last_ping_at).total_seconds()))
        if elapsed + 5 < habit.min_minutes * 60:
            raise GameError(f"Keep going! Need {habit.min_minutes} min of focus.")
        if gap > TIMER_MAX_GAP_S and habit.min_minutes * 60 > TIMER_MAX_GAP_S:
            raise GameError("Session failed — you went AFK.")
        ts.used = True
        db.add(ts)
        timer_minutes = int(elapsed // 60)
        status, verified_by = "verified", "timer"
    else:
        raise GameError("Unknown proof method.")
    try:
        ci, fx = perform_checkin(db, me, habit, methods=methods, status=status, verified_by=verified_by,
                                 photo_path=photo_path, lat=la, lon=lo, distance=distance,
                                 timer_minutes=timer_minutes, prove_it=prove)
    except IntegrityError:
        db.rollback()
        delete_photo(photo_path)
        raise GameError("Quest already complete today.", 409)
    except GameError:
        db.rollback()
        delete_photo(photo_path)
        raise
    if distance is not None:
        fx.toast(f"📍 {distance:,.0f} m from {habit.gps_label or 'your saved spot'}", "good" if distance <= game.GPS_RADIUS_M else "pink")
    _commit_checkin(db, photo_path)
    if photo_path and status == "pending" and ANTHROPIC_API_KEY:
        bg.add_task(ai_verify_checkin, ci.id)
    return _checkin_response(request, db, me, habit, fx)


def _checkpoint_landing(kind: str, token: str, request: Request, db: Session, me: User) -> HTMLResponse:
    habit = _checkpoint_habit(db, me, token, kind)
    return render(request, "qr_confirm", habit=habit, token=token, kind=kind,
                  done=today_checkin(db, habit, local_today(me)))


@app.get("/qr/{token}", response_class=HTMLResponse)
def qr_landing(token: str, request: Request, db: DB, me: Me) -> HTMLResponse:
    """Opened by any phone camera that scans a printed quest QR code."""
    return _checkpoint_landing("qr", token, request, db, me)


@app.get("/nfc/{token}", response_class=HTMLResponse)
def nfc_landing(token: str, request: Request, db: DB, me: Me) -> HTMLResponse:
    """Opened when a phone taps an NFC tag that holds a quest checkpoint URL (works on iPhone and Android)."""
    return _checkpoint_landing("nfc", token, request, db, me)


def _checkpoint_checkin(kind: str, token: str, request: Request, db: Session, me: User) -> Response:
    rate_limit(f"checkin:{me.id}", 20, 60)
    habit = _checkpoint_habit(db, me, token, kind)
    if habit.archived:
        raise GameError("This quest is archived.")
    try:
        _, fx = perform_checkin(db, me, habit, methods=[kind], status="verified", verified_by=kind)
    except IntegrityError:
        db.rollback()
        raise GameError("Quest already complete today.", 409)
    _commit_checkin(db, None)
    fx.toast(f"{kind.upper()} checkpoint verified", "good")
    if is_hx(request):  # in-app scanner / NFC reader: reload home, effects ride along in the session
        session(request)["fx"] = fx.payload()
        touch_session(request)
        return Response(status_code=204, headers={"HX-Redirect": "/"})
    return deliver(request, redirect("/"), fx)


@app.post("/qr/checkin")
def qr_checkin(request: Request, db: DB, me: Me, token: str = Form(...)) -> Response:
    return _checkpoint_checkin("qr", token, request, db, me)


@app.post("/nfc/checkin")
def nfc_checkin(request: Request, db: DB, me: Me, token: str = Form(...)) -> Response:
    return _checkpoint_checkin("nfc", token, request, db, me)


@app.post("/timer/start")
def timer_start(db: DB, me: Me, habit_id: int = Form(...)) -> JSONResponse:
    habit = owned_habit(db, me, habit_id)
    ts = TimerSession(id=secrets.token_urlsafe(16), user_id=me.id, habit_id=habit.id)
    db.add(ts)
    db.commit()
    return JSONResponse({"sid": ts.id, "min_minutes": habit.min_minutes, "timer_proof": "timer" in habit.methods})


@app.post("/timer/{sid}/ping")
def timer_ping(sid: str, db: DB, me: Me) -> JSONResponse:
    ts = db.get(TimerSession, sid)
    if not ts or ts.user_id != me.id or ts.used:
        raise StarletteHTTPException(404, "Session not found")
    now = utcnow()
    ts.max_gap_s = max(ts.max_gap_s, int((now - ts.last_ping_at).total_seconds()))
    ts.last_ping_at = now
    ts.prompts += 1
    db.add(ts)
    db.commit()
    return JSONResponse({"ok": True, "elapsed": int((now - ts.started_at).total_seconds())})


def _visible_checkin(db: Session, me: User, checkin_id: int) -> CheckIn:
    ci = db.get(CheckIn, checkin_id)
    if not ci or not can_see(db, me, ci.user_id):
        raise StarletteHTTPException(404, "Check-in not found")
    return ci


@app.post("/checkins/{checkin_id}/reflect")
def reflect(checkin_id: int, request: Request, db: DB, me: Me, mood: str = Form(""), energy: str = Form(""),
            note: Optional[str] = Form(None)) -> Response:
    ci = db.get(CheckIn, checkin_id)
    if not ci or ci.user_id != me.id:
        raise StarletteHTTPException(404, "Check-in not found")
    if mood.isdigit() and 1 <= int(mood) <= len(game.MOODS):
        ci.mood = int(mood)
    if energy.isdigit() and 1 <= int(energy) <= len(game.ENERGY):
        ci.energy = int(energy)
    if note is not None:
        ci.note = note.strip()[:200]
    db.add(ci)
    fx = Fx()
    check_badges(db, me, fx)
    db.commit()
    habit = db.get(Habit, ci.habit_id)
    fx.toast("Reflection saved ✍️" if note is not None else "Mood logged", "good")
    return _checkin_response(request, db, me, habit, fx) if habit else redirect("/")


@app.post("/checkins/{checkin_id}/react")
def react(checkin_id: int, request: Request, db: DB, me: Me, emoji: str = Form(...)) -> Response:
    rate_limit(f"react:{me.id}", 60, 60)
    ci = _visible_checkin(db, me, checkin_id)
    if emoji not in game.REACTIONS:
        raise GameError("Unknown reaction.")
    existing = db.exec(select(Reaction).where(Reaction.checkin_id == ci.id, Reaction.user_id == me.id, Reaction.emoji == emoji)).first()
    if existing:
        db.delete(existing)
    else:
        db.add(Reaction(checkin_id=ci.id, user_id=me.id, emoji=emoji))
        if ci.user_id != me.id:
            notify(db, ci.user_id, "reaction", f"{emoji} {me.username} reacted to your check-in", "/feed",
                   dedupe=f"react:{ci.id}:{me.id}:{emoji}", push=False)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    view = checkin_view(db, ci, me, {}, {}, {})
    if is_hx(request):
        return HTMLResponse(render_partial(request, "reactions_bar", it=view))
    return redirect(safe_back(request, "/feed"))


@app.post("/checkins/{checkin_id}/vote")
def vote(checkin_id: int, request: Request, db: DB, me: Me, value: str = Form(...)) -> Response:
    rate_limit(f"vote:{me.id}", 30, 60)
    ci = _visible_checkin(db, me, checkin_id)
    fx = cast_vote(db, me, ci, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise GameError("One vote per proof.", 409)
    db.refresh(ci)
    view = checkin_view(db, ci, me, {}, {}, {})
    if is_hx(request):
        return deliver(request, HTMLResponse(render_partial(request, "vote_bar", it=view)), fx)
    return deliver(request, redirect(safe_back(request, "/feed")), fx)


@app.post("/checkins/{checkin_id}/reprove")
def reprove(checkin_id: int, request: Request, db: DB, me: Me, method: str = Form(...),
            photo: Optional[UploadFile] = File(None), lat: str = Form(""), lon: str = Form(""),
            qr_token: str = Form(""), nfc_token: str = Form("")) -> Response:
    ci = db.get(CheckIn, checkin_id)
    if not ci or ci.user_id != me.id:
        raise StarletteHTTPException(404, "Check-in not found")
    if ci.status != "flagged" or not ci.reprove_deadline or utcnow() > ci.reprove_deadline:
        raise GameError("There's nothing to re-verify, or the 12-hour window has closed.", 409)
    habit = db.get(Habit, ci.habit_id)
    assert habit is not None
    if method in ("qr", "nfc"):
        token = qr_token if method == "qr" else nfc_token
        if _checkpoint_habit(db, me, token, method).id != habit.id:
            raise GameError("That checkpoint belongs to a different quest.")
        ci.methods = ",".join(dict.fromkeys(ci.methods.split(",") + [method]))
    elif method == "photo_gps":
        la, lo = parse_float(lat), parse_float(lon)
        if photo is None or not photo.filename:
            raise GameError("Re-verification needs a live photo.")
        if not game.valid_coords(la, lo):
            raise GameError("Re-verification needs your GPS location.")
        if habit.has_location:
            dist = game.haversine_m(la, lo, habit.gps_lat, habit.gps_lon)  # type: ignore[arg-type]
            if dist > game.GPS_RADIUS_M:
                raise GameError(f"Too far! {dist:,.0f} m away (max {game.GPS_RADIUS_M} m)")
            ci.distance_m = dist
        new_path = save_photo(photo)
        delete_photo(ci.photo_path)
        ci.photo_path, ci.lat, ci.lon = new_path, la, lo
        ci.methods = ",".join(dict.fromkeys(ci.methods.split(",") + ["photo", "gps"]))
    else:
        raise GameError("Re-verify with a live photo and GPS, or your QR/NFC checkpoint.")
    verify_checkin(db, ci, "reprove")
    db.commit()
    fx = Fx().toast("Re-verification accepted. Your streak is safe.", "good").set("stamp", "Streak saved").sfx("checkin")
    if ci.local_date != local_today(me) and is_hx(request):  # an older proof, re-proved from the ON TRIAL list
        return deliver(request, HTMLResponse(render_partial(request, "trial_card", ci=ci, h=habit, deadline_left="")), fx)
    return _checkin_response(request, db, me, habit, fx)


# ===========================================================================
# Routes — feed, taunts, notifications, HUD
# ===========================================================================


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request, db: DB, me: Me) -> HTMLResponse:
    return render(request, "feed", items=feed_items(db, me), targets=tauntable_friends(db, me))


@app.get("/feed/items", response_class=HTMLResponse)
def feed_partial(request: Request, db: DB, me: Me) -> HTMLResponse:
    return HTMLResponse(render_partial(request, "feed_list", items=feed_items(db, me)))


@app.get("/taunt/{username}", response_class=HTMLResponse)
def taunt_menu(username: str, request: Request, db: DB, me: Me) -> HTMLResponse:
    target = get_user_by_name(db, username)
    ok, reason = taunt_state(db, me, target)
    return HTMLResponse(render_partial(request, "taunt_modal", target=target, ok=ok, reason=reason))


@app.post("/taunt/{username}")
def taunt(username: str, request: Request, db: DB, me: Me, key: str = Form(...)) -> Response:
    rate_limit(f"taunt:{me.id}", 10, 60)
    target = get_user_by_name(db, username)
    fx = send_taunt(db, me, target, key)
    db.commit()
    if is_hx(request):
        html = render_partial(request, "taunt_sent", target=target, taunt=game.TAUNTS[key])
        return deliver(request, HTMLResponse(html), fx)
    return deliver(request, redirect(safe_back(request, f"/u/{target.username}")), fx)


@app.get("/hud/events", response_class=HTMLResponse)
def hud_events(request: Request, db: DB, me: Me) -> Response:
    """Polled every ~20s: refreshes the HUD and delivers taunts, Prove It Now and other alerts."""
    fx = Fx()
    for t in db.exec(select(Taunt).where(Taunt.target_id == me.id, Taunt.seen == False)).all():  # noqa: E712
        sender = db.get(User, t.sender_id)
        tt = game.TAUNTS.get(t.key)
        if sender and tt:
            fx.set("taunted", {"from": sender.username.upper(), "sticker": tt.sticker, "text": tt.text, "sound": tt.sound})
        t.seen = True
        db.add(t)
    if me.prove_it_until and me.prove_it_until > utcnow():
        fx.set("proveIt", {"seconds": int((me.prove_it_until - utcnow()).total_seconds()), "url": "/prove"})
    fresh = db.exec(select(Notification).where(Notification.user_id == me.id, Notification.announced == False)  # noqa: E712
                    .order_by(Notification.id).limit(5)).all()
    for n in fresh:
        n.announced = True
        db.add(n)
        if n.kind == "rejected":
            fx.toast(n.text, "bad").set("shake").sfx("error")
        elif n.kind in ("comeback", "boss", "duel", "flagged", "friend", "verified", "challenge", "tile", "dare", "proveit"):
            fx.toast(n.text, "gold" if n.kind in ("boss", "comeback") else "good")
    db.commit()
    html = render_partial(request, "hud", hud=hud_data(db, me), oob=True)
    return deliver(request, HTMLResponse(html), fx)


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, db: DB, me: Me) -> HTMLResponse:
    items = db.exec(select(Notification).where(Notification.user_id == me.id).order_by(col(Notification.created_at).desc()).limit(60)).all()
    unread_ids = [n.id for n in items if not n.read]
    page = render(request, "notifications", items=items, unread_ids=set(unread_ids))
    for n in items:
        if not n.read:
            n.read = True
            n.announced = True
            db.add(n)
    db.commit()
    return page


@app.post("/notifications/read")
def notifications_read(request: Request, db: DB, me: Me) -> Response:
    for n in db.exec(select(Notification).where(Notification.user_id == me.id, Notification.read == False)).all():  # noqa: E712
        n.read = True
        n.announced = True
        db.add(n)
    db.commit()
    return redirect(safe_back(request, "/notifications"))


# ===========================================================================
# Routes — leaderboards, profile, stats, recap
# ===========================================================================


@app.get("/boards", response_class=HTMLResponse)
def boards(request: Request, db: DB, me: Me, scope: str = "friends", period: str = "week", mode: str = "points",
           party: Optional[int] = None, challenge: Optional[int] = None) -> HTMLResponse:
    board = leaderboard(db, me, scope, period, mode, party, challenge)
    parties = [p for p in (db.get(Party, pid) for pid in user_party_ids(db, me.id)) if p]
    my_challenges = db.exec(select(Challenge).join(ChallengeMember, ChallengeMember.challenge_id == Challenge.id)
                            .where(ChallengeMember.user_id == me.id).order_by(col(Challenge.id).desc())).all()
    today = local_today(me)
    leagues = [{"party": p, "rows": league_table(db, p, today)} for p in parties]
    return render(request, "boards", board=board, parties=parties, challenges=my_challenges, walls=walls(db, me),
                  leagues=leagues, scopes=SCOPES, periods=PERIODS, modes=MODES)


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(username: str, request: Request, db: DB, me: Me) -> HTMLResponse:
    user = get_user_by_name(db, username)
    is_me = user.id == me.id
    friendship = friendship_between(db, me.id, user.id)
    visible = is_me or can_see(db, me, user.id)
    badges = {b.key: b for b in active_badges(db, user)}
    stickers = db.exec(select(Taunt).where(Taunt.target_id == user.id, Taunt.resolved == False)).all() if visible else []  # noqa: E712
    taunt_ok, taunt_reason = taunt_state(db, me, user) if not is_me else (False, "")
    stats = player_stats(db, user) if visible else {}
    habits = active_habits(db, user) if visible else []
    return render(request, "profile", user=user, is_me=is_me, friendship=friendship, visible=visible, badges=badges,
                  stickers=stickers, taunt_ok=taunt_ok, taunt_reason=taunt_reason, stats=stats, habits=habits,
                  heat=heatmap(db, user, 26) if visible else [], prog=game.level_progress(user.xp),
                  parties=[db.get(Party, pid) for pid in user_party_ids(db, user.id)] if visible else [])


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: DB, me: Me, bg: BackgroundTasks) -> HTMLResponse:
    hist = hour_histogram(db, me)
    buckets = [sum(hist[i:i + 3]) for i in range(0, 24, 3)]
    peak = max(buckets) or 1
    habits = db.exec(select(Habit).where(Habit.user_id == me.id).order_by(col(Habit.best_streak).desc())).all()
    best_hour = max(range(24), key=lambda h: hist[h]) if sum(hist) else None
    return render(request, "stats", heat=heatmap(db, me, 52), rates=habit_rates(db, me), buckets=buckets, peak=peak,
                  best_hour=best_hour, moods=mood_trend(db, me), streak_habits=habits[:8], tip=get_coach_tip(db, me, bg),
                  stats=player_stats(db, me))


@app.get("/recap", response_class=HTMLResponse)
def recap(request: Request, db: DB, me: Me, week: str = "last") -> HTMLResponse:
    ws = game.week_start(local_today(me))
    if week == "last":
        ws -= timedelta(days=7)
    data = recap_data(db, me, ws)
    payload = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in data.items() if isinstance(v, (int, str, date))}
    payload.update({"username": me.username,
                    "top_habit": f"{data['top_habit'].icon} {data['top_habit'].name}" if data["top_habit"] else "—",
                    "photo": f"/media/{data['best_photo'].photo_path}" if data["best_photo"] else None,
                    "avatar": avatar_url(me)})
    return render(request, "recap", r=data, week=week, recap_json=payload)


# ===========================================================================
# Routes — friends and parties
# ===========================================================================


@app.get("/friends", response_class=HTMLResponse)
def friends(request: Request, db: DB, me: Me) -> HTMLResponse:
    incoming = db.exec(select(Friendship).where(Friendship.addressee_id == me.id, Friendship.status == "pending")).all()
    outgoing = db.exec(select(Friendship).where(Friendship.requester_id == me.id, Friendship.status == "pending")).all()
    others = users_by_ids(db, [f.requester_id for f in incoming] + [f.addressee_id for f in outgoing])
    flist = sorted(users_by_ids(db, friend_ids(db, me.id)).values(), key=lambda u: u.username.lower())
    taunts = {u.id: taunt_state(db, me, u)[0] for u in flist}
    return render(request, "friends", incoming=incoming, outgoing=outgoing, others=others, friends=flist, taunts=taunts,
                  invite_url=f"{BASE_URL}/invite/{me.invite_code}", needs=["qr", "qrgen"])


@app.get("/friends/search", response_class=HTMLResponse)
def friend_search(request: Request, db: DB, me: Me, q: str = "") -> HTMLResponse:
    q = q.strip()
    results: list[User] = []
    if len(q) >= 2 and re.fullmatch(r"[A-Za-z0-9_]+", q):
        results = list(db.exec(select(User).where(col(User.username).ilike(f"{q}%"), User.id != me.id).limit(10)).all())
    rel = {u.id: friendship_between(db, me.id, u.id) for u in results}
    return HTMLResponse(render_partial(request, "friend_results", results=results, rel=rel, q=q))


@app.post("/friends/request/{username}")
def friend_request(username: str, request: Request, db: DB, me: Me) -> Response:
    rate_limit(f"friendreq:{me.id}", 20, 3600)
    target = get_user_by_name(db, username)
    if target.id == me.id:
        raise GameError("You're already your own best friend.")
    existing = friendship_between(db, me.id, target.id)
    if existing and existing.status == "accepted":
        raise GameError("Already friends.")
    if existing and existing.status == "pending" and existing.addressee_id == me.id:
        existing.status = "accepted"  # they already asked us: accept
        db.add(existing)
        notify(db, target.id, "friend", f"🤝 {me.username} accepted your friend request", f"/u/{me.username}")
        msg = f"🤝 Now friends with {target.username}"
    elif existing:
        existing.requester_id, existing.addressee_id, existing.status = me.id, target.id, "pending"
        db.add(existing)
        notify(db, target.id, "friend", f"👋 {me.username} wants to be friends", "/friends", dedupe=f"freq:{me.id}:{target.id}:{utcnow():%Y%m%d}")
        msg = "Friend request sent 📨"
    else:
        db.add(Friendship(requester_id=me.id, addressee_id=target.id))
        notify(db, target.id, "friend", f"👋 {me.username} wants to be friends", "/friends")
        msg = "Friend request sent 📨"
    fx = Fx().toast(msg, "good").sfx("coin")
    check_badges(db, me, fx)
    db.commit()
    return deliver(request, redirect(safe_back(request, "/friends")), fx)


def _incoming_request(db: Session, me: User, fid: int) -> Friendship:
    f = db.get(Friendship, fid)
    if not f or f.addressee_id != me.id or f.status != "pending":
        raise StarletteHTTPException(404, "Request not found")
    return f


@app.post("/friends/{fid}/accept")
def friend_accept(fid: int, request: Request, db: DB, me: Me) -> Response:
    f = _incoming_request(db, me, fid)
    f.status = "accepted"
    db.add(f)
    requester = db.get(User, f.requester_id)
    if requester:
        notify(db, requester.id, "friend", f"🤝 {me.username} accepted your friend request", f"/u/{me.username}")
    fx = Fx().toast("Friend request accepted.", "gold").sfx("coin")
    db.flush()
    check_badges(db, me, fx)
    if requester:
        check_badges(db, requester)
    db.commit()
    return deliver(request, redirect("/friends"), fx)


@app.post("/friends/{fid}/decline")
def friend_decline(fid: int, request: Request, db: DB, me: Me) -> Response:
    f = _incoming_request(db, me, fid)
    f.status = "declined"
    db.add(f)
    db.commit()
    return deliver(request, redirect("/friends"), Fx().toast("Request declined", "pink"))


@app.post("/friends/remove/{username}")
def friend_remove(username: str, request: Request, db: DB, me: Me) -> Response:
    target = get_user_by_name(db, username)
    f = friendship_between(db, me.id, target.id)
    if f:
        db.delete(f)
        db.commit()
    return deliver(request, redirect("/friends"), Fx().toast("Friend removed", "pink"))


@app.get("/parties", response_class=HTMLResponse)
def parties_page(request: Request, db: DB, me: Me) -> HTMLResponse:
    parties = [p for p in (db.get(Party, pid) for pid in user_party_ids(db, me.id)) if p]
    sizes = {p.id: len(party_member_ids(db, p.id)) for p in parties}
    return render(request, "parties", parties=parties, sizes=sizes)


@app.post("/parties")
def party_create(request: Request, db: DB, me: Me, name: str = Form(...)) -> Response:
    name = name.strip()
    if not 3 <= len(name) <= 30:
        raise GameError("Party name: 3-30 characters.")
    if len(user_party_ids(db, me.id)) >= 5:
        raise GameError("Max 5 parties per player.")
    party = Party(name=name, owner_id=me.id, invite_code=new_code())
    db.add(party)
    db.flush()
    db.add(PartyMember(party_id=party.id, user_id=me.id))
    db.commit()
    return deliver(request, redirect(f"/parties/{party.id}"), Fx().toast(f"🎉 Party {name} formed!", "gold").sfx("levelup"))


def _join_party(db: Session, me: User, code: str) -> Party:
    party = db.exec(select(Party).where(Party.invite_code == code.strip())).first()
    if not party:
        raise GameError("No party with that code.", 404)
    members = party_member_ids(db, party.id)
    if me.id in members:
        return party
    if len(members) >= MAX_PARTY_SIZE:
        raise GameError(f"Party full ({MAX_PARTY_SIZE} players max)")
    db.add(PartyMember(party_id=party.id, user_id=me.id))
    feed_event(db, "party", "🎉", f"{me.username} joined {party.name}!", party_id=party.id)
    for uid in members:
        notify(db, uid, "party", f"🎉 {me.username} joined {party.name}", f"/parties/{party.id}", push=False)
    boss = db.exec(select(BossBattle).where(BossBattle.party_id == party.id, BossBattle.status == "active")).first()
    if boss:  # scale the current boss to the new party size
        boss.hp_max += game.BOSS_HP_PER_MEMBER
        boss.hp += game.BOSS_HP_PER_MEMBER
        db.add(boss)
    db.commit()
    return party


@app.post("/parties/join")
def party_join(request: Request, db: DB, me: Me, code: str = Form(...)) -> Response:
    party = _join_party(db, me, code)
    return deliver(request, redirect(f"/parties/{party.id}"), Fx().toast(f"Joined {party.name}!", "gold"))


@app.get("/party/join/{code}")
def party_join_link(code: str, request: Request, db: DB, me: Me) -> HTMLResponse:
    party = db.exec(select(Party).where(Party.invite_code == code)).first()
    if not party:
        raise StarletteHTTPException(404, "Party not found")
    return render(request, "party_join", party=party, size=len(party_member_ids(db, party.id)),
                  member=me.id in party_member_ids(db, party.id))


def _member_party(db: Session, me: User, pid: int) -> Party:
    party = db.get(Party, pid)
    if not party or me.id not in party_member_ids(db, pid):
        raise StarletteHTTPException(404, "Party not found")
    return party


@app.get("/parties/{pid}", response_class=HTMLResponse)
def party_page(pid: int, request: Request, db: DB, me: Me) -> HTMLResponse:
    party = _member_party(db, me, pid)
    today = local_today(me)
    members = sorted(users_by_ids(db, party_member_ids(db, pid)).values(), key=lambda u: -u.xp)
    week = point_totals(db, [u.id for u in members], game.week_start(today), today)
    claims = {c.user_id: c for c in db.exec(select(DareClaim).where(DareClaim.party_id == pid, DareClaim.day == today)).all()}
    boss = ensure_boss(db, party)
    db.commit()
    return render(request, "party", party=party, members=members, week=week, pet=pet_view(party),
                  dare=game.daily_dare(pid, today), claims=claims, boss=boss_view(db, boss, me),
                  join_url=f"{BASE_URL}/party/join/{party.invite_code}", league=league_table(db, party, today),
                  needs=["camera"])


@app.post("/parties/{pid}/leave")
def party_leave(pid: int, request: Request, db: DB, me: Me) -> Response:
    party = _member_party(db, me, pid)
    pm = db.exec(select(PartyMember).where(PartyMember.party_id == pid, PartyMember.user_id == me.id)).one()
    db.delete(pm)
    db.flush()
    remaining = party_member_ids(db, pid)
    if party.owner_id == me.id and remaining:
        party.owner_id = remaining[0]
        db.add(party)
    db.commit()
    return deliver(request, redirect("/parties"), Fx().toast(f"LEFT {party.name.upper()}", "pink"))


@app.post("/parties/{pid}/rename-pet")
def party_rename_pet(pid: int, request: Request, db: DB, me: Me, name: str = Form(...)) -> Response:
    party = _member_party(db, me, pid)
    name = name.strip()[:16]
    if not name:
        raise GameError("Name your pet.")
    party.pet_name = name
    db.add(party)
    db.commit()
    return deliver(request, redirect(f"/parties/{pid}"), Fx().toast(f"🐣 Pet renamed: {name}"))


@app.post("/parties/{pid}/dare")
def party_dare(pid: int, request: Request, db: DB, me: Me, photo: Optional[UploadFile] = File(None)) -> Response:
    party = _member_party(db, me, pid)
    today = local_today(me)
    if db.exec(select(DareClaim.id).where(DareClaim.party_id == pid, DareClaim.user_id == me.id, DareClaim.day == today)).first():
        raise GameError("Dare already claimed today.", 409)
    if photo is None or not photo.filename:
        raise GameError("Dares need a live photo.")
    path = save_photo(photo)
    dare = game.daily_dare(pid, today)
    db.add(DareClaim(party_id=pid, user_id=me.id, day=today, dare=dare, photo_path=path))
    me.coins += game.DARE_COINS
    db.add(me)
    feed_event(db, "dare", "😈", f"{me.username} completed today's dare: {dare}", party_id=pid, user_id=me.id)
    fx = Fx().toast(f"😈 Dare complete! +{game.DARE_COINS} 🪙", "gold").sfx("coin")
    try:
        db.flush()
        check_badges(db, me, fx)
        db.commit()
    except IntegrityError:
        db.rollback()
        delete_photo(path)
        raise GameError("Dare already claimed today.", 409)
    return deliver(request, redirect(safe_back(request, f"/parties/{party.id}")), fx)


# ===========================================================================
# Routes — challenges (duels, stake pots), boss, map, prove it, settings
# ===========================================================================


@app.get("/challenges", response_class=HTMLResponse)
def challenges_page(request: Request, db: DB, me: Me) -> HTMLResponse:
    today = local_today(me)
    duels = db.exec(select(Duel).where(or_(Duel.challenger_id == me.id, Duel.opponent_id == me.id))
                    .order_by(col(Duel.id).desc()).limit(20)).all()
    people = users_by_ids(db, {d.challenger_id for d in duels} | {d.opponent_id for d in duels})
    duel_rows = [{"d": d, "runs": duel_runs(db, d, today), "a": people.get(d.challenger_id), "b": people.get(d.opponent_id)}
                 for d in duels]
    pids = user_party_ids(db, me.id)
    chs = db.exec(select(Challenge).where(col(Challenge.party_id).in_(pids or [0])).order_by(col(Challenge.id).desc()).limit(20)).all()
    ch_rows = []
    for ch in chs:
        members = db.exec(select(ChallengeMember).where(ChallengeMember.challenge_id == ch.id)).all()
        mu = users_by_ids(db, [m.user_id for m in members])
        ch_rows.append({"c": ch, "party": db.get(Party, ch.party_id), "joined": any(m.user_id == me.id for m in members),
                        "members": [{"m": m, "u": mu.get(m.user_id), "progress": challenge_progress(db, ch, m.user_id)} for m in members]})
    parties = [p for p in (db.get(Party, pid) for pid in pids) if p]
    friends_ = sorted(users_by_ids(db, friend_ids(db, me.id)).values(), key=lambda u: u.username.lower())
    return render(request, "challenges", duels=duel_rows, challenges=ch_rows, parties=parties, friends=friends_, today=today)


@app.post("/duels")
def duel_create(request: Request, db: DB, me: Me, opponent: str = Form(...), days: int = Form(7)) -> Response:
    data = validated(DuelIn, opponent=opponent.strip(), days=days)
    target = get_user_by_name(db, data.opponent)
    if target.id == me.id or not are_friends(db, me.id, target.id):
        raise GameError("Duel a friend, not yourself.")
    open_duel = db.exec(select(Duel).where(col(Duel.status).in_(("pending", "active")), or_(
        (Duel.challenger_id == me.id) & (Duel.opponent_id == target.id),
        (Duel.challenger_id == target.id) & (Duel.opponent_id == me.id)))).first()
    if open_duel:
        raise GameError("You already have a duel with them.", 409)
    duel = Duel(challenger_id=me.id, opponent_id=target.id, days=data.days)
    db.add(duel)
    db.flush()
    notify(db, target.id, "duel", f"⚔️ {me.username} challenges you to a {data.days}-day STREAK DUEL!", "/challenges")
    db.commit()
    return deliver(request, redirect("/challenges"), Fx().toast(f"⚔️ Duel sent to {target.username}", "gold"))


def _my_duel(db: Session, me: User, did: int) -> Duel:
    duel = db.get(Duel, did)
    if not duel or me.id not in (duel.challenger_id, duel.opponent_id):
        raise StarletteHTTPException(404, "Duel not found")
    return duel


@app.post("/duels/{did}/respond")
def duel_respond(did: int, request: Request, db: DB, me: Me, accept: str = Form(...)) -> Response:
    duel = _my_duel(db, me, did)
    if duel.opponent_id != me.id or duel.status != "pending":
        raise GameError("Nothing to respond to.", 409)
    if accept == "1":
        duel.status = "active"
        duel.start_date = local_today(me)
        duel.end_date = duel.start_date + timedelta(days=duel.days - 1)
        notify(db, duel.challenger_id, "duel", f"⚔️ {me.username} accepted your duel. FIGHT!", "/challenges")
        fx = Fx().toast("⚔️ Duel on! Longest streak wins", "gold").sfx("levelup")
    else:
        duel.status = "declined"
        notify(db, duel.challenger_id, "duel", f"🐔 {me.username} declined your duel", "/challenges")
        fx = Fx().toast("Duel declined 🐔", "pink")
    db.add(duel)
    db.commit()
    return deliver(request, redirect("/challenges"), fx)


@app.post("/duels/{did}/dare")
def duel_pick_dare(did: int, request: Request, db: DB, me: Me, dare: str = Form(...)) -> Response:
    duel = _my_duel(db, me, did)
    if duel.status != "finished" or duel.winner_id != me.id or duel.dare_key:
        raise GameError("Only the winner picks a dare (once)", 403)
    if dare not in game.DUEL_DARES:
        raise GameError("Preset dares only.")
    duel.dare_key = dare
    loser = duel.opponent_id if duel.challenger_id == me.id else duel.challenger_id
    notify(db, loser, "duel", f"😈 YOUR DARE from {me.username}: {game.DUEL_DARES[dare]}", "/challenges")
    db.add(duel)
    db.commit()
    return deliver(request, redirect("/challenges"), Fx().toast("😈 Dare assigned", "gold"))


@app.post("/duels/{did}/dare-done")
def duel_dare_done(did: int, request: Request, db: DB, me: Me) -> Response:
    duel = _my_duel(db, me, did)
    if duel.status != "finished" or not duel.dare_key or duel.winner_id == me.id or duel.dare_done:
        raise GameError("No dare to complete.", 409)
    duel.dare_done = True
    if duel.winner_id:
        notify(db, duel.winner_id, "duel", f"✅ {me.username} completed the dare: {game.DUEL_DARES[duel.dare_key]}", "/challenges")
    db.add(duel)
    db.commit()
    return deliver(request, redirect("/challenges"), Fx().toast("Dare marked as done.", "good"))


@app.post("/challenges")
def challenge_create(request: Request, db: DB, me: Me, party_id: int = Form(...), name: str = Form(...),
                     days: int = Form(7), stake: int = Form(50), required: int = Form(5)) -> Response:
    data = validated(ChallengeIn, party_id=party_id, name=name.strip(), days=days, stake=stake, required=required)
    _member_party(db, me, data.party_id)
    if data.required > data.days * 5:
        raise GameError("Required check-ins too high.")
    if me.coins < data.stake:
        raise GameError("Not enough coins.")
    start = local_today(me) + timedelta(days=1)
    ch = Challenge(party_id=data.party_id, creator_id=me.id, name=data.name, start_date=start,
                   end_date=start + timedelta(days=data.days - 1), stake=data.stake, required=data.required, pot=data.stake)
    db.add(ch)
    db.flush()
    db.add(ChallengeMember(challenge_id=ch.id, user_id=me.id))
    me.coins -= data.stake
    db.add(me)
    for uid in party_member_ids(db, data.party_id):
        if uid != me.id:
            notify(db, uid, "challenge", f"🎰 {me.username} opened a stake pot: {data.name} ({data.stake} 🪙 to join)", "/challenges")
    db.commit()
    return deliver(request, redirect("/challenges"), Fx().toast(f"🎰 Pot opened: {data.name}", "gold").sfx("coin"))


@app.post("/challenges/{cid}/join")
def challenge_join(cid: int, request: Request, db: DB, me: Me) -> Response:
    ch = db.get(Challenge, cid)
    if not ch:
        raise StarletteHTTPException(404, "Challenge not found")
    _member_party(db, me, ch.party_id)
    if ch.status != "open" or local_today(me) >= ch.start_date:
        raise GameError("This pot is closed.", 409)
    if db.exec(select(ChallengeMember.id).where(ChallengeMember.challenge_id == cid, ChallengeMember.user_id == me.id)).first():
        raise GameError("Already in.", 409)
    if me.coins < ch.stake:
        raise GameError("Not enough coins.")
    me.coins -= ch.stake
    ch.pot += ch.stake
    db.add(ChallengeMember(challenge_id=cid, user_id=me.id))
    db.add(me)
    db.add(ch)
    db.commit()
    return deliver(request, redirect("/challenges"), Fx().toast(f"Staked {ch.stake} 🪙 — pot now {ch.pot}", "gold").sfx("coin"))


@app.get("/boss", response_class=HTMLResponse)
def boss_page(request: Request, db: DB, me: Me, party: Optional[int] = None) -> HTMLResponse:
    pids = user_party_ids(db, me.id)
    parties = [p for p in (db.get(Party, pid) for pid in pids) if p]
    current = next((p for p in parties if p.id == party), parties[0] if parties else None)
    view = boss_view(db, ensure_boss(db, current), me) if current else None
    db.commit()
    history = db.exec(select(BossBattle).where(BossBattle.party_id == current.id, BossBattle.status != "active")
                      .order_by(col(BossBattle.week_start).desc()).limit(6)).all() if current else []
    return render(request, "boss", parties=parties, party=current, v=view, history=history)


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request, db: DB, me: Me) -> HTMLResponse:
    owned = len(db.exec(select(Tile.id).where(Tile.owner_id == me.id)).all())
    return render(request, "map", needs=["leaflet"], owned=owned)


@app.get("/map/tiles.json")
def map_tiles(db: DB, me: Me) -> JSONResponse:
    ids = friend_ids(db, me.id) | {me.id}
    users = users_by_ids(db, ids)
    tiles = db.exec(select(Tile).where(col(Tile.owner_id).in_(list(ids))).limit(3000)).all()
    my_counts = {(c.tx, c.ty): c.count for c in db.exec(select(TileCount).where(TileCount.user_id == me.id)).all()}
    out = []
    for t in tiles:
        (s, w), (n, e) = game.tile_bounds(t.tx, t.ty)
        owner = users.get(t.owner_id) if t.owner_id else None
        owner_count = db.exec(select(TileCount.count).where(TileCount.tx == t.tx, TileCount.ty == t.ty,
                                                            TileCount.user_id == t.owner_id)).first() or 0
        out.append({"bounds": [[s, w], [n, e]], "owner": owner.username if owner else None,
                    "color": game.matte_color(owner.avatar_color) if owner else "#8c8579", "mine": t.owner_id == me.id,
                    "owner_count": owner_count, "my_count": my_counts.get((t.tx, t.ty), 0)})
    home_ = next(((o["bounds"][0][0], o["bounds"][0][1]) for o in out if o["mine"]), None)
    return JSONResponse({"tiles": out, "home": home_})


@app.get("/quick", response_class=HTMLResponse)
def quick_sheet(request: Request, db: DB, me: Me) -> HTMLResponse:
    """Bottom sheet behind the centre check-in button: today's quests at a glance."""
    now = local_now(me)
    quests = [quest_view(db, me, h, now.date(), now.hour) for h in active_habits(db, me)]
    quests.sort(key=lambda q: (q["done"], not q["due"]))
    return HTMLResponse(render_partial(request, "quick_sheet", quests=quests))


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request, me: Me) -> HTMLResponse:
    """One scanner for everything: quest QR/NFC checkpoints and friend codes."""
    return render(request, "scan", needs=["qr"])


@app.get("/prove", response_class=HTMLResponse)
def prove_page(request: Request, db: DB, me: Me) -> HTMLResponse:
    active = bool(me.prove_it_until and me.prove_it_until > utcnow())
    today = local_today(me)
    habits = [h for h in active_habits(db, me) if "photo" in h.methods and not today_checkin(db, h, today)]
    return render(request, "prove", active=active, habits=habits, needs=["camera"],
                  seconds=int((me.prove_it_until - utcnow()).total_seconds()) if active else 0)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DB, me: Me) -> HTMLResponse:
    _roll_chill_month(me)
    return render(request, "settings", level=game.level_for_xp(me.xp), timezones=_TIMEZONES,
                  invite_url=f"{BASE_URL}/invite/{me.invite_code}", push_enabled=PUSH_ENABLED)


@app.post("/settings")
def settings_save(request: Request, db: DB, me: Me, bio: str = Form(""), avatar_color: str = Form(game.DEFAULT_AVATAR_COLOR),
                  avatar_hat: str = Form("none"), tz: str = Form("UTC"), global_opt_in: str = Form("")) -> Response:
    data = validated(SettingsIn, bio=bio.strip(), avatar_color=avatar_color, avatar_hat=avatar_hat, tz=tz,
                     global_opt_in=global_opt_in == "1")
    if data.avatar_color not in game.AVATAR_COLORS:
        raise GameError("Pick a colour from the palette.")
    if not game.hat_unlocked(data.avatar_hat, game.level_for_xp(me.xp)):
        raise GameError("That avatar frame is still locked.")
    if data.tz not in available_timezones():
        raise GameError("Unknown timezone.")
    if data.tz != me.tz:
        if me.tz_changed_at and utcnow() - me.tz_changed_at < TZ_CHANGE_COOLDOWN:
            days = (me.tz_changed_at + TZ_CHANGE_COOLDOWN - utcnow()).days + 1
            raise GameError(f"You can change your timezone again in {days} day{'s' if days != 1 else ''}.", 429)
        me.tz_changed_at = utcnow()
    me.bio, me.avatar_color, me.avatar_hat, me.tz, me.global_opt_in = data.bio, data.avatar_color, data.avatar_hat, data.tz, data.global_opt_in
    db.add(me)
    db.commit()
    return deliver(request, redirect("/settings"), Fx().toast("Settings saved 💾", "good"))


def _roll_chill_month(user: User) -> None:
    month = game.season_key(local_today(user))
    if user.chill_month != month:
        user.chill_month, user.chill_days_used = month, 0


@app.post("/settings/theme")
def settings_theme(request: Request, theme: str = Form(...)) -> Response:
    """Theme lives in a cookie so every page renders in it immediately (no flash), signed in or not."""
    if theme not in THEMES:
        raise GameError("Unknown theme.")
    resp = deliver(request, redirect(safe_back(request, "/settings")), Fx().toast(f"{THEMES[theme][0]} theme applied.", "good"))
    resp.set_cookie(THEME_COOKIE, theme, max_age=60 * 60 * 24 * 365, samesite="lax", secure=not DEBUG, httponly=False, path="/")
    return resp


@app.post("/settings/chill")
def settings_chill(request: Request, db: DB, me: Me) -> Response:
    _roll_chill_month(me)
    if not me.chill_on and me.chill_days_used >= CHILL_DAYS_PER_MONTH:
        raise GameError(f"Chill Mode used up ({CHILL_DAYS_PER_MONTH} days/month)")
    me.chill_on = not me.chill_on
    db.add(me)
    db.commit()
    msg = "Chill Mode is on — taunts and penalties are paused." if me.chill_on else "Chill Mode is off."
    return deliver(request, redirect(safe_back(request, "/settings")), Fx().toast(msg, "good"))


@app.post("/push/subscribe")
async def push_subscribe(request: Request, db: DB, me: Me) -> JSONResponse:
    if not PUSH_ENABLED:
        return JSONResponse({"ok": False, "reason": "push disabled"}, status_code=400)
    try:
        body = await request.json()
        sub = {"endpoint": str(body["endpoint"]), "keys": {"p256dh": str(body["keys"]["p256dh"]), "auth": str(body["keys"]["auth"])}}
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False}, status_code=400)
    if not allowed_push_endpoint(sub["endpoint"]):
        return JSONResponse({"ok": False, "reason": "unsupported push service"}, status_code=400)
    me.push_subscription = json.dumps(sub)
    db.add(me)
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
def push_unsubscribe(db: DB, me: Me) -> JSONResponse:
    me.push_subscription = None
    db.add(me)
    db.commit()
    return JSONResponse({"ok": True})


# ===========================================================================
# Scheduler — every job is timezone-aware and idempotent (safe to re-run)
# ===========================================================================

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
LOCK_TTL = timedelta(seconds=150)


def acquire_lock(name: str = "scheduler") -> bool:
    """Leader election through the DB so N uvicorn workers don't all run the jobs."""
    with Session(engine) as db:
        now = utcnow()
        lock = db.get(JobLock, name)
        if lock and lock.holder != WORKER_ID and lock.expires_at > now:
            return False
        if lock:
            lock.holder, lock.expires_at = WORKER_ID, now + LOCK_TTL
        else:
            lock = JobLock(name=name, holder=WORKER_ID, expires_at=now + LOCK_TTL)
        db.add(lock)
        try:
            db.commit()
            return True
        except IntegrityError:
            return False


def _each_user(fn, label: str) -> None:
    with Session(engine) as db:
        ids = db.exec(select(User.id)).all()
    for uid in ids:
        with Session(engine) as db:
            user = db.get(User, uid)
            if not user:
                continue
            try:
                fn(db, user)
                db.commit()
            except Exception:
                db.rollback()
                log.exception(f"job {label} failed", extra={"fields": {"user": uid}})


def process_user_day(db: Session, user: User, day: date) -> None:
    """Resolve one finished local day: chill → shield → streak break (in that order)."""
    if user.chill_on and not db.exec(select(ChillDay.id).where(ChillDay.user_id == user.id, ChillDay.day == day)).first():
        _roll_chill_month(user)
        db.add(ChillDay(user_id=user.id, day=day))
        user.chill_days_used += 1
        if user.chill_days_used >= CHILL_DAYS_PER_MONTH:
            user.chill_on = False
            notify(db, user.id, "chill", "🧊 Chill Mode used up for this month — back in the game!", "/settings")
    for habit in active_habits(db, user):
        if habit_start_day(habit, user) > day:
            continue
        if habit.frequency == "weekly":
            if day.weekday() != 6:
                continue
            ws = game.week_start(day)
            done = len(active_days_for_habit(db, habit, max(ws, habit_start_day(habit, user)), day))
            if done >= habit.times_per_week or habit_start_day(habit, user) > ws:
                continue
        elif not game.is_scheduled_day(habit.schedule, day) or active_days_for_habit(db, habit, day, day):
            continue
        if db.exec(select(DayMark.id).where(DayMark.habit_id == habit.id, DayMark.day == day)).first():
            continue
        if user.chill_on:
            db.add(DayMark(habit_id=habit.id, user_id=user.id, day=day, kind="chill"))
        elif user.shields > 0:
            user.shields -= 1
            db.add(DayMark(habit_id=habit.id, user_id=user.id, day=day, kind="shield"))
            notify(db, user.id, "shield", f"🛡️ SHIELD USED! Your {habit.name} streak survived {day:%a}.", "/")
        else:
            lost = game.compute_streak(habit.schedule, habit_done_days(db, habit), habit_protected_days(db, habit),
                                       day - timedelta(days=1), habit_start_day(habit, user))
            db.add(DayMark(habit_id=habit.id, user_id=user.id, day=day, kind="miss", streak_lost=lost))
            if lost > 0:
                notify(db, user.id, "streak", f"💀 GAME OVER… your {lost}-day {habit.name} streak fell.", "/")
                if lost >= 3:
                    feed_event(db, "shame", "💔", f"{user.username} dropped a {lost}-day {habit.name} streak", user_id=user.id)
        db.flush()
        refresh_streak(db, habit, user, day + timedelta(days=1))
    db.add(user)


def rollover_user(db: Session, user: User) -> None:
    today = local_today(user)
    yesterday = today - timedelta(days=1)
    created = local_date_of(user.created_at, user)
    first = max((user.last_rollover or yesterday - timedelta(days=1)) + timedelta(days=1),
                yesterday - timedelta(days=6), created)
    for day in game.daterange(first, yesterday):
        process_user_day(db, user, day)
    if user.last_rollover is None or user.last_rollover < yesterday:
        user.last_rollover = yesterday
        db.add(user)
    if local_now(user).hour >= 7:
        for pid in user_party_ids(db, user.id):
            party = db.get(Party, pid)
            if party:
                notify(db, user.id, "dare", f"😈 Today's {party.name} dare: {game.daily_dare(pid, today)} (+{game.DARE_COINS} 🪙)",
                       f"/parties/{pid}", dedupe=f"dare:{pid}:{today}:{user.id}", push=False)


def job_parties() -> None:
    with Session(engine) as db:
        pids = db.exec(select(Party.id)).all()
    for pid in pids:
        with Session(engine) as db:
            try:
                party = db.get(Party, pid)
                if not party:
                    continue
                today = party_today(db, party)
                yesterday = today - timedelta(days=1)
                members = party_member_ids(db, pid)
                if party.pet_last_day is None or party.pet_last_day < yesterday:
                    active = len({uid for uid in members if active_days(db, uid, yesterday, yesterday)})
                    before = game.pet_mood(party.pet_health)
                    party.pet_health = game.pet_health_update(party.pet_health, active, len(members))
                    party.pet_last_day = yesterday
                    after = game.pet_mood(party.pet_health)
                    if after != before and after in ("sad", "sick"):
                        for uid in members:
                            notify(db, uid, "pet", f"{'🤒' if after == 'sick' else '😢'} {party.pet_name} is {after}! Check in to cheer them up.",
                                   f"/parties/{pid}", dedupe=f"pet:{pid}:{yesterday}:{uid}")
                    db.add(party)
                for boss in db.exec(select(BossBattle).where(BossBattle.party_id == pid, BossBattle.status == "active",
                                                             BossBattle.week_start < game.week_start(today))).all():
                    boss.status = "failed"
                    b = game.BOSS_MAP[boss.boss_key]
                    db.add(boss)
                    feed_event(db, "boss", b.sprite, f"{b.laugh} ({party.name} left it at {boss.hp} HP)", party_id=pid)
                    for uid in members:
                        notify(db, uid, "boss", f"{b.sprite} {b.name} survived the week… and LAUGHS at {party.name}.",
                               f"/boss?party={pid}", dedupe=f"bossfail:{boss.id}:{uid}")
                if members:
                    ensure_boss(db, party)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("job parties failed", extra={"fields": {"party": pid}})


def job_votes() -> None:
    with Session(engine) as db:
        now = utcnow()
        for ci in db.exec(select(CheckIn).where(CheckIn.status == "pending",
                                                CheckIn.created_at <= now - timedelta(hours=game.VOTE_WINDOW_HOURS))).all():
            verify_checkin(db, ci, "timeout")
        for ci in db.exec(select(CheckIn).where(CheckIn.status == "flagged", col(CheckIn.reprove_deadline).is_not(None),
                                                CheckIn.reprove_deadline <= now)).all():
            reject_checkin(db, ci)
        for b in db.exec(select(UserBadge).where(col(UserBadge.expires_at).is_not(None), UserBadge.expires_at <= now)).all():
            db.delete(b)
        for ts in db.exec(select(TimerSession).where(TimerSession.started_at <= now - timedelta(days=2))).all():
            db.delete(ts)
        db.commit()


def job_duels_challenges() -> None:
    with Session(engine) as db:
        for duel in db.exec(select(Duel).where(Duel.status == "active")).all():
            challenger = db.get(User, duel.challenger_id)
            if challenger and duel.end_date and duel.end_date < local_today(challenger):
                finish_duel(db, duel)
        for ch in db.exec(select(Challenge).where(col(Challenge.status).in_(("open", "active")))).all():
            creator = db.get(User, ch.creator_id)
            today = local_today(creator) if creator else utcnow().date()
            if ch.status == "open" and today >= ch.start_date:
                ch.status = "active"
                db.add(ch)
            if ch.status == "active" and today > ch.end_date:
                settle_challenge(db, ch)
        db.commit()


def job_seasons() -> None:
    """Monthly league rollover: promote the top 20% of each tier, relegate the bottom 20%."""
    today = utcnow().date()
    key, start, end = game.previous_season(today)
    with Session(engine) as db:
        if db.exec(select(SeasonResult.id).where(SeasonResult.season_key == key)).first():
            return
        parties = db.exec(select(Party)).all()
        by_tier: dict[str, list[tuple[float, Party]]] = defaultdict(list)
        for p in parties:
            members = party_member_ids(db, p.id)
            if not members or p.created_at.date() > end:
                continue
            score = sum(point_totals(db, members, start, end).values()) / len(members)
            by_tier[p.tier].append((score, p))
        for tier, rows in by_tier.items():
            rows.sort(key=lambda r: (-r[0], r[1].id))
            promote, relegate = game.season_moves([p.id for _, p in rows])
            for score, p in rows:
                move = 1 if p.id in promote else -1 if p.id in relegate else 0
                new = game.shift_tier(tier, move)
                db.add(SeasonResult(season_key=key, party_id=p.id, score=score, old_tier=tier, new_tier=new))
                if new != tier:
                    p.tier = new
                    db.add(p)
                    icon = "⬆️" if move > 0 else "⬇️"
                    feed_event(db, "league", icon, f"{p.name} {'PROMOTED' if move > 0 else 'relegated'} to {new} {game.TIER_ICON[new]}",
                               party_id=p.id)
                    for uid in party_member_ids(db, p.id):
                        notify(db, uid, "league", f"{icon} SEASON {key}: {p.name} is now {new} {game.TIER_ICON[new]}", "/boards")
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def job_prove_it(db: Session, user: User) -> None:
    now = local_now(user)
    today = now.date()
    if user.prove_it_date == today:
        return
    at = datetime.combine(today, game.prove_it_time(user.id, today), tzinfo=now.tzinfo)
    if not (at <= now <= at + timedelta(minutes=10)):
        return
    if not any("photo" in h.methods for h in active_habits(db, user)):
        return
    user.prove_it_date = today
    user.prove_it_until = utcnow() + timedelta(seconds=game.PROVE_IT_WINDOW_SECONDS)
    db.add(user)
    notify(db, user.id, "proveit", "⚡ PROVE IT NOW! 2 minutes to post a live photo for 2x points!", "/prove",
           dedupe=f"prove:{user.id}:{today}", announced=True)


def job_reminders(db: Session, user: User) -> None:
    now = local_now(user)
    today = now.date()
    for h in active_habits(db, user):
        if not h.reminder_time:
            continue
        hh, mm = map(int, h.reminder_time.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (at <= now < at + timedelta(minutes=10)):
            continue
        if not is_due_today(db, h, today) or today_checkin(db, h, today):
            continue
        tiny = f" Short on time? {h.tiny_version}" if h.tiny_version and now.hour >= 18 else ""
        notify(db, user.id, "reminder", f"⏰ Quest reminder: {h.icon} {h.name}.{tiny}", "/", dedupe=f"rem:{h.id}:{today}")


def run_minutely() -> None:
    if not acquire_lock():
        return
    _each_user(lambda db, u: (job_prove_it(db, u), job_reminders(db, u)), "minutely")


def run_periodic() -> None:
    if not acquire_lock():
        return
    for name, fn in (("rollover", lambda: _each_user(rollover_user, "rollover")), ("parties", job_parties),
                     ("votes", job_votes), ("duels", job_duels_challenges), ("seasons", job_seasons)):
        try:
            fn()
        except Exception:
            log.exception(f"job {name} failed")


def start_scheduler() -> None:
    global scheduler
    if DISABLE_SCHEDULER or scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=timezone.utc)
    now = datetime.now(timezone.utc)
    scheduler.add_job(run_minutely, "interval", minutes=1, id="minutely", max_instances=1, coalesce=True,
                      next_run_time=now + timedelta(seconds=10))
    scheduler.add_job(run_periodic, "interval", minutes=5, id="periodic", max_instances=1, coalesce=True,
                      next_run_time=now + timedelta(seconds=20))
    scheduler.start()


# ===========================================================================
# Demo seed
# ===========================================================================

DEMO_PASSWORD = "arcade123"
DEMO_SPOT = (37.76904, -122.48386)  # a park "gym" in San Francisco


def _seed_photo(label: str, color: str, rng: random.Random) -> str:
    """A tasteful placeholder 'photo' for demo check-ins."""
    img = Image.new("RGB", (640, 480), "#f3ece0")
    d = ImageDraw.Draw(img)
    for y in range(0, 480, 4):  # soft vertical gradient
        shade = int(246 - 28 * y / 480)
        d.rectangle([0, y, 640, y + 4], fill=(shade, shade - 6, shade - 18))
    for _ in range(9):
        x, y, s = rng.randrange(0, 580), rng.randrange(0, 260), rng.choice((24, 40, 56))
        d.ellipse([x, y, x + s, y + s], fill=rng.choice(["#e8d9bf", "#e6cfc4", "#d9dcc6", "#f0deb0"]))
    d.rectangle([160, 300, 480, 410], fill=color)
    d.rectangle([168, 308, 472, 402], fill="#ffffff")
    d.text((188, 345), label[:32], fill="#3b2a20")
    rel = f"seed/{uuid.uuid4().hex}.webp"
    (UPLOAD_DIR / "seed").mkdir(parents=True, exist_ok=True)
    img.save(UPLOAD_DIR / rel, "WEBP", quality=82)
    return rel


def seed() -> None:
    """Create a lively demo world. Safe to re-run: it does nothing if the demo players exist."""
    rng = random.Random(42)
    with Session(engine) as db:
        if db.exec(select(User).where(User.username == "alice")).first():
            print("Demo data already present — skipping seed.")
            return
        now = utcnow()
        today = now.date()
        pw = hash_password(DEMO_PASSWORD)
        spec = [("alice", "#b5452f", "crown", "Gym rat by day, guitar hero by night 🎸"),
                ("bob", "#2f7a72", "cap", "Runner. Mostly from responsibilities."),
                ("cara", "#7a4a6b", "headband", "Books > people (except you guys)"),
                ("dan", "#c98f1c", "none", "Currently loading motivation…"),
                ("eve", "#3f5f8a", "beanie", "Zen speedrunner 🧘")]
        users: dict[str, User] = {}
        for name, color, hat, bio in spec:
            u = User(username=name, email=f"{name}@habitor.demo", password_hash=pw, invite_code=new_code(),
                     avatar_color=color, avatar_hat=hat, bio=bio, global_opt_in=name != "dan",
                     created_at=now - timedelta(days=30), last_rollover=today - timedelta(days=1))
            db.add(u)
            users[name] = u
        db.flush()
        a, b, c, d, e = (users[n] for n in ("alice", "bob", "cara", "dan", "eve"))
        for x, y in ((a, b), (a, c), (a, d), (b, c), (b, d), (c, d), (b, e)):
            db.add(Friendship(requester_id=x.id, addressee_id=y.id, status="accepted"))
        db.add(Friendship(requester_id=e.id, addressee_id=a.id, status="pending"))
        crew = Party(name="Pixel Pushers", owner_id=a.id, invite_code=new_code(), tier="Silver", pet_name="Bitsy",
                     pet_health=74, pet_last_day=today - timedelta(days=1), created_at=now - timedelta(days=40))
        owls = Party(name="Night Owls", owner_id=b.id, invite_code=new_code(), tier="Silver", pet_name="Hoot",
                     pet_health=35, pet_last_day=today - timedelta(days=1), created_at=now - timedelta(days=40))
        rivals = Party(name="Snooze Squad", owner_id=e.id, invite_code=new_code(), tier="Silver",
                       created_at=now - timedelta(days=40))
        db.add_all([crew, owls, rivals])
        db.flush()
        for p, members in ((crew, (a, b, c, d)), (owls, (b, e)), (rivals, (e,))):
            for m in members:
                db.add(PartyMember(party_id=p.id, user_id=m.id))

        def habit(u: User, name: str, cat: str, icon: str, methods: str, **kw: Any) -> Habit:
            h = Habit(user_id=u.id, name=name, category=cat, icon=icon, proof_methods=methods,
                      created_at=now - timedelta(days=28), **kw)
            db.add(h)
            return h

        hs = {
            "a_gym": habit(a, "Gym session", "Gym", "🏋️", "qr,nfc,gps,photo", frequency="weekly", times_per_week=3,
                           difficulty="hard", gps_lat=DEMO_SPOT[0], gps_lon=DEMO_SPOT[1], gps_label="Park Gym",
                           tiny_version="10 squats at home"),
            "a_guitar": habit(a, "Guitar practice", "Music", "🎸", "timer,photo", difficulty="normal", min_minutes=1,
                              tiny_version="One chord change x10", reminder_time="19:00"),
            "a_read": habit(a, "Read 20 pages", "Reading", "📚", "photo", difficulty="easy", tiny_version="Read one page"),
            "b_run": habit(b, "Morning run", "Gym", "🏃", "gps,photo", difficulty="hard",
                           gps_lat=DEMO_SPOT[0] + 0.004, gps_lon=DEMO_SPOT[1] + 0.006, gps_label="Lake loop"),
            "b_study": habit(b, "Study Spanish", "Study", "📖", "timer,photo", difficulty="normal", min_minutes=15),
            "c_read": habit(c, "Read before bed", "Reading", "📚", "photo", difficulty="easy"),
            "c_sleep": habit(c, "Lights out by 11", "Sleep", "😴", "photo", difficulty="normal"),
            "d_gym": habit(d, "Lift", "Gym", "🏋️", "gps,photo", frequency="days", days_csv="0,2,4", difficulty="hard",
                           gps_lat=DEMO_SPOT[0] - 0.005, gps_lon=DEMO_SPOT[1] - 0.004, gps_label="Iron Temple"),
            "e_med": habit(e, "Meditate", "Meditation", "🧘", "timer", difficulty="easy", min_minutes=10),
        }
        db.flush()
        hs["a_read"].stack_after_id = hs["a_guitar"].id  # habit stacking demo: guitar → read

        # Which days each habit was completed (days ago). 0 = today.
        plan: dict[str, set[int]] = {
            "a_gym": {1, 3, 6, 8, 10, 13, 15, 17, 20, 22, 24},
            "a_guitar": set(range(1, 19)) - {9},
            "a_read": set(range(2, 15)),  # missed yesterday → tauntable
            "b_run": set(range(0, 21)) - {12},
            "b_study": set(range(0, 10)),
            "c_read": set(range(0, 25)) - {5, 11},
            "c_sleep": set(range(1, 9)),
            "d_gym": {d_ for d_ in range(2, 28) if (today - timedelta(days=d_)).weekday() in (0, 2, 4)} - {2, 3},
            "e_med": set(range(0, 6)),
        }
        hour_for = {"a": 18, "b": 6, "c": 22, "d": 17, "e": 7}
        checkins: list[CheckIn] = []
        for key, days_ago in plan.items():
            h = hs[key]
            owner = db.get(User, h.user_id)
            assert owner is not None
            for ago in sorted(days_ago, reverse=True):
                day = today - timedelta(days=ago)
                if h.frequency == "days" and day.weekday() not in h.schedule.days:
                    continue
                created = datetime.combine(day, time(hour_for[key[0]], rng.randrange(0, 59)))
                if ago == 0:
                    created = min(created, now - timedelta(minutes=rng.randrange(20, 90)))
                    created = max(created, datetime.combine(today, time(0, 5)))
                methods = h.methods
                use_gps = "gps" in methods and rng.random() < 0.7
                photo = None
                if ("photo" in methods and not use_gps) or ago <= 1:
                    photo = _seed_photo(h.name, owner.avatar_color, rng) if "photo" in methods else None
                kinds = (["gps"] if use_gps else []) + (["photo"] if photo else []) + (["timer"] if "timer" in methods and not photo and not use_gps else [])
                kinds = kinds or [methods[0]]
                verified_by = "gps" if use_gps else ("timer" if kinds == ["timer"] else ("qr" if kinds == ["qr"] else "votes"))
                status = "verified"
                if photo and not use_gps and ago == 0:
                    status, verified_by = "pending", ""
                lat = lon = None
                if use_gps:
                    lat = (h.gps_lat or 0) + rng.uniform(-0.0005, 0.0005)
                    lon = (h.gps_lon or 0) + rng.uniform(-0.0005, 0.0005)
                ci = CheckIn(habit_id=h.id, user_id=owner.id, slot=day.isoformat(), local_date=day, created_at=created,
                             status=status, methods=",".join(kinds), verified_by=verified_by, photo_path=photo,
                             lat=lat, lon=lon, distance_m=game.haversine_m(lat, lon, h.gps_lat, h.gps_lon) if use_gps else None,
                             timer_minutes=h.min_minutes if "timer" in kinds else None,
                             mood=rng.choice([3, 4, 4, 5, 2]) if rng.random() < 0.6 else None,
                             energy=rng.choice([1, 2, 3]) if rng.random() < 0.5 else None)
                db.add(ci)
                db.flush()
                streak = game.compute_streak(h.schedule, habit_done_days(db, h), set(), day, local_date_of(h.created_at, owner))
                pts = game.checkin_points(streak, verified_by in ("gps", "qr", "votes"), h.difficulty)
                ci.streak, ci.points = streak, pts
                award(db, owner, pts, "checkin", f"checkin:{ci.id}", day)
                owner.coins += game.coins_for_points(pts)
                if use_gps and lat is not None and lon is not None:
                    capture_tile(db, owner, lat, lon)
                checkins.append(ci)
        db.flush()
        # Missed days → day marks (alice skipped Read yesterday; dan broke a streak this week).
        db.add(DayMark(habit_id=hs["a_read"].id, user_id=a.id, day=today - timedelta(days=1), kind="miss", streak_lost=13))
        ws = game.week_start(today)
        dan_miss = today - timedelta(days=1)  # recent miss: dan is tauntable in the demo
        db.add(DayMark(habit_id=hs["d_gym"].id, user_id=d.id, day=dan_miss, kind="miss", streak_lost=9))
        db.add(DayMark(habit_id=hs["a_guitar"].id, user_id=a.id, day=today - timedelta(days=9), kind="shield"))
        for key, h in hs.items():
            owner = db.get(User, h.user_id)
            refresh_streak(db, h, owner, today)  # type: ignore[arg-type]
            h.best_streak = max(h.best_streak, h.current_streak, {"a_guitar": 17, "c_read": 19, "d_gym": 9}.get(key, 0))
        a.shields, b.shields = 1, 2

        # Social layer: reactions & votes on the recent photo check-ins.
        recent = [ci for ci in checkins if ci.local_date >= today - timedelta(days=2)]
        for ci in recent:
            for u in (a, b, c, d):
                if u.id != ci.user_id and rng.random() < 0.5:
                    db.add(Reaction(checkin_id=ci.id, user_id=u.id, emoji=rng.choice(game.REACTIONS)))
        pending = [ci for ci in recent if ci.status == "pending"]
        for ci in pending:
            if ci.user_id == c.id:
                db.add(Vote(checkin_id=ci.id, user_id=b.id, value="legit"))
        # One of bob's proofs got flagged → re-prove countdown + Wall of Shame material.
        bob_flag = next((ci for ci in checkins if ci.user_id == b.id and ci.habit_id == hs["b_study"].id and ci.local_date == today - timedelta(days=1)), None)
        if bob_flag:
            bob_flag.status = "flagged"
            bob_flag.reprove_deadline = now + timedelta(hours=7)
            for u in (a, c, d):
                db.add(Vote(checkin_id=bob_flag.id, user_id=u.id, value="sus"))
        db.add(UserBadge(user_id=d.id, key="caught_cappin", expires_at=now + timedelta(days=4)))

        # Taunts: bob taunted alice an hour ago (she sees the dialog and can CLAP BACK).
        db.add(Taunt(sender_id=b.id, target_id=a.id, key="crying", sender_day=today, created_at=now - timedelta(hours=1)))
        db.add(Taunt(sender_id=c.id, target_id=d.id, key="chicken", sender_day=today - timedelta(days=1),
                     created_at=now - timedelta(hours=20), seen=True))

        # Boss battles: this week's fight is live; last week the Night Owls lost.
        b_now = game.boss_for_week(crew.id, ws)
        boss = BossBattle(party_id=crew.id, week_start=ws, boss_key=b_now.key, hp_max=game.boss_hp(4), hp=game.boss_hp(4))
        db.add(boss)
        db.flush()
        for ci in checkins:
            if ci.local_date >= ws and ci.user_id in (a.id, b.id, c.id, d.id) and ci.status != "rejected":
                db.add(BossHit(boss_id=boss.id, user_id=ci.user_id, checkin_id=ci.id, damage=ci.points, created_at=ci.created_at))
                boss.hp = max(boss.hp_max * 2 // 5, boss.hp - ci.points)  # keep the demo fight winnable but alive
        crew.pet_points = sum(1 for ci in checkins if ci.user_id in (a.id, b.id, c.id, d.id))
        owls.pet_points = sum(1 for ci in checkins if ci.user_id in (b.id, e.id))
        last = game.boss_for_week(owls.id, ws - timedelta(days=7))
        db.add(BossBattle(party_id=owls.id, week_start=ws - timedelta(days=7), boss_key=last.key, hp_max=500, hp=140, status="failed"))
        db.add(FeedEvent(party_id=owls.id, kind="boss", icon=last.sprite, text=f"{last.laugh} (Night Owls left it at 140 HP)",
                         created_at=datetime.combine(ws, time(0, 10))))
        db.add(FeedEvent(party_id=crew.id, kind="boss", icon=b_now.sprite,
                         text=f"A wild {b_now.name.upper()} appears before Pixel Pushers! HP {boss.hp_max}",
                         created_at=datetime.combine(ws, time(0, 5))))

        # Duels and a stake pot.
        db.add(Duel(challenger_id=a.id, opponent_id=b.id, days=7, status="active", start_date=today - timedelta(days=4),
                    end_date=today + timedelta(days=2), created_at=now - timedelta(days=4)))
        db.add(Duel(challenger_id=d.id, opponent_id=a.id, days=5, status="pending"))
        db.add(Duel(challenger_id=c.id, opponent_id=a.id, days=3, status="finished", start_date=today - timedelta(days=10),
                    end_date=today - timedelta(days=8), winner_id=a.id))
        ch = Challenge(party_id=crew.id, creator_id=a.id, name="October Grind", start_date=today - timedelta(days=3),
                       end_date=today + timedelta(days=10), stake=50, required=10, pot=200, status="active")
        db.add(ch)
        db.flush()
        for u in (a, b, c, d):
            db.add(ChallengeMember(challenge_id=ch.id, user_id=u.id))
            u.coins -= 50

        # Daily dare: cara already did today's.
        db.add(DareClaim(party_id=crew.id, user_id=c.id, day=today, dare=game.daily_dare(crew.id, today),
                         photo_path=_seed_photo("dare done!", c.avatar_color, rng)))
        db.add(FeedEvent(party_id=crew.id, user_id=c.id, kind="dare", icon="😈",
                         text=f"cara completed today's dare: {game.daily_dare(crew.id, today)}", created_at=now - timedelta(minutes=50)))

        # Notifications for alice's bell.
        notify(db, a.id, "friend", "👋 eve wants to be friends", "/friends", push=False, announced=True)
        notify(db, a.id, "duel", "⚔️ dan challenges you to a 5-day STREAK DUEL!", "/challenges", push=False, announced=True)
        notify(db, a.id, "boss", f"{b_now.sprite} BOSS BATTLE! {b_now.name} attacks Pixel Pushers", "/boss", push=False, announced=True)
        notify(db, a.id, "taunt", "😈 PLAYER BOB HAS TAUNTED YOU: 🔥 Your streak called. It's crying.", "/", push=False, announced=True)

        for u in users.values():
            db.add(u)
        db.flush()
        for u in users.values():
            check_badges(db, u)
        db.add(UserBadge(user_id=a.id, key="duelist"))
        db.add(UserBadge(user_id=b.id, key="comeback_kid"))
        db.commit()
    print("Seeded demo world. Log in as alice / arcade123 (also bob, cara, dan, eve).")


# ===========================================================================
# Entry point
# ===========================================================================


def seed_allowed(debug: bool, allow_demo: bool, host: str, database_url: str) -> bool:
    """Demo players share a public password, so only seed a local dev database unless explicitly allowed."""
    local_host = host in ("127.0.0.1", "localhost", "::1")
    return debug or allow_demo or (local_host and database_url.startswith("sqlite"))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Habitor — build habits that stick, together.")
    parser.add_argument("--seed", action="store_true", help="load demo players, party, boss battle… then start")
    parser.add_argument("--seed-only", action="store_true", help="load demo data and exit")
    parser.add_argument("--allow-demo", action="store_true",
                        help="permit demo players (public password!) on a non-local host or non-SQLite database")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.seed or args.seed_only:
        if not seed_allowed(DEBUG, args.allow_demo, args.host, DATABASE_URL):
            print("Refusing to create demo players: they share the public password 'arcade123'.\n"
                  "Seeding is allowed on a local SQLite database, with DEBUG=1, or with --allow-demo.")
            sys.exit(2)
        seed()
        if args.seed_only:
            return
    import uvicorn

    print(f"\n  >> HABITOR is live at http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}  (Ctrl+C to quit)\n")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, log_config=None)


if __name__ == "__main__":
    main()
