from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("FLIGHT_DATABASE_PATH", BASE_DIR / "data" / "flights.db"))
SESSION_SECRET = os.getenv("RIHAL_SESSION_SECRET") or secrets.token_urlsafe(32)
SESSION_COOKIE = "rihal_session"
SESSION_MAX_AGE = 8 * 60 * 60
COOKIE_SECURE = os.getenv("RIHAL_COOKIE_SECURE", "false").casefold() == "true"
PASSWORD_ITERATIONS = 310_000
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
logger = logging.getLogger("rihal")


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class UserRole(str, Enum):
    admin = "admin"
    staff = "staff"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    role: UserRole = UserRole.staff

    @field_validator("username", mode="before")
    @classmethod
    def strip_username(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not value or any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("اسم المستخدم لا يقبل المسافات أو محارف التحكم")
        return value


class UserActiveInput(BaseModel):
    active: bool


class FlightStatus(str, Enum):
    scheduled = "scheduled"
    boarding = "boarding"
    delayed = "delayed"
    cancelled = "cancelled"
    completed = "completed"


class FlightSort(str, Enum):
    departure = "departure"
    flight_number = "flight_number"
    price = "price"
    capacity = "capacity"


class SortDirection(str, Enum):
    ascending = "asc"
    descending = "desc"


class FlightInput(BaseModel):
    flight_number: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    origin: str = Field(min_length=2, max_length=80)
    destination: str = Field(min_length=2, max_length=80)
    departure_time: datetime
    arrival_time: datetime
    capacity: int = Field(ge=1, le=1000)
    price: float = Field(ge=0, le=1_000_000)
    status: FlightStatus = FlightStatus.scheduled

    @field_validator("flight_number", "origin", "destination", mode="before")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_route_and_times(self) -> FlightInput:
        if self.origin.strip().casefold() == self.destination.strip().casefold():
            raise ValueError("يجب أن تختلف مدينة الوصول عن مدينة المغادرة")
        departure_is_aware = self.departure_time.utcoffset() is not None
        arrival_is_aware = self.arrival_time.utcoffset() is not None
        if departure_is_aware != arrival_is_aware:
            raise ValueError("يجب تحديد المنطقة الزمنية لكلا الموعدين أو تركهما دون منطقة زمنية")
        if self.arrival_time <= self.departure_time:
            raise ValueError("يجب أن يكون موعد الوصول بعد موعد المغادرة")
        return self


class Flight(FlightInput):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class FlightPage(BaseModel):
    items: list[Flight]
    total: int
    page: int
    page_size: int
    total_pages: int


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"


class BookingInput(BaseModel):
    flight_id: int = Field(ge=1)
    passenger_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=24)
    email: str | None = Field(default=None, max_length=254)
    seats: int = Field(ge=1, le=20)

    @field_validator("passenger_name", "phone", "email", mode="before")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("passenger_name")
    @classmethod
    def validate_passenger_name(cls, value: str) -> str:
        if not value:
            raise ValueError("اسم الراكب مطلوب")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not all(character.isdigit() or character in "+() .-" for character in value):
            raise ValueError("رقم الهاتف يحتوي على محارف غير صالحة")
        if sum(character.isdigit() for character in value) < 7:
            raise ValueError("يرجى إدخال رقم هاتف صحيح")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("يرجى إدخال بريد إلكتروني صحيح")
        return value or None


class BookingStatusInput(BaseModel):
    status: BookingStatus


class Booking(BaseModel):
    id: int
    flight_id: int
    flight_number: str
    passenger_name: str
    phone: str
    email: str | None
    seats: int
    status: BookingStatus
    created_at: datetime
    updated_at: datetime


