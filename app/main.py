"""
Employee Attendance & Analytics API — HROne assignment.

Run from the candidate_kit directory:
    python -m uvicorn app.main:app --port 8000

Configuration:
    MONGO_URI and MONGO_DB must be supplied in the environment or .env.
    Existing process environment variables take precedence over .env values.
"""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured in the environment or .env file.")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes and common helpers
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """Create indexes idempotently. Unique indexes enforce concurrency safety."""
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="IX_EMPLOYEES_EMP_CODE"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="IX_EMPLOYEES_DEPARTMENT_EMP_CODE",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="IX_EMPLOYEES_DEPARTMENT_JOINED_ON",
    )
    # Supports department_summary's joined_on range when no department is supplied.
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="IX_EMPLOYEES_JOINED_ON",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="IX_ATTENDANCE_EMP_CODE_DATE",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="IX_ATTENDANCE_DATE_EMP_CODE",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="IX_ATTENDANCE_STATUS_DATE",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="IX_ATTENDANCE_EMP_PUNCH_IN",
    )


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def epoch_ms_to_datetime(value: int) -> datetime:
    # Pydantic rejects floats and strings; keep the range check here as a guard.
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail="Timestamp must be an integer epoch millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail="Timestamp is outside the supported epoch-millisecond range")
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid timestamp")
    # Truncate to whole seconds before all calculations and storage.
    return dt.replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp() * 1000)


def utc_now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ist_datetime(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    shift_end_le_start = shift_end <= shift_start
    if shift_end_le_start and local.strftime("%H:%M") < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_day: Optional[date] = None,
) -> int:
    local = punch_in.astimezone(IST)

    # Use the attendance date to identify the correct shift start.
    shift_day = attendance_day or local.date()
    shift_start_dt = ist_datetime(shift_day, shift_start)

    elapsed_seconds = int((local - shift_start_dt).total_seconds())

    # The first 10 minutes are a grace period.
    if elapsed_seconds <= 10 * 60:
        return 0

    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: Optional[datetime], shift_end: str, attendance_day: date, shift_start: str) -> int:
    if punch_out is None:
        return 0
    end_day = attendance_day + timedelta(days=1) if shift_end <= shift_start else attendance_day
    shift_end_dt = ist_datetime(end_day, shift_end).astimezone(UTC)
    elapsed_seconds = int((punch_out - shift_end_dt).total_seconds())
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def compute_derived(record: dict, employee: dict) -> dict:
    """Return computed fields from final record timestamps and employee shift."""
    status = record.get("status")
    pin = record.get("punch_in")
    pout = record.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        pin = pout = None
    late = 0
    work_hours = None
    overtime = 0
    half_day = False
    if pin is not None:
        late = compute_late_minutes(
            pin,
            employee["shift_start"],
            parse_date(record["date"]),
        )
    if pin is not None and pout is not None:
        work_hours = compute_work_hours(pin, pout)
        overtime = compute_overtime(
            pout, employee["shift_end"], parse_date(record["date"]), employee["shift_start"]
        )
        half_day = work_hours < 4.50
    return {
        "punch_in": pin,
        "punch_out": pout,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }


