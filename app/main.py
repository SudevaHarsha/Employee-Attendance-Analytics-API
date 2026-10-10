"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

All application code lives in this file. Contract: openapi.yaml. Storage: DATA_MODEL.md.
Business rules R1-R10 are defined in openapi.yaml.
"""
import calendar
import logging
import os
from bisect import bisect_right
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Real environment variables win: load_dotenv(override=False) is the default.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# An instant must look like milliseconds (see openapi EpochMillis).
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

logger = logging.getLogger("attendance")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current instant, truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware/naive datetime -> epoch milliseconds (whole seconds). None passes through."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return calendar.timegm(dt.utctimetuple()) * 1000


def to_ist(dt: datetime) -> datetime:
    """Convert an instant to IST (R1)."""
    return dt.astimezone(IST)


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(emp: dict) -> bool:
    """R1: shift_end <= shift_start means an overnight shift."""
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """
    R1: a record's date is the IST calendar date of its punch_in. For an overnight shift a
    punch-in whose IST time is earlier than shift_end belongs to the shift that started the
    previous day.
    """
    ist = to_ist(punch_in)
    day = ist.date()
    if is_overnight(emp):
        eh, em = _hhmm(emp["shift_end"])
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day = day - timedelta(days=1)
    return day.isoformat()


def shift_start_ist(date_str: str, emp: dict) -> datetime:
    h, m = _hhmm(emp["shift_start"])
    y, mo, d = (int(x) for x in date_str.split("-"))
    return datetime(y, mo, d, h, m, 0, tzinfo=IST)


def shift_end_ist(date_str: str, emp: dict) -> datetime:
    """Shift end in IST; for an overnight shift it falls on the next calendar day (R1)."""
    h, m = _hhmm(emp["shift_end"])
    y, mo, d = (int(x) for x in date_str.split("-"))
    end = datetime(y, mo, d, h, m, 0, tzinfo=IST)
    if is_overnight(emp):
        end = end + timedelta(days=1)
    return end


# --------------------------------------------------------------------------- #
# Derived-field helpers (R2-R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """
    R2: grace is 10 minutes; late only if the punch-in is strictly more than 10:00 after
    shift_start. late_minutes is the whole minutes (floor) since shift_start.
    """
    delta_seconds = int((to_ist(punch_in) - shift_start_ist(date_str, emp)).total_seconds())
    if delta_seconds <= 600:
        return 0
    return delta_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes (floor) from shift_end to punch_out, only if >= 30, else 0."""
    seconds = int((to_ist(punch_out) - shift_end_ist(date_str, emp)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half_day is true when the rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def round_half_up(value: float, places: int) -> float:
    """R8: round half-up (not banker's) to `places` decimals."""
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Calendar / reporting helpers (R5, R7, R9, R10) - shared by analytics
# --------------------------------------------------------------------------- #
PRESENT_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})  # R6
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_range(month: str) -> tuple[date, date]:
    """First and last calendar day of a `YYYY-MM` month, both inclusive."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, 12, 31) if mon == 12 else date(year, mon + 1, 1) - timedelta(days=1)
    return first, last


def month_date_filter(month: str) -> dict:
    """Mongo filter selecting logs whose stored `date` string falls inside the month."""
    first, last = month_range(month)
    return {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def is_working_day(day: date) -> bool:
    """R7: Mon-Fri, no holiday calendar."""
    return day.weekday() < 5


def count_working_days(start: date, end: date) -> int:
    """Inclusive count of Mon-Fri days between `start` and `end` (0 when end < start)."""
    if end < start:
        return 0
    total = (end - start).days + 1
    weeks, remainder = divmod(total, 7)
    count = weeks * 5
    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            count += 1
    return count


def working_days_in_month(month: str, joined_on: Optional[str] = None) -> int:
    """R7: working days in the month, counted from `joined_on` for a mid-month joiner."""
    first, last = month_range(month)
    if joined_on:
        first = max(first, date.fromisoformat(joined_on))
    return count_working_days(first, last)


def present_day_value(half_day: bool) -> float:
    """R5: a half day counts as 0.5 of a present day, a full present day as 1."""
    return 0.5 if half_day else 1.0


def day_series(start: date, end: date) -> list[date]:
    """Every calendar day from `start` to `end` inclusive (used by the trend builder)."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# --------------------------------------------------------------------------- #