class BookingPage(BaseModel):
    items: list[Booking]
    total: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: UserRole


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def run_migrations() -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_number TEXT NOT NULL UNIQUE COLLATE NOCASE,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            arrival_time TEXT NOT NULL,
            capacity INTEGER NOT NULL CHECK (capacity > 0),
            price REAL NOT NULL CHECK (price >= 0),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE RESTRICT,
            passenger_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            seats INTEGER NOT NULL CHECK (seats > 0),
            status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            flight_id INTEGER REFERENCES flights(id) ON DELETE SET NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            client_ip TEXT PRIMARY KEY,
            failures INTEGER NOT NULL,
            window_started_at REAL NOT NULL,
            blocked_until REAL NOT NULL DEFAULT 0
        )
        """,
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_flights_departure ON flights(departure_time)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_flight_status ON bookings(flight_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_created ON bookings(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
    )
    with get_connection() as connection:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            (iso_now(),),
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, encoded_salt, encoded_digest = password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = base64.urlsafe_b64decode(encoded_digest.encode())
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.urlsafe_b64decode(encoded_salt.encode()),
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def seed_initial_admin() -> None:
    with get_connection() as connection:
        has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_users:
            return
        username = os.getenv("RIHAL_USERNAME")
        password = os.getenv("RIHAL_PASSWORD")
        if not username or not password:
            logger.warning("No users exist; set administrator bootstrap environment variables.")
            return
        initial_admin = UserCreate(
            username=username,
            password=password,
            role=UserRole.admin,
        )
        connection.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (
                initial_admin.username,
                hash_password(initial_admin.password),
                iso_now(),
            ),
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    run_migrations()
    seed_initial_admin()
    yield


app = FastAPI(
    title="رِحال | إدارة الرحلات الجوية",
    description="واجهة برمجية لإدارة الرحلات والحجوزات والمستخدمين.",
    version="2.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def flight_from_row(row: sqlite3.Row) -> Flight:
    return Flight.model_validate(dict(row))


def create_session_token(username: str) -> str:
    payload = json.dumps(
        {"sub": username, "exp": int(time.time()) + SESSION_MAX_AGE},
        separators=(",", ":"),
    ).encode()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        SESSION_SECRET.encode(), encoded_payload.encode(), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_payload}.{encoded_signature}"


def session_username(token: str | None) -> str | None:
    if not token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        expected_signature = hmac.new(
            SESSION_SECRET.encode(), encoded_payload.encode(), hashlib.sha256
        ).digest()
        supplied_signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
        if not hmac.compare_digest(expected_signature, supplied_signature):
            return None
        payload = base64.urlsafe_b64decode(
            encoded_payload + "=" * (-len(encoded_payload) % 4)
        )
        claims = json.loads(payload)
        if not isinstance(claims, dict) or claims.get("exp", 0) <= time.time():
            return None
        username = claims.get("sub")
        return username if isinstance(username, str) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def get_current_user(
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> CurrentUser:
    username = session_username(session)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="يرجى تسجيل الدخول للمتابعة",
        )
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if row is None or not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="انتهت صلاحية الجلسة أو تم تعطيل الحساب",
        )
    return CurrentUser(row["id"], row["username"], UserRole(row["role"]))


def require_admin(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="هذه العملية متاحة لمدير النظام فقط",
        )
    return current_user


UserDep = Annotated[CurrentUser, Depends(get_current_user)]
AdminDep = Annotated[CurrentUser, Depends(require_admin)]


def set_session_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def enforce_login_limit(connection: sqlite3.Connection, client_ip: str) -> None:
    now = time.time()
    row = connection.execute(
        "SELECT blocked_until FROM login_attempts WHERE client_ip = ?", (client_ip,)
    ).fetchone()
    if row is not None and row["blocked_until"] > now:
        retry_after = int(row["blocked_until"] - now) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="تم تجاوز عدد محاولات الدخول. حاول لاحقاً.",
            headers={"Retry-After": str(retry_after)},
        )


def record_login_failure(connection: sqlite3.Connection, client_ip: str) -> None:
    now = time.time()
    row = connection.execute(
        "SELECT failures, window_started_at FROM login_attempts WHERE client_ip = ?",
        (client_ip,),
    ).fetchone()
    if row is None or now - row["window_started_at"] > LOGIN_WINDOW_SECONDS:
        failures = 1
        window_started_at = now
    else:
        failures = row["failures"] + 1
        window_started_at = row["window_started_at"]
    blocked_until = now + LOGIN_LOCK_SECONDS if failures >= LOGIN_FAILURE_LIMIT else 0
    connection.execute(
        """
        INSERT INTO login_attempts(client_ip, failures, window_started_at, blocked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(client_ip) DO UPDATE SET
            failures = excluded.failures,
            window_started_at = excluded.window_started_at,
            blocked_until = excluded.blocked_until
        """,
        (client_ip, failures, window_started_at, blocked_until),
    )


def record_audit(
    connection: sqlite3.Connection,
    actor: CurrentUser,
    action: str,
    entity: str,
    entity_id: int | None,
    details: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log(actor_id, actor_username, action, entity, entity_id, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor.id,
            actor.username,
            action,
            entity,
            entity_id,
            json.dumps(details, ensure_ascii=False, separators=(",", ":")),
            iso_now(),
        ),
    )


def notify_flight_status(
    connection: sqlite3.Connection,
    flight_id: int,
    flight_number: str,
    flight_status: FlightStatus,
) -> None:
    if flight_status not in (FlightStatus.delayed, FlightStatus.cancelled):
        return
    labels = {
        FlightStatus.delayed: "تأخرت",
        FlightStatus.cancelled: "أُلغيت",
    }
    users = connection.execute(
        "SELECT id FROM users WHERE is_active = 1"
    ).fetchall()
    connection.executemany(
        """
        INSERT INTO notifications(user_id, flight_id, message, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                user["id"],
                flight_id,
                f"الرحلة {flight_number} {labels[flight_status]}.",
                iso_now(),
            )
            for user in users
        ],
    )