def serialize_history_entry(entry: dict) -> dict:
    result = {
        "at": datetime_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {},
    }
    for field, change in entry.get("changes", {}).items():
        def convert(v):
            if field in ("punch_in", "punch_out"):
                return datetime_to_epoch_ms(v)
            return v
        result["changes"][field] = {
            "from": convert(change.get("from")),
            "to": convert(change.get("to")),
        }
    return result


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def working_days(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def present_fraction(status: str, half_day: bool = False) -> float:
    if status not in PRESENT_STATUSES:
        return 0.0
    return 0.5 if half_day else 1.0


def validate_pagination(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")


def safe_explain(cursor_or_pipeline, collection, pipeline=None):
    if pipeline is None:
        # Query explain, preserving sort/skip/limit where supported by the find command.
        return collection.database.command(
            "explain",
            {
                "find": collection.name,
                "filter": cursor_or_pipeline["filter"],
                "sort": cursor_or_pipeline.get("sort", {}),
                "skip": cursor_or_pipeline.get("skip", 0),
                "limit": cursor_or_pipeline.get("limit", 0),
            },
            verbosity="executionStats",
        )
    return collection.database.command(
        "explain",
        {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    validate_pagination(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size
    ).limit(page_size)
    return {
        "items": [
            {
                "emp_code": d["emp_code"], "name": d["name"], "email": d["email"],
                "department": d["department"], "shift_start": d["shift_start"],
                "shift_end": d["shift_end"], "joined_on": d["joined_on"],
                "created_at": datetime_to_epoch_ms(d["created_at"]),
            }
            for d in docs
        ],
        "total": total, "page": page, "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance CRUD
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    punch_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now_truncated()
    local = punch_time.astimezone(IST)
    attendance_day = attendance_date_for_punch(
        punch_time, employee["shift_start"], employee["shift_end"]
    )
    record = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punch_time,
            employee["shift_start"],
            attendance_day,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this attendance date")
    return serialize_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    punch_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now_truncated()

    # Find the most recent eligible punch-in. The update predicate makes concurrent punch-outs race-safe.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_time}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found at or before this time")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="This attendance record is already punched out")
    elapsed = (punch_time - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")

    derived = compute_derived({**record, "punch_out": punch_time}, employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_time, **{k: derived[k] for k in (
            "work_hours", "late_minutes", "overtime_minutes", "half_day"
        )}}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="This attendance record was punched out concurrently")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    validate_pagination(page, page_size)
    if date_from is not None:
        parse_date(date_from)
    if date_to is not None:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_attendance(d) for d in docs],
        "total": total, "page": page, "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    target_day = parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": target_day.isoformat()})
    if existing is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)
    final = dict(existing)
    final.update({
        k: epoch_ms_to_datetime(v) if k in ("punch_in", "punch_out") and v is not None else v
        for k, v in requested.items()
    })

    if final.get("status") in ("ABSENT", "LEAVE"):
        if ("punch_in" in requested and requested["punch_in"] is not None) or (
            "punch_out" in requested and requested["punch_out"] is not None
        ):
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")
        final["punch_in"] = None
        final["punch_out"] = None
    elif final.get("punch_in") is None:
        raise HTTPException(status_code=422, detail="Presence statuses require punch_in")

    if final.get("punch_in") is not None:
        calculated_day = attendance_date_for_punch(
            final["punch_in"], employee["shift_start"], employee["shift_end"]
        )
        if calculated_day != target_day:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
    if final.get("punch_in") is not None and final.get("punch_out") is not None:
        seconds = (final["punch_out"] - final["punch_in"]).total_seconds()
        if seconds <= 0 or seconds > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    derived = compute_derived(final, employee)
    changes = {}
    compare_fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    before = {**existing}
    after = {**final, **derived}
    for field in compare_fields:
        old, new = before.get(field), after.get(field)
        if isinstance(old, datetime):
            old = old.replace(microsecond=0)
        if isinstance(new, datetime):
            new = new.replace(microsecond=0)
        if old != new:
            changes[field] = {"from": before.get(field), "to": after.get(field)}
    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change the record")

    history_entry = {
        "at": utc_now_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Compare-and-set on the original history length prevents lost concurrent updates.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": existing["_id"], "history": existing.get("history", [])},
        {"$set": {**derived}, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Attendance record changed concurrently; retry")
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Analytics helpers and endpoints. Calculations are performed in MongoDB.
# ---------------------------------------------------------------------------
def date_bounds_as_strings(month: str) -> tuple[str, str]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat()


def present_days_expression():
    return {
        "$cond": [
            {"$in": ["$status", list(PRESENT_STATUSES)]},
            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
            0.0,
        ]
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown employee")
    month_start, month_end = parse_month(month)
    joined = parse_date(employee["joined_on"])
    effective_start = max(month_start, joined)
    days = working_days(effective_start, month_end) if effective_start <= month_end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0.0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    agg = rows[0] if rows else {}
    present = round_half_up(agg.get("present_days", 0), 2)
    pct = round_half_up(present / days * 100, 2) if days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": days,
        "present_days": present, "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = parse_month(month)
    start_s, end_s = month_start.isoformat(), month_end.isoformat()
    employee_match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start_s]},
                    {"$lte": ["$date", end_s]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$project": {
                    "status": 1, "date": 1, "work_hours": 1,
                    "late_minutes": 1, "overtime_minutes": 1, "half_day": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                ]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]}, 0.0
            ]}},
            "hours": {"$push": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                    {"$ne": ["$logs.work_hours", None]},
                ]}, "$logs.work_hours", None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id",
            "headcount": {"$size": "$headcount_codes"},
            "present_days": 1,
            "avg_work_hours": {"$let": {
                "vars": {"valid_hours": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
                "in": {"$cond": [
                    {"$gt": [{"$size": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, 0]},
                    {"$avg": "$$valid_hours"}, None
                ]},
            }},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = date_bounds_as_strings(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id",
            "name": "$employee.name", "department": "$employee.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)

    if end < start:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Unknown department")

    start_s = start.isoformat()
    end_s = end.isoformat()
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    # MongoDB $densify uses an exclusive upper bound for explicit bounds.
    # Add one day so the requested end date is included even when it has no logs.
    end_exclusive_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_s]},
                                    {"$lte": ["$date", end_s]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {"$unwind": {"path": "$daily_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": {"$ifNull": ["$daily_logs._id", start_s]}
                    }
                },
                "present_count": {"$ifNull": ["$daily_logs.present_count", 0]},
                "late_count": {"$ifNull": ["$daily_logs.late_count", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive_dt],
                },
            }
        },
        {
            "$set": {
                "date": "$_id",
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
        item["present_count"] = round_half_up(item["present_count"], 2)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    """Explain the same query/aggregation used by each production endpoint."""
    validate_pagination(page, page_size)

    if endpoint == "attendance_list":
        if date_from is not None:
            parse_date(date_from)
        if date_to is not None:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be on or before date_to",
            )

        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status

        sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
        explanation = safe_explain(
            {
                "filter": query,
                "sort": dict(sort),
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            db.attendance_logs,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "employee_monthly":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")

        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "joined_on": 1},
        )
        if employee is None:
            raise HTTPException(status_code=404, detail="Unknown employee")

        month_start, month_end = parse_month(month)
        # Keep this aggregation identical to employee_monthly().
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {
                                            "$lte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                6,
                                            ]
                                        },
                                        {
                                            "$gte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                2,
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        explanation = safe_explain(
            pipeline,
            db.attendance_logs,
            pipeline=pipeline,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        month_start, month_end = parse_month(month)
        start_s, end_s = month_start.isoformat(), month_end.isoformat()
        employee_match = {"joined_on": {"$lte": end_s}}
        if department is not None:
            employee_match["department"] = department

        # Keep this aggregation identical to department_summary().
        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code", "joined": "$joined_on"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start_s]},
                                        {"$lte": ["$date", end_s]},
                                        {"$gte": ["$date", "$$joined"]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "status": 1,
                                "date": 1,
                                "work_hours": 1,
                                "late_minutes": 1,
                                "overtime_minutes": 1,
                                "half_day": 1,
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "headcount_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                        {
                                            "$lte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                6,
                                            ]
                                        },
                                        {
                                            "$gte": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                2,
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "hours": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$headcount_codes"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$let": {
                            "vars": {
                                "valid_hours": {
                                    "$filter": {
                                        "input": "$hours",
                                        "as": "h",
                                        "cond": {"$ne": ["$$h", None]},
                                    }
                                }
                            },
                            "in": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {
                                                "$size": {
                                                    "$filter": {
                                                        "input": "$hours",
                                                        "as": "h",
                                                        "cond": {"$ne": ["$$h", None]},
                                                    }
                                                }
                                            },
                                            0,
                                        ]
                                    },
                                    {"$avg": "$$valid_hours"},
                                    None,
                                ]
                            },
                        }
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$match": {"headcount": {"$gt": 0}}},
            {"$sort": {"department": 1}},
        ]
        explanation = safe_explain(
            pipeline,
            db.employees,
            pipeline=pipeline,
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explanation,
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        start, end = date_bounds_as_strings(month)
        # Keep this aggregation identical to late_leaderboard().
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start, "$lte": end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )
        explanation = safe_explain(
            pipeline,
            db.attendance_logs,
            pipeline=pipeline,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )

        start_day, end_day = parse_date(from_date), parse_date(to_date)
        if end_day < start_day:
            raise HTTPException(
                status_code=422,
                detail="'to' must be on or after 'from'",
            )
        if (end_day - start_day).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(status_code=404, detail="Unknown department")

        start_s = start_day.isoformat()
        end_s = end_day.isoformat()
        start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
        # $densify's explicit upper bound is exclusive; include the requested end day.
        end_exclusive_dt = datetime.combine(
            end_day + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )

        # Keep this aggregation identical to department_trend().
        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start_s]},
                                        {"$lte": ["$date", end_s]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": "$date",
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$in": ["$status", list(PRESENT_STATUSES)]},
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1.0,
                                                ]
                                            },
                                            0.0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": ["$late_minutes", 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "daily_logs",
                }
            },
            {"$unwind": {"path": "$daily_logs", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateFromString": {
                            "dateString": {"$ifNull": ["$daily_logs._id", start_s]}
                        }
                    },
                    "present_count": {"$ifNull": ["$daily_logs.present_count", 0]},
                    "late_count": {"$ifNull": ["$daily_logs.late_count", 0]},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"},
                }
            },
            {
                "$densify": {
                    "field": "_id",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_exclusive_dt],
                    },
                }
            },
            {
                "$set": {
                    "date": "$_id",
                    "date_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_id",
                            "timezone": "UTC",
                        }
                    },
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                        ]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$date_string"},
                    "pipeline": [
                        {
                            "$match": {
                                "department": department,
                                "$expr": {"$lte": ["$joined_on", "$$day"]},
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$set": {
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                    "present_count": {"$ifNull": ["$present_count", 0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$date_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]
        explanation = safe_explain(
            pipeline,
            db.employees,
            pipeline=pipeline,
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explanation,
        }

    raise HTTPException(status_code=422, detail="Unsupported endpoint")
