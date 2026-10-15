"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables.
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import Collection
# ============================================================
# Configuration
# ============================================================
# Existing environment variables take precedence over .env.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees: Collection = db["employees"]
attendance: Collection = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)
# ============================================================
# Validation and date/time helpers
# ============================================================
def validate_date(value: str) -> str:
    """Validate an ISO calendar date."""
    try:
        if not isinstance(value, str):
            raise ValueError
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
        return value
    except (TypeError, ValueError):
        raise ValueError("Expected date in YYYY-MM-DD format")
def validate_month(value: str) -> str:
    """Validate a calendar month in YYYY-MM format."""
    if not isinstance(value, str):
        raise ValueError("Expected month in YYYY-MM format")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Expected month in YYYY-MM format")
    return value
def validate_time(value: str) -> str:
    """Validate a 24-hour HH:MM time."""
    if not isinstance(value, str):
        raise ValueError("Expected time in HH:MM format")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Expected time in HH:MM format")
    return value
def truncate_seconds(value: datetime) -> datetime:
    """Truncate an instant to whole seconds."""
    return value.replace(microsecond=0)
def epoch_to_datetime(value: Optional[int]) -> datetime:
    """
    Convert epoch milliseconds to a naive UTC datetime for MongoDB.
    Naive UTC datetimes are used consistently for BSON storage.
    """
    if value is None:
        return datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError(
            "Timestamp must be an integer epoch-millisecond value"
        )
    return datetime.fromtimestamp(
        value / 1000,
        UTC,
    ).replace(microsecond=0, tzinfo=None)
def utc_to_epoch(value: Optional[datetime]) -> Optional[int]:
    """Convert a MongoDB datetime to epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)
def datetime_to_ist(value: datetime) -> datetime:
    """Interpret MongoDB naive datetimes as UTC and convert to IST."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)