def build_flight_filters(
    search: str | None,
    flight_status: FlightStatus | None,
    start_date: date | None,
    end_date: date | None,
) -> tuple[str, list[object]]:
    conditions: list[str] = []
    parameters: list[object] = []
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        conditions.append("(flight_number LIKE ? OR origin LIKE ? OR destination LIKE ?)")
        parameters.extend((pattern, pattern, pattern))
    if flight_status:
        conditions.append("status = ?")
        parameters.append(flight_status.value)
    if start_date:
        conditions.append("substr(departure_time, 1, 10) >= ?")
        parameters.append(start_date.isoformat())
    if end_date:
        conditions.append("substr(departure_time, 1, 10) <= ?")
        parameters.append(end_date.isoformat())
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="تاريخ النهاية يجب أن يكون بعد تاريخ البداية",
        )
    return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), parameters


def query_flights(
    connection: sqlite3.Connection,
    where_clause: str,
    parameters: list[object],
    page: int,
    page_size: int,
    sort_by: FlightSort = FlightSort.departure,
    sort_order: SortDirection = SortDirection.ascending,
) -> FlightPage:
    total = connection.execute(
        f"SELECT COUNT(*) FROM flights {where_clause}", parameters
    ).fetchone()[0]
    sort_columns = {
        FlightSort.departure: "julianday(departure_time)",
        FlightSort.flight_number: "flight_number COLLATE NOCASE",
        FlightSort.price: "price",
        FlightSort.capacity: "capacity",
    }
    rows = connection.execute(
        f"""
        SELECT * FROM flights {where_clause}
        ORDER BY {sort_columns[sort_by]} {sort_order.value.upper()}, id DESC
        LIMIT ? OFFSET ?
        """,
        [*parameters, page_size, (page - 1) * page_size],
    ).fetchall()
    return FlightPage(
        items=[flight_from_row(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


def safe_csv_value(value: object) -> object:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_response(filename: str, headers: list[str], rows: list[list[object]]) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows([[safe_csv_value(value) for value in row] for row in rows])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/auth/login", tags=["المصادقة"])
def login(
    credentials: LoginInput, response: Response, request: Request
) -> dict[str, str | bool | int]:
    client_ip = request.client.host if request.client else "unknown"
    invalid_credentials = False
    with get_connection() as connection:
        enforce_login_limit(connection, client_ip)
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="لا يوجد حساب مدير بعد. اضبط بيانات التهيئة وأعد تشغيل الخادم.",
            )
        row = connection.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1",
            (credentials.username.strip(),),
        ).fetchone()
        if row is None or not verify_password(credentials.password, row["password_hash"]):
            record_login_failure(connection, client_ip)
            logger.warning("Rejected login attempt from %s", client_ip)
            invalid_credentials = True
        else:
            connection.execute("DELETE FROM login_attempts WHERE client_ip = ?", (client_ip,))
            set_session_cookie(response, row["username"])
            result = {
                "authenticated": True,
                "username": row["username"],
                "user_id": row["id"],
                "role": connection.execute(
                    "SELECT role FROM users WHERE id = ?", (row["id"],)
                ).fetchone()["role"],
            }
    if invalid_credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="اسم المستخدم أو كلمة المرور غير صحيحة.",
        )
    return result