# Query builders - reused by the endpoints AND /admin/explain so both build
# the exact same query/pipeline (contract requirement for the explain output).
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]
EMPLOYEE_SORT = [("emp_code", 1)]


def paginate(page: int, page_size: int) -> tuple[int, int]:
    """(skip, limit) for the 1-based page R10."""
    return (page - 1) * page_size, page_size


def employees_filter(department: Optional[str] = None) -> dict:
    return {"department": department} if department else {}


def attendance_filter(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        span: dict = {}
        if date_from:
            span["$gte"] = date_from.isoformat()
        if date_to:
            span["$lte"] = date_to.isoformat()
        query["date"] = span
    if status:
        query["status"] = status.value
    return query


# --------------------------------------------------------------------------- #
# Aggregation pipelines (analytics) - shared by the endpoints AND /admin/explain
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. They read the STORED derived values (R2-R5);
# nothing here recomputes from punch times (contract: "read the stored values").
_PRESENCE_EXPR = {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}  # R6
_DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
_WEEKDAY_EXPR = {"$in": [_DAY_OF_WEEK, [2, 3, 4, 5, 6]]}  # R7: Mon=2 .. Fri=6
_HALF_EXPR = {"$ifNull": ["$half_day", False]}
_LATE_EXPR = {"$ifNull": ["$late_minutes", 0]}
_OVERTIME_EXPR = {"$ifNull": ["$overtime_minutes", 0]}
_PRESENT_VALUE = {"$cond": [_HALF_EXPR, 0.5, 1]}  # R5: half day = 0.5


def _department_lookup(department: Optional[str] = None) -> list:
    """Join each log to its employee (drops logs with no employee) and optionally one department."""
    stages = [
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        stages.append({"$match": {"emp.department": department}})
    return stages


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    query = {"emp_code": emp_code}
    query.update(month_date_filter(month))
    return [
        {"$match": query},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {"$cond": [{"$and": [_PRESENCE_EXPR, _WEEKDAY_EXPR]}, _PRESENT_VALUE, 0]}
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_EXPR, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": _LATE_EXPR},
                "total_overtime_minutes": {"$sum": _OVERTIME_EXPR},
            }
        },
        {"$project": {"_id": 0}},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    return [
        {"$match": month_date_filter(month)},
        *_department_lookup(department),
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {
                    "$sum": {"$cond": [{"$and": [_PRESENCE_EXPR, _WEEKDAY_EXPR]}, _PRESENT_VALUE, 0]}
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_EXPR, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": _LATE_EXPR},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": [_PRESENCE_EXPR, {"$ne": ["$work_hours", None]}]},
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_n": {
                    "$sum": {
                        "$cond": [{"$and": [_PRESENCE_EXPR, {"$ne": ["$work_hours", None]}]}, 1, 0]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def department_headcount_pipeline(month: str, department: Optional[str] = None) -> list:
    _, last = month_range(month)
    query: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department:
        query["department"] = department
    return [
        {"$match": query},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, department: Optional[str] = None) -> list:
    return [
        {"$match": month_date_filter(month)},
        *_department_lookup(department),
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": _LATE_EXPR},
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_EXPR, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]


def department_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    start = date.fromisoformat(from_str)
    end = date.fromisoformat(to_str)
    # $densify's upper bound is EXCLUSIVE, so ask for one day past `to`.
    upper = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        *_department_lookup(department),
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [_PRESENCE_EXPR, _PRESENT_VALUE, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE_EXPR, 0]}, 1, 0]}},
            }
        },
        {"$project": {"date": "$_id", "present_count": 1, "late_count": 1}},
        {
            "$addFields": {
                "_day": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
            }
        },
        {
            "$densify": {
                "field": "_day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [datetime(start.year, start.month, start.day), upper],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_day"}},
            }
        },
        {"$sort": {"_day": 1}},
        {"$unset": ["_id", "_day"]},
    ]