def round_half_up(
    value: Optional[float],
    places: int = 2,
) -> Optional[float]:
    """Round using decimal ROUND_HALF_UP rather than banker's rounding."""
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )
def shift_start_end(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> tuple[datetime, datetime]:
    """Return the IST start and end instants for a shift."""
    day = date.fromisoformat(attendance_date)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    start = datetime.combine(
        day,
        time(start_hour, start_minute),
        IST,
    )
    end_day = (
        day + timedelta(days=1)
        if shift_end <= shift_start
        else day
    )
    end = datetime.combine(
        end_day,
        time(end_hour, end_minute),
        IST,
    )
    return start, end
def get_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """
    R1: Determine the attendance date from the punch-in instant.
    For an overnight shift, a punch-in earlier than shift_end
    belongs to the previous shift-start date.
    """
    local = datetime_to_ist(punch_in)
    attendance_day = local.date()
    if (
        shift_end <= shift_start
        and local.strftime("%H:%M") < shift_end
    ):
        attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()
def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """
    R2: Count whole minutes after shift start, but only when
    the delay is strictly greater than ten minutes.
    """
    shift_day = date.fromisoformat(attendance_date)
    hour, minute = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(
        shift_day,
        time(hour, minute),
        IST,
    )
    local_punch_in = datetime_to_ist(punch_in)
    delay_seconds = (
        local_punch_in - shift_start_dt
    ).total_seconds()
    if delay_seconds <= 600:
        return 0
    return int(delay_seconds // 60)
def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """R4: Calculate elapsed work hours, rounded half-up to 2 decimals."""
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0:
        raise ValueError("punch_out must be after punch_in")
    return round_half_up(seconds / 3600, 2)
def compute_overtime(
    punch_out: datetime,
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> int:
    """
    R3: Count whole minutes after the scheduled shift end.
    Overtime is zero unless the elapsed time is at least 30 minutes.
    """
    _, shift_end_dt = shift_start_end(
        attendance_date,
        shift_start,
        shift_end,
    )
    shift_end_utc = (
        shift_end_dt.astimezone(UTC)
        .replace(tzinfo=None)
    )
    overtime_seconds = (
        punch_out - shift_end_utc
    ).total_seconds()
    if overtime_seconds < 1800:
        return 0
    return int(overtime_seconds // 60)
# ============================================================
# API response serializers
# ============================================================
def serialize_employee(document: dict) -> dict:
    """Remove MongoDB _id and convert created_at to epoch milliseconds."""
    result = {
        key: value
        for key, value in document.items()
        if key != "_id"
    }
    if "created_at" in result:
        result["created_at"] = utc_to_epoch(
            result["created_at"]
        )
    return result
def serialize_attendance(document: dict) -> dict:
    """
    Return the exact attendance response fields.
    Attendance records have no public id; their natural key is
    (emp_code, date).
    """
    history = []
    for entry in document.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            old_value = change.get("from")
            new_value = change.get("to")
            if field in {"punch_in", "punch_out"}:
                old_value = utc_to_epoch(old_value)
                new_value = utc_to_epoch(new_value)
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }
        history.append({
            "at": utc_to_epoch(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes,
        })
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": utc_to_epoch(document.get("punch_in")),
        "punch_out": utc_to_epoch(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": int(document.get("late_minutes", 0) or 0),
        "overtime_minutes": int(
            document.get("overtime_minutes", 0) or 0
        ),
        "half_day": bool(document.get("half_day", False)),
        "history": history,
    }
# ============================================================
# Request models
# ============================================================
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.****[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift_time(cls, value):
        return validate_time(value)
    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        return validate_date(value)
    @model_validator(mode="after")
    def check_shift_values(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self
class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            if (
                type(value) is not int
                or not EPOCH_MIN <= value <= EPOCH_MAX
            ):
                raise ValueError(
                    "punched_at must be integer epoch milliseconds"
                )
        return value
class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            if (
                type(value) is not int
                or not EPOCH_MIN <= value <= EPOCH_MAX
            ):
                raise ValueError(
                    "punched_at must be integer epoch milliseconds"
                )
        return value
class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            if (
                type(value) is not int
                or not EPOCH_MIN <= value <= EPOCH_MAX
            ):
                raise ValueError(
                    "Timestamp must be integer epoch milliseconds"
                )
        return value
# ============================================================
# Indexes
# ============================================================
@app.on_event("startup")
def create_indexes():
    """Create indexes idempotently at application startup."""
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined",
    )
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    attendance.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    attendance.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="ix_attendance_emp_date_status",
    )
    attendance.create_index(
        [("status", ASCENDING), ("date", ASCENDING)],
        name="ix_attendance_status_date",
    )
# ============================================================
# Health
# ============================================================
@app.get("/health")
def health():
    """Return 200 only if MongoDB responds to a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )
# ============================================================
# Employees
# ============================================================
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee; the unique index handles concurrent duplicates."""
    document = body.model_dump()
    document["created_at"] = (
        datetime.now(UTC)
        .replace(microsecond=0, tzinfo=None)
    )
    try:
        employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    return serialize_employee(document)
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees, sorted by emp_code, with filtered totals."""
    query = {}
    if department is not None:
        query["department"] = department
    total = employees.count_documents(query)
    documents = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [
            serialize_employee(document)
            for document in documents
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
# ============================================================
# Attendance punch-in
# ============================================================
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """Create one attendance record for the employee's shift date."""
    employee = employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0},
    )
    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )
    punched_at = epoch_to_datetime(body.punched_at)
    attendance_day = get_attendance_date(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
    )
    late_minutes = compute_late_minutes(
        punched_at,
        employee["shift_start"],
        attendance_day,
    )
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )
    return serialize_attendance(document)