@app.get("/api/auth/session", tags=["المصادقة"])
def get_session(
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, str | bool | int]:
    username = session_username(session)
    if username is None:
        return {"authenticated": False}
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1",
            (username,),
        ).fetchone()
    if row is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": row["username"],
        "user_id": row["id"],
        "role": row["role"],
    }


@app.post("/api/auth/logout", tags=["المصادقة"])
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return {"authenticated": False}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/stats", tags=["لوحة المعلومات"])
def get_stats(_: UserDep) -> dict[str, int]:
    with get_connection() as connection:
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) AS scheduled,
                SUM(CASE WHEN status = 'boarding' THEN 1 ELSE 0 END) AS boarding,
                SUM(CASE WHEN status = 'delayed' THEN 1 ELSE 0 END) AS delayed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM flights
            """
        ).fetchone()
        booked = connection.execute(
            "SELECT COALESCE(SUM(seats), 0) FROM bookings WHERE status = 'confirmed'"
        ).fetchone()[0]
        booking_count = connection.execute(
            "SELECT COUNT(*) FROM bookings WHERE status = 'confirmed'"
        ).fetchone()[0]
    result = {key: counts[key] or 0 for key in counts.keys()}
    result["confirmed_bookings"] = booking_count
    result["booked_seats"] = booked
    return result


@app.get("/api/flights", response_model=list[Flight], tags=["الرحلات"])
def list_flights(
    _: UserDep,
    search: Annotated[str | None, Query(max_length=80)] = None,
    flight_status: Annotated[FlightStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[Flight]:
    where_clause, parameters = build_flight_filters(search, flight_status, None, None)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM flights {where_clause}
            ORDER BY julianday(departure_time) ASC, id DESC
            LIMIT ?
            """,
            [*parameters, limit],
        ).fetchall()
    return [flight_from_row(row) for row in rows]