# --------------------------------------------------------------------------- #
# Serialization (API boundary: BSON datetimes -> epoch ms; never expose _id)
# --------------------------------------------------------------------------- #
def _history_out(entries: Optional[list]) -> list:
    out = []
    for entry in entries or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            old, new = change.get("from"), change.get("to")
            if key in ("punch_in", "punch_out"):
                old, new = to_ms(old), to_ms(new)
            changes[key] = {"from": old, "to": new}
        out.append(
            {
                "at": to_ms(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )
    return out


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainEndpoint(str, Enum):
    """The endpoints /admin/explain can explain (openapi enum)."""

    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%d")
        return value

    @model_validator(mode="after")
    def _shift_must_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    """RegularizeRequest: only these fields are changeable; derived fields are not accepted."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# Query-parameter annotations shared by the endpoints and /admin/explain.
Month = Annotated[str, Query(pattern=MONTH_PATTERN, description="Calendar month YYYY-MM.")]
Limit = Annotated[int, Query(ge=1, le=50)]
PageNumber = Annotated[int, Query(ge=1)]
PageSizeNumber = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# Indexes (created idempotently at startup)
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="emp_code_date_unique"
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp_code")
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_desc_emp_code"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        create_indexes()
    except Exception as exc:  # pragma: no cover - startup resilience
        logger.warning("index creation skipped: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints provided by the starter (fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = employees_filter(department)
    total = db.employees.count_documents(query)
    skip, limit = paginate(page, page_size)
    items = list(db.employees.find(query).sort(EMPLOYEE_SORT).skip(skip).limit(limit))
    return {
        "items": [employee_out(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    skip, limit = paginate(page, page_size)
    items = list(
        db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip(skip).limit(limit)
    )
    return {
        "items": [record_out(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Regularization helpers
# --------------------------------------------------------------------------- #
def _final_state(rec: dict, emp: dict, body: RegularizeIn, date_str: str) -> dict:
    """
    Apply the requested changes to a record and recompute the derived fields (R2-R5).
    Raises 422 for any request-level rule violation.
    """
    final_status = body.status.value if body.status is not None else rec.get("status")

    if final_status in (Status.ABSENT.value, Status.LEAVE.value):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times are not allowed for ABSENT/LEAVE")
        punch_in = punch_out = None
    else:
        punch_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        punch_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if punch_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(punch_in, emp) != date_str:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if punch_out is not None:
            if punch_out <= punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (punch_out - punch_in) > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if punch_in is None:
        late_minutes, work_hours, overtime_minutes, half_day = 0, None, 0, False
    else:
        late_minutes = compute_late_minutes(punch_in, date_str, emp)
        if punch_out is not None:
            work_hours = compute_work_hours(punch_in, punch_out)
            overtime_minutes = compute_overtime_minutes(punch_out, date_str, emp)
            half_day = is_half_day(work_hours)
        else:
            work_hours, overtime_minutes, half_day = None, 0, False

    return {
        "status": final_status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def _changed_fields(rec: dict, final: dict) -> dict:
    """One key per field that actually changed, as {"from", "to"}."""
    current = {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    return {
        key: {"from": current[key], "to": value}
        for key, value in final.items()
        if current[key] != value
    }


# --------------------------------------------------------------------------- #
# Endpoints built for Part B (CRUD)
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in = rec["punch_in"]
    if ts <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in) > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime_minutes(ts, rec["date"], emp),
                "half_day": is_half_day(work_hours),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    # strptime accepts unpadded values like "2026-7-6"; the contract's path date is the
    # record's stored zero-padded YYYY-MM-DD, so reject anything that does not round-trip.
    if parsed_date.strftime("%Y-%m-%d") != date:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for this date")

    final = _final_state(rec, emp, body, date)
    changes = _changed_fields(rec, final)
    if not changes:
        raise HTTPException(422, "nothing changed")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic guard: the stored `history` must still be the one we read. A legacy
    # record may omit `history` entirely, so an empty snapshot accepts either shape.
    current_history = rec.get("history", [])
    guard: dict = {"_id": rec["_id"]}
    if current_history:
        guard["history"] = current_history
    else:
        guard["$or"] = [{"history": []}, {"history": {"$exists": False}}]

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "concurrent modification")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints (Part B) - each backed by its aggregation builder above
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}

    present_days = round_half_up(float(row.get("present_days", 0) or 0), 2)
    working_days = working_days_in_month(month, emp.get("joined_on"))  # R7
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    headcount = {
        d["_id"]: d["headcount"]
        for d in db.employees.aggregate(department_headcount_pipeline(month, department))
    }
    metrics = {
        d["_id"]: d
        for d in db.attendance_logs.aggregate(department_summary_pipeline(month, department))
    }

    items = []
    for dept in sorted(headcount):
        m = metrics.get(dept, {})
        work_sum = float(m.get("work_sum", 0) or 0)
        work_n = int(m.get("work_n", 0) or 0)
        items.append(
            {
                "department": dept,
                "headcount": headcount[dept],
                "present_days": round_half_up(float(m.get("present_days", 0) or 0), 2),
                "avg_work_hours": round_half_up(work_sum / work_n, 2) if work_n else None,
                "late_count": int(m.get("late_count", 0) or 0),
                "total_late_minutes": int(m.get("total_late_minutes", 0) or 0),
                "leave_count": int(m.get("leave_count", 0) or 0),
                "on_duty_count": int(m.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Month,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, department)))

    # Standard competition ranking (1, 2, 2, 4); `limit` filters by rank, not position.
    items = []
    rank = 0
    previous_total = None
    for index, row in enumerate(rows):
        total = int(row["total_late_minutes"])
        if previous_total is None or total != previous_total:
            rank = index + 1
            previous_total = total
        if rank > limit:
            break
        items.append(
            {
                "rank": rank,
                "emp_code": row["_id"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": total,
                "late_count": int(row["late_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: date = Query(..., alias="from"),
    to: date = Query(..., alias="to"),
):
    if to < from_:
        raise HTTPException(422, "to must not be before from")
    if (to - from_).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")

    from_str, to_str = from_.isoformat(), to.isoformat()
    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(department, from_str, to_str)))
    by_date = {r["date"]: r for r in rows}

    joined_on = sorted(
        e["joined_on"] for e in db.employees.find({"department": department}, {"joined_on": 1})
    )

    items = []
    for day in day_series(from_, to):  # exactly one row per calendar day
        day_str = day.isoformat()
        row = by_date.get(day_str, {})
        headcount = bisect_right(joined_on, day_str)  # R9: joined_on <= day
        working = is_working_day(day)
        present_count = float(row.get("present_count", 0) or 0)
        if working and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None
        items.append(
            {
                "date": day_str,
                "is_working_day": working,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": int(row.get("late_count", 0) or 0),
                "attendance_rate": attendance_rate,
            }
        )

    # 7-day moving average of the non-null rates, within the requested range only.
    for index, item in enumerate(items):
        window = [
            items[j]["attendance_rate"]
            for j in range(max(0, index - 6), index + 1)
            if items[j]["attendance_rate"] is not None
        ]
        item["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: raw MongoDB explain for an endpoint's main query/aggregation (Part B)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs `explain` (executionStats verbosity) for the named endpoint's main query or
    aggregation, built exactly as that endpoint builds it (same filters, sort, pagination).
    """

    def required(value, name: str):
        if value is None:
            raise HTTPException(422, f"{name} is required")
        return value

    # Each branch builds the exact same command the endpoint issues.
    if endpoint is ExplainEndpoint.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        query = attendance_filter(emp_code, date_from, date_to, status)
        skip, page_limit = paginate(page, page_size)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_limit,
        }
    elif endpoint is ExplainEndpoint.employee_monthly:
        command = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(
                required(emp_code, "emp_code"), required(month, "month")
            ),
            "cursor": {},
        }
    elif endpoint is ExplainEndpoint.department_summary:
        command = {
            "aggregate": "attendance_logs",
            "pipeline": department_summary_pipeline(required(month, "month"), department),
            "cursor": {},
        }
    elif endpoint is ExplainEndpoint.late_leaderboard:
        command = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(required(month, "month"), department),
            "cursor": {},
        }
    else:  # department_trend
        start = required(from_, "from")
        end = required(to, "to")
        if end < start:
            raise HTTPException(422, "to must not be before from")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, "range must not exceed 92 days")
        command = {
            "aggregate": "attendance_logs",
            "pipeline": department_trend_pipeline(
                required(department, "department"), start.isoformat(), end.isoformat()
            ),
            "cursor": {},
        }

    explain = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint.value, "collection": "attendance_logs", "explain": explain}