# ============================================================
# Attendance punch-out
# ============================================================
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """
    Close the latest punch-in at or before punched_at.
    Conditional update makes simultaneous punch-outs race-safe:
    only one request can update a record whose punch_out is null.
    """
    employee = employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0},
    )
    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )
    punched_out = epoch_to_datetime(body.punched_at)
    document = attendance.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_out},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not document:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found before punch-out",
        )
    if document.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="This record is already punched out",
        )
    elapsed_seconds = (
        punched_out - document["punch_in"]
    ).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in and within 24 hours",
        )
    work_hours = compute_work_hours(
        document["punch_in"],
        punched_out,
    )
    overtime_minutes = compute_overtime(
        punched_out,
        document["date"],
        employee["shift_start"],
        employee["shift_end"],
    )
    half_day = work_hours < 4.50
    result = attendance.update_one(
        {
            "_id": document["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="This record was punched out concurrently",
        )
    document.update({
        "punch_out": punched_out,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    })
    return serialize_attendance(document)
# ============================================================
# Attendance list
# ============================================================
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Filter and paginate attendance in MongoDB, not in Python."""
    if date_from is not None:
        validate_date(date_from)
    if date_to is not None:
        validate_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be less than or equal to date_to",
        )
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = attendance.count_documents(query)
    documents = (
        attendance.find(query, {"_id": 0})
        .sort([
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [
            serialize_attendance(document)
            for document in documents
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
# ============================================================
# Attendance regularization (manual correction)
# ============================================================
@app.patch("/attendance/{emp_code}/{attendance_day}")
def regularize_attendance(
    emp_code: str,
    attendance_day: str,
    body: RegularizeIn,
):
    """Correct an attendance record and append one audit entry."""
    try:
        validate_date(attendance_day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    old = attendance.find_one({
        "emp_code": emp_code,
        "date": attendance_day,
    })
    if not old:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )
    changes_requested = body.model_dump(exclude_unset=True)
    new_status = changes_requested.get("status", old["status"])
    if new_status in {"ABSENT", "LEAVE"}:
        if (
            changes_requested.get("punch_in") is not None
            or changes_requested.get("punch_out") is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT/LEAVE cannot have punch times",
            )
        new_in = None
        new_out = None
    else:
        new_in = (
            epoch_to_datetime(changes_requested["punch_in"])
            if "punch_in" in changes_requested
            else old.get("punch_in")
        )
        new_out = (
            epoch_to_datetime(changes_requested["punch_out"])
            if "punch_out" in changes_requested
            else old.get("punch_out")
        )
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )
        local_in = datetime_to_ist(new_in)
        valid_attendance_date = local_in.date().isoformat()
        if (
            employee["shift_end"] <= employee["shift_start"]
            and local_in.strftime("%H:%M") < employee["shift_end"]
        ):
            valid_attendance_date = (
                local_in.date() - timedelta(days=1)
            ).isoformat()
        if valid_attendance_date != attendance_day:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
    if new_status in {"ABSENT", "LEAVE"}:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            new_in,
            employee["shift_start"],
            attendance_day,
        )
        if new_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(new_in, new_out)
            overtime_minutes = compute_overtime(
                new_out,
                attendance_day,
                employee["shift_start"],
                employee["shift_end"],
            )
            half_day = work_hours < 4.50
    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    # Record only values that actually changed.
    actual_changes = {}
    for field, new_value in proposed.items():
        old_value = old.get(field)
        if old_value != new_value:
            actual_changes[field] = {
                "from": old_value,
                "to": new_value,
            }
    if not actual_changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change the record",
        )
    history_entry = {
        "at": datetime.now(UTC).replace(
            microsecond=0,
            tzinfo=None,
        ),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }
    # Compare-and-swap: if the document changed since it was read,
    # this update fails instead of losing another correction.
    condition = {
        "_id": old["_id"],
        "history": old.get("history", []),
    }
    for field in proposed:
        if field in old:
            condition[field] = old[field]
        else:
            condition[field] = {"$exists": False}
    result = attendance.update_one(
        condition,
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Record changed concurrently; retry the correction",
        )
    old.update(proposed)
    old.setdefault("history", []).append(history_entry)
    return serialize_attendance(old)
# ============================================================
# Analytics helpers
# ============================================================
def month_bounds(month: str) -> tuple[str, str]:
    """Return inclusive first and last dates for YYYY-MM."""
    validate_month(month)
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return (
        f"{year:04d}-{month_number:02d}-01",
        f"{year:04d}-{month_number:02d}-{last_day:02d}",
    )
def count_weekdays(start: date, end: date) -> int:
    """Count Monday-Friday dates, inclusive."""
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total
def analytics_round(value: Any, places: int = 2) -> float:
    """Return a number rounded using ROUND_HALF_UP."""
    return round_half_up(float(value or 0), places)
def attendance_metrics_pipeline(match: dict) -> list[dict]:
    """Aggregate attendance metrics inside MongoDB."""
    return [
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "total_logs": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            1,
                            0,
                        ]
                    }
                },
                "absent_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_days": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "work_hours": {
                    "$sum": {"$ifNull": ["$work_hours", 0]}
                },
                "half_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$half_day", True]}, 1, 0]
                    }
                },
            }
        },
    ]
def aggregate_metrics(match: dict) -> dict:
    """Return zero-valued metrics when no attendance rows match."""
    result = list(attendance.aggregate(attendance_metrics_pipeline(match)))
    if not result:
        return {
            "total_logs": 0,
            "present_days": 0,
            "absent_days": 0,
            "leave_days": 0,
            "late_days": 0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": 0.0,
            "half_days": 0,
        }
    row = result[0]
    row.pop("_id", None)
    row["work_hours"] = analytics_round(row.get("work_hours", 0))
    return row
# ============================================================
# Employee monthly analytics
# ============================================================
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance metrics for one employee."""
    try:
        start_day, end_day = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    joined = max(date.fromisoformat(start_day), date.fromisoformat(employee["joined_on"]))
    end = date.fromisoformat(end_day)
    working_days = count_weekdays(joined, end) if joined <= end else 0
    metrics = aggregate_metrics({
        "emp_code": emp_code,
        "date": {"$gte": start_day, "$lte": end_day},
    })
    presence_days = metrics["present_days"]
    attendance_rate = (
        presence_days / working_days if working_days else 0.0
    )
    return {
        "emp_code": emp_code,
        "name": employee["name"],
        "department": employee["department"],
        "month": month,
        "working_days": working_days,
        **metrics,
        "attendance_rate": analytics_round(attendance_rate, 4),
    }
# ============================================================
# Department monthly summary
# ============================================================
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Monthly per-department metrics, including employees with no logs."""
    try:
        start_day, end_day = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if department is not None and not employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    employee_match = {"joined_on": {"$lte": end_day}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start_day]},
                    {"$lte": ["$date", end_day]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]}, "$logs.work_hours", 0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]}, 1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0,
            ]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$set": {"avg_work_hours": {"$cond": [
            {"$gt": ["$work_hours_count", 0]},
            {"$divide": ["$work_hours_sum", "$work_hours_count"]},
            None,
        ]}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    try:
        items = list(employees.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Department summary failed: {exc}")
    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = analytics_round(item["avg_work_hours"], 2)
        item["present_days"] = analytics_round(item.get("present_days", 0), 2)
    return {"month": month, "items": items}


# ============================================================
# Late-arrival leaderboard
# ============================================================
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    """Rank employees by late-arrival days using MongoDB $rank."""
    try:
        start_day, end_day = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_day, "$lte": end_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "late_days": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
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
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"late_days": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$sort": {"rank": 1, "_id": 1}},
        {"$limit": limit},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "late_days": 1,
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ])
    return {
        "month": month,
        "department": department,
        "items": list(attendance.aggregate(pipeline)),
    }
# ============================================================
# Department attendance trend
# ============================================================
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Return one MongoDB-computed trend row per date, including gaps."""
    try:
        validate_date(from_date)
        validate_date(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    first = date.fromisoformat(from_date)
    last = date.fromisoformat(to_date)
    if first > last:
        raise HTTPException(status_code=422, detail="from must be <= to")
    if (last - first).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    if not employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(first, time.min)
    end_dt = datetime.combine(last + timedelta(days=1), time.min)
    pipeline = [
        {"$documents": [{"start": start_dt, "end": end_dt}]},
        {"$project": {"dates": {"$map": {
            "input": {"$range": [0, {"$toInt": {"$divide": [
                {"$subtract": ["$end", "$start"]}, 86400000
            ]}}]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$dates"},
        {"$project": {"_id": 0, "date": {"$dateToString": {
            "format": "%Y-%m-%d", "date": "$dates"
        }}}},
        {"$set": {"is_working_day": {"$in": [
            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 2, 3, 4, 5]
        ]}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees", "localField": "emp_code",
                    "foreignField": "emp_code", "as": "employee",
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                    ]}},
                }},
            ],
            "as": "daily",
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"date": 1}},
    ]
    try:
        rows = list(db.command({
            "aggregate": 1, "pipeline": pipeline, "cursor": {}
        })["cursor"]["firstBatch"])
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Trend aggregation failed: {exc}")
    for row in rows:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = analytics_round(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = analytics_round(row["moving_avg_7d"], 4)
        row["present_count"] = analytics_round(row.get("present_count", 0), 2)
    return {"department": department, "from": from_date, "to": to_date, "items": rows}


# ============================================================
# Query execution plans
# ============================================================
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    """Return MongoDB execution statistics for supported query patterns."""
    supported = {
        "attendance_list": {
            "collection": "attendance_logs",
            "filter": {},
            "sort": {"date": -1, "emp_code": 1},
        },
        "employee_monthly": {
            "collection": "attendance_logs",
            "filter": {
                "emp_code": {"$exists": True},
                "date": {"$gte": "2026-01-01", "$lte": "2026-01-31"},
            },
            "sort": {"date": 1},
        },
        "department_summary": {
            "collection": "attendance_logs",
            "filter": {
                "date": {"$gte": "2026-01-01", "$lte": "2026-01-31"},
            },
            "sort": {"date": 1},
        },
        "late_leaderboard": {
            "collection": "attendance_logs",
            "filter": {
                "date": {"$gte": "2026-01-01", "$lte": "2026-01-31"},
                "late_minutes": {"$gt": 0},
            },
            "sort": {"date": 1},
        },
        "department_trend": {
            "collection": "attendance_logs",
            "filter": {
                "date": {"$gte": "2026-01-01", "$lte": "2026-01-31"},
            },
            "sort": {"date": 1},
        },
    }
    if endpoint not in supported:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Unsupported endpoint identifier",
                "supported": list(supported.keys()),
            },
        )
    spec = supported[endpoint]
    collection = db[spec["collection"]]
    try:
        explanation = collection.find(
            spec["filter"]
        ).sort(
            list(spec["sort"].items())
        ).explain()
    except PyMongoError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not explain query: {exc}",
        )
    execution = explanation.get("executionStats", {})
    winning_plan = explanation.get("queryPlanner", {}).get(
        "winningPlan", {}
    )
    def find_stages(plan):
        """Collect stage names from nested explain-plan documents."""
        stages = []
        if isinstance(plan, dict):
            if "stage" in plan:
                stages.append(plan["stage"])
            for value in plan.values():
                stages.extend(find_stages(value))
        elif isinstance(plan, list):
            for item in plan:
                stages.extend(find_stages(item))
        return stages
    stages = find_stages(winning_plan)
    return {
        "endpoint": endpoint,
        "collection": spec["collection"],
        "filter": spec["filter"],
        "sort": spec["sort"],
        "executionStats": {
            "executionSuccess": execution.get("executionSuccess"),
            "nReturned": execution.get("nReturned"),
            "totalKeysExamined": execution.get("totalKeysExamined"),
            "totalDocsExamined": execution.get("totalDocsExamined"),
            "executionTimeMillis": execution.get("executionTimeMillis"),
        },
        "winningPlan": winning_plan,
        "stages": stages,
        "uses_index": any(
            stage in {"IXSCAN", "DISTINCT_SCAN"}
            for stage in stages
        ),
        "has_collection_scan": "COLLSCAN" in stages,
    }