@app.get("/api/flights/page", response_model=FlightPage, tags=["الرحلات"])
def list_flights_page(
    _: UserDep,
    search: Annotated[str | None, Query(max_length=80)] = None,
    flight_status: Annotated[FlightStatus | None, Query(alias="status")] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sort_by: FlightSort = FlightSort.departure,
    sort_order: SortDirection = SortDirection.ascending,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FlightPage:
    where_clause, parameters = build_flight_filters(search, flight_status, start_date, end_date)
    with get_connection() as connection:
        return query_flights(
            connection, where_clause, parameters, page, page_size, sort_by, sort_order
        )


@app.get("/api/flights/export.csv", tags=["الرحلات"])
def export_flights(
    _: UserDep,
    search: Annotated[str | None, Query(max_length=80)] = None,
    flight_status: Annotated[FlightStatus | None, Query(alias="status")] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sort_by: FlightSort = FlightSort.departure,
    sort_order: SortDirection = SortDirection.ascending,
) -> Response:
    where_clause, parameters = build_flight_filters(search, flight_status, start_date, end_date)
    sort_columns = {
        FlightSort.departure: "julianday(departure_time)",
        FlightSort.flight_number: "flight_number COLLATE NOCASE",
        FlightSort.price: "price",
        FlightSort.capacity: "capacity",
    }
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM flights {where_clause} ORDER BY {sort_columns[sort_by]} {sort_order.value.upper()}, id DESC",
            parameters,
        ).fetchall()
    return csv_response(
        "rihal-flights.csv",
        ["رقم الرحلة", "المغادرة", "الوصول", "موعد المغادرة", "موعد الوصول", "السعة", "السعر", "الحالة"],
        [
            [
                row["flight_number"],
                row["origin"],
                row["destination"],
                row["departure_time"],
                row["arrival_time"],
                row["capacity"],
                row["price"],
                row["status"],
            ]
            for row in rows
        ],
    )


@app.post(
    "/api/flights",
    response_model=Flight,
    status_code=status.HTTP_201_CREATED,
    tags=["الرحلات"],
)
def create_flight(flight: FlightInput, current_user: UserDep) -> Flight:
    values = flight.model_dump()
    values["flight_number"] = values["flight_number"].upper()
    values["departure_time"] = flight.departure_time.isoformat()
    values["arrival_time"] = flight.arrival_time.isoformat()
    values["status"] = flight.status.value
    values["created_at"] = iso_now()
    with get_connection() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO flights (
                    flight_number, origin, destination, departure_time,
                    arrival_time, capacity, price, status, created_at
                ) VALUES (
                    :flight_number, :origin, :destination, :departure_time,
                    :arrival_time, :capacity, :price, :status, :created_at
                )
                """,
                values,
            )
        except sqlite3.IntegrityError as error:
            if "flight_number" in str(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="رقم الرحلة مستخدم بالفعل",
                ) from error
            raise
        record_audit(
            connection,
            current_user,
            "create",
            "flight",
            cursor.lastrowid,
            {"flight_number": values["flight_number"]},
        )
        notify_flight_status(
            connection, cursor.lastrowid, values["flight_number"], flight.status
        )
        row = connection.execute(
            "SELECT * FROM flights WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return flight_from_row(row)


@app.get("/api/flights/{flight_id}", response_model=Flight, tags=["الرحلات"])
def get_flight(flight_id: int, _: UserDep) -> Flight:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="الرحلة غير موجودة")
    return flight_from_row(row)


@app.put("/api/flights/{flight_id}", response_model=Flight, tags=["الرحلات"])
def update_flight(
    flight_id: int, flight: FlightInput, current_user: UserDep
) -> Flight:
    values = flight.model_dump()
    values["flight_number"] = values["flight_number"].upper()
    values["departure_time"] = flight.departure_time.isoformat()
    values["arrival_time"] = flight.arrival_time.isoformat()
    values["status"] = flight.status.value
    with get_connection() as connection:
        old = connection.execute(
            "SELECT flight_number, status FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
        if old is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="الرحلة غير موجودة"
            )
        booked_seats = connection.execute(
            "SELECT COALESCE(SUM(seats), 0) FROM bookings WHERE flight_id = ? AND status = 'confirmed'",
            (flight_id,),
        ).fetchone()[0]
        if values["capacity"] < booked_seats:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="لا يمكن خفض السعة عن عدد المقاعد المحجوزة حالياً",
            )
        try:
            connection.execute(
                """
                UPDATE flights
                SET flight_number = :flight_number,
                    origin = :origin,
                    destination = :destination,
                    departure_time = :departure_time,
                    arrival_time = :arrival_time,
                    capacity = :capacity,
                    price = :price,
                    status = :status
                WHERE id = :id
                """,
                {**values, "id": flight_id},
            )
        except sqlite3.IntegrityError as error:
            if "flight_number" in str(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="رقم الرحلة مستخدم بالفعل",
                ) from error
            raise
        record_audit(
            connection,
            current_user,
            "update",
            "flight",
            flight_id,
            {"flight_number": values["flight_number"], "status": values["status"]},
        )
        if old["status"] != flight.status.value:
            notify_flight_status(
                connection, flight_id, values["flight_number"], flight.status
            )
        row = connection.execute(
            "SELECT * FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
    return flight_from_row(row)


@app.delete(
    "/api/flights/{flight_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["الرحلات"],
)
def delete_flight(flight_id: int, current_user: UserDep) -> Response:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT flight_number FROM flights WHERE id = ?", (flight_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="الرحلة غير موجودة"
            )
        bookings = connection.execute(
            "SELECT COUNT(*) FROM bookings WHERE flight_id = ?", (flight_id,)
        ).fetchone()[0]
        if bookings:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="لا يمكن حذف رحلة لها سجل حجوزات؛ احتفظ بالرحلة للحفاظ على سجل الركاب",
            )
        connection.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
        record_audit(
            connection,
            current_user,
            "delete",
            "flight",
            flight_id,
            {"flight_number": row["flight_number"]},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def booking_from_row(row: sqlite3.Row) -> Booking:
    return Booking.model_validate(dict(row))


@app.get("/api/bookings", response_model=BookingPage, tags=["الحجوزات"])
def list_bookings(
    _: UserDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
    booking_status: Annotated[BookingStatus | None, Query(alias="status")] = None,
    flight_id: int | None = Query(default=None, ge=1),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BookingPage:
    conditions: list[str] = []
    parameters: list[object] = []
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        conditions.append("(b.passenger_name LIKE ? OR b.phone LIKE ? OR f.flight_number LIKE ?)")
        parameters.extend((pattern, pattern, pattern))
    if booking_status:
        conditions.append("b.status = ?")
        parameters.append(booking_status.value)
    if flight_id:
        conditions.append("b.flight_id = ?")
        parameters.append(flight_id)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    base_query = """
        FROM bookings b JOIN flights f ON f.id = b.flight_id
    """
    with get_connection() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) {base_query} {where_clause}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT b.id, b.flight_id, f.flight_number, b.passenger_name, b.phone,
                   b.email, b.seats, b.status, b.created_at, b.updated_at
            {base_query} {where_clause}
            ORDER BY b.created_at DESC, b.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
    return BookingPage(
        items=[booking_from_row(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@app.get("/api/bookings/export.csv", tags=["الحجوزات"])
def export_bookings(_: UserDep) -> Response:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT b.id, f.flight_number, b.passenger_name, b.phone, b.email,
                   b.seats, b.status, b.created_at
            FROM bookings b JOIN flights f ON f.id = b.flight_id
            ORDER BY b.created_at DESC
            """
        ).fetchall()
    return csv_response(
        "rihal-bookings.csv",
        ["رقم الحجز", "رقم الرحلة", "اسم الراكب", "الهاتف", "البريد", "المقاعد", "الحالة", "تاريخ الإنشاء"],
        [list(row) for row in rows],
    )


@app.post(
    "/api/bookings",
    response_model=Booking,
    status_code=status.HTTP_201_CREATED,
    tags=["الحجوزات"],
)
def create_booking(booking: BookingInput, current_user: UserDep) -> Booking:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        flight = connection.execute(
            "SELECT flight_number, capacity, status FROM flights WHERE id = ?",
            (booking.flight_id,),
        ).fetchone()
        if flight is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="الرحلة غير موجودة"
            )
        if flight["status"] in ("cancelled", "completed"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="لا يمكن الحجز على رحلة ملغاة أو مكتملة",
            )
        booked = connection.execute(
            "SELECT COALESCE(SUM(seats), 0) FROM bookings WHERE flight_id = ? AND status = 'confirmed'",
            (booking.flight_id,),
        ).fetchone()[0]
        if booked + booking.seats > flight["capacity"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"المقاعد المتاحة لهذه الرحلة: {flight['capacity'] - booked}",
            )
        now = iso_now()
        cursor = connection.execute(
            """
            INSERT INTO bookings(
                flight_id, passenger_name, phone, email, seats, status, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)
            """,
            (
                booking.flight_id,
                booking.passenger_name,
                booking.phone,
                booking.email,
                booking.seats,
                current_user.id,
                now,
                now,
            ),
        )
        record_audit(
            connection,
            current_user,
            "create",
            "booking",
            cursor.lastrowid,
            {"flight_number": flight["flight_number"], "seats": booking.seats},
        )
        row = connection.execute(
            """
            SELECT b.id, b.flight_id, f.flight_number, b.passenger_name, b.phone,
                   b.email, b.seats, b.status, b.created_at, b.updated_at
            FROM bookings b JOIN flights f ON f.id = b.flight_id WHERE b.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return booking_from_row(row)


@app.get("/api/bookings/{booking_id}", response_model=Booking, tags=["الحجوزات"])
def get_booking(booking_id: int, _: UserDep) -> Booking:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT b.id, b.flight_id, f.flight_number, b.passenger_name, b.phone,
                   b.email, b.seats, b.status, b.created_at, b.updated_at
            FROM bookings b JOIN flights f ON f.id = b.flight_id WHERE b.id = ?
            """,
            (booking_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="الحجز غير موجود")
    return booking_from_row(row)


@app.patch("/api/bookings/{booking_id}", response_model=Booking, tags=["الحجوزات"])
def update_booking_status(
    booking_id: int, update: BookingStatusInput, current_user: UserDep
) -> Booking:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        booking = connection.execute(
            """
            SELECT b.*, f.flight_number, f.capacity, f.status AS flight_status
            FROM bookings b JOIN flights f ON f.id = b.flight_id WHERE b.id = ?
            """,
            (booking_id,),
        ).fetchone()
        if booking is None:
            raise HTTPException(status_code=404, detail="الحجز غير موجود")
        if update.status == BookingStatus.confirmed and booking["status"] != "confirmed":
            if booking["flight_status"] in ("cancelled", "completed"):
                raise HTTPException(
                    status_code=409, detail="لا يمكن تأكيد حجز لرحلة ملغاة أو مكتملة"
                )
            booked = connection.execute(
                """
                SELECT COALESCE(SUM(seats), 0) FROM bookings
                WHERE flight_id = ? AND status = 'confirmed' AND id != ?
                """,
                (booking["flight_id"], booking_id),
            ).fetchone()[0]
            if booked + booking["seats"] > booking["capacity"]:
                raise HTTPException(status_code=409, detail="لا توجد مقاعد كافية لإعادة تأكيد الحجز")
        connection.execute(
            "UPDATE bookings SET status = ?, updated_at = ? WHERE id = ?",
            (update.status.value, iso_now(), booking_id),
        )
        record_audit(
            connection,
            current_user,
            "update",
            "booking",
            booking_id,
            {"status": update.status.value},
        )
        row = connection.execute(
            """
            SELECT b.id, b.flight_id, f.flight_number, b.passenger_name, b.phone,
                   b.email, b.seats, b.status, b.created_at, b.updated_at
            FROM bookings b JOIN flights f ON f.id = b.flight_id WHERE b.id = ?
            """,
            (booking_id,),
        ).fetchone()
    return booking_from_row(row)


@app.get("/api/users", tags=["المستخدمون"])
def list_users(_: AdminDep) -> list[dict[str, object]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, username, role, is_active, created_at FROM users ORDER BY id"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "active": bool(row["is_active"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.post("/api/users", status_code=status.HTTP_201_CREATED, tags=["المستخدمون"])
def create_user(user: UserCreate, current_user: AdminDep) -> dict[str, object]:
    with get_connection() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user.username, hash_password(user.password), user.role.value, iso_now()),
            )
        except sqlite3.IntegrityError as error:
            if "username" in str(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="اسم المستخدم مستخدم بالفعل",
                ) from error
            raise
        record_audit(
            connection,
            current_user,
            "create",
            "user",
            cursor.lastrowid,
            {"username": user.username, "role": user.role.value},
        )
    return {
        "id": cursor.lastrowid,
        "username": user.username,
        "role": user.role.value,
        "active": True,
    }


@app.put("/api/users/{user_id}/active", tags=["المستخدمون"])
def set_user_active(
    user_id: int, update: UserActiveInput, current_user: AdminDep
) -> dict[str, object]:
    with get_connection() as connection:
        target = connection.execute(
            "SELECT username, role, is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")
        if user_id == current_user.id and not update.active:
            raise HTTPException(status_code=409, detail="لا يمكنك تعطيل حسابك الحالي")
        if target["role"] == "admin" and target["is_active"] and not update.active:
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()[0]
            if active_admins <= 1:
                raise HTTPException(status_code=409, detail="يجب إبقاء مدير واحد نشط على الأقل")
        connection.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (int(update.active), user_id),
        )
        record_audit(
            connection,
            current_user,
            "update",
            "user",
            user_id,
            {"username": target["username"], "active": update.active},
        )
    return {"id": user_id, "username": target["username"], "active": update.active}


@app.get("/api/notifications", tags=["التنبيهات"])
def list_notifications(current_user: UserDep) -> dict[str, object]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, flight_id, message, created_at
            FROM notifications WHERE user_id = ? AND read_at IS NULL
            ORDER BY created_at DESC LIMIT 50
            """,
            (current_user.id,),
        ).fetchall()
        count = connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
            (current_user.id,),
        ).fetchone()[0]
    return {
        "unread": count,
        "items": [dict(row) for row in rows],
    }


@app.post("/api/notifications/{notification_id}/read", status_code=204, tags=["التنبيهات"])
def mark_notification_read(notification_id: int, current_user: UserDep) -> Response:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE notifications SET read_at = ?
            WHERE id = ? AND user_id = ? AND read_at IS NULL
            """,
            (iso_now(), notification_id, current_user.id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="التنبيه غير موجود")
    return Response(status_code=204)


@app.get("/api/audit", tags=["التدقيق"])
def list_audit_log(
    _: AdminDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    with get_connection() as connection:
        total = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        rows = connection.execute(
            """
            SELECT id, actor_username, action, entity, entity_id, details_json, created_at
            FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [
            {
                **dict(row),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


def remove_backup(path: Path) -> None:
    path.unlink(missing_ok=True)


@app.get("/api/admin/backup", tags=["الإدارة"])
def download_backup(_: AdminDep) -> StreamingResponse:
    descriptor, filename = tempfile.mkstemp(prefix="rihal-backup-", suffix=".db")
    os.close(descriptor)
    backup_path = Path(filename)
    try:
        with get_connection() as source:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
    except sqlite3.Error:
        remove_backup(backup_path)
        logger.exception("Could not create database backup")
        raise

    def stream_backup() -> Iterator[bytes]:
        try:
            with backup_path.open("rb") as backup_file:
                while chunk := backup_file.read(64 * 1024):
                    yield chunk
        finally:
            remove_backup(backup_path)

    return StreamingResponse(
        stream_backup(),
        media_type="application/vnd.sqlite3",
        headers={
            "Content-Disposition": (
                f'attachment; filename="rihal-backup-{datetime.now().astimezone():%Y%m%d-%H%M%S}.db"'
            )
        },
    )
