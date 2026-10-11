"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from contextlib import asynccontextmanager
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

MS_MIN = 100_000_000_000
MS_MAX = 4_102_444_800_000

def create_indexes():
    """Create all required indexes idempotently at startup."""
    # unique employee lookup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, background=True)
    # unique one-record-per-day constraint + analytics lookup
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, background=True
    )
    # attendance list sort: date desc, emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], background=True
    )
    # punch-out: find most recent punch_in <= instant
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)], background=True
    )
    # department filter on employees
    db.employees.create_index([("department", ASCENDING)], background=True)
    # joined_on for headcount queries
    db.employees.create_index([("joined_on", ASCENDING)], background=True)
    # attendance logs by date range + status for analytics
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)], background=True
    )


@asynccontextmanager
async def lifespan(application):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def validate_ms(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError("timestamp must be an integer, not bool")
    if not isinstance(v, int):
        raise ValueError("timestamp must be an integer (epoch milliseconds)")
    if v < MS_MIN or v > MS_MAX:
        raise ValueError(f"timestamp out of range [{MS_MIN}, {MS_MAX}]")
    return v


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch ms. None-safe."""
    if dt is None:
        return None
    return int(dt.timestamp()) * 1000


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def half_up(value: float, places: int) -> float:
    """Decimal half-up rounding."""
    q = Decimal(10) ** -places
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def attendance_date_for_punch(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: IST calendar date for a punch-in.
    Overnight shift (shift_end <= shift_start): punch-in whose IST time is
    earlier than shift_end belongs to the previous calendar day.
    """
    ist = to_ist(punch_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        if (ist.hour * 60 + ist.minute) < (eh * 60 + em):
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: late only if strictly > 10 min after shift_start.
    Returns whole minutes elapsed since shift_start (not since grace end).
    """
    h, m = map(int, shift_start.split(":"))
    att_d = date_type.fromisoformat(att_date)
    shift_start_ist = datetime(att_d.year, att_d.month, att_d.day, h, m, 0, tzinfo=IST)
    elapsed_seconds = (punch_in_utc - shift_start_ist).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: elapsed seconds / 3600, half-up to 2 decimals."""
    return half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    """
    R3: whole minutes between shift_end and punch_out, only if >= 30; else 0.
    Handles overnight shifts (shift_end on next calendar day).
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att_d = date_type.fromisoformat(att_date)
    end_date = att_d + timedelta(days=1) if (eh * 60 + em) <= (sh * 60 + sm) else att_d
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    elapsed = int((punch_out - shift_end_ist).total_seconds() // 60)
    return elapsed if elapsed >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day when rounded work_hours < 4.50."""
    return work_hours < 4.50


def serialize_record(doc: dict) -> dict:
    """Convert MongoDB attendance_log doc to API response shape."""
    def _ts(v):
        return dt_to_ms(v) if isinstance(v, datetime) else v

    history = doc.get("history") or []
    out_history = []
    for entry in history:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            changes[field] = {"from": _ts(change.get("from")), "to": _ts(change.get("to"))}
        out_history.append({
            "at": _ts(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ts(doc.get("punch_in")),
        "punch_out": _ts(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": out_history,
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
        "created_at": dt_to_ms(doc["created_at"]),
    }


def month_bounds(month: str):
    """Return (first_day_str, last_day_str) for YYYY-MM."""
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    first = date_type(y, m, 1)
    last = date_type(y + 1, 1, 1) - timedelta(days=1) if m == 12 else date_type(y, m + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days between start and end inclusive."""
    start = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = {"extra": "forbid"}
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def val_emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def val_name(cls, v):
        if not 1 <= len(v) <= 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def val_email(cls, v):
        if len(v) > 120 or not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def val_dept(cls, v):
        if not 1 <= len(v) <= 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def val_shift(cls, v):
        if not SHIFT_RE.match(v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def val_joined(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on is not a valid date")
        return v

    @model_validator(mode="after")
    def val_shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = {"extra": "forbid"}
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        return None if v is None else validate_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v not in PRESENCE:
            raise ValueError(f"status must be one of {sorted(PRESENCE)}")
        return v


class PunchOutRequest(BaseModel):
    model_config = {"extra": "forbid"}
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        return None if v is None else validate_ms(v)


class RegularizeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_ts(cls, v):
        return None if v is None else validate_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("reason")
    @classmethod
    def val_reason(cls, v):
        if not 5 <= len(v) <= 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def val_reg_by(cls, v):
        if not 1 <= len(v) <= 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = [
        serialize_employee(d)
        for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    now_ms = int(datetime.now(tz=timezone.utc).timestamp()) * 1000
    punch_utc = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms)
    att_date = attendance_date_for_punch(punch_utc, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    now_ms = int(datetime.now(tz=timezone.utc).timestamp()) * 1000
    punch_utc = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms)

    # Most recent open record for this employee (punch_out still null)
    # We do NOT filter by punch_in <= punch_utc here so that a punch_out
    # timestamp earlier than punch_in returns 422 rather than 404.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$exists": True}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no matching punch-in record found")

    pin = record["punch_in"]
    diff = (punch_utc - pin).total_seconds()
    if diff <= 0:
        raise HTTPException(422, "punch_out must be strictly after punch_in")
    if diff > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    wh = compute_work_hours(pin, punch_utc)
    ot = compute_overtime(punch_utc, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)

    # Atomic: only update if punch_out is still null (concurrency guard)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_utc, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "record already punched out")
    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and not DATE_RE.match(date_from):
        raise HTTPException(422, "date_from must be YYYY-MM-DD")
    if date_to and not DATE_RE.match(date_to):
        raise HTTPException(422, "date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be later than date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = db.attendance_logs.find(q, {"_id": 0}).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)

    return {"items": [serialize_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    if not DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    # ABSENT/LEAVE: reject punch times in request; clear existing ones
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times must not be supplied for ABSENT or LEAVE status")
        new_pin = None
        new_pout = None
    else:
        # Presence: punch_in required
        new_pin = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        if new_pin is None:
            raise HTTPException(422, "punch_in is required for presence status")

        # R1: punch_in must stay on the record's attendance date
        if attendance_date_for_punch(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not fall on the record's attendance date")

        new_pout = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if new_pout is not None:
            diff = (new_pout - new_pin).total_seconds()
            if diff <= 0:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if diff > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields from final state
    if new_status in ("ABSENT", "LEAVE") or new_pin is None:
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_pin, emp["shift_start"], date)
        if new_pout is not None:
            new_wh = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime(new_pout, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    def _ts(v):
        return dt_to_ms(v) if isinstance(v, datetime) else v

    old_pin = record.get("punch_in")
    old_pout = record.get("punch_out")
    old_wh = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)
    old_status = record["status"]

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pin != old_pin:
        changes["punch_in"] = {"from": _ts(old_pin), "to": _ts(new_pin)}
    if new_pout != old_pout:
        changes["punch_out"] = {"from": _ts(old_pout), "to": _ts(new_pout)}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, "no changes detected")

    # Store punch timestamps as BSON datetimes inside history.changes
    def _to_bson_ts(k, v):
        if k in ("punch_in", "punch_out") and v is not None:
            return ms_to_dt(v)
        return v

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            k: {"from": _to_bson_ts(k, v["from"]), "to": _to_bson_ts(k, v["to"])}
            for k, v in changes.items()
        },
    }

    # Atomic conditional update: match on history length to detect concurrent edits
    history_len = len(record.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "concurrent edit detected, please retry")
    return serialize_record(result)


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first_str, last_str = month_bounds(month)
    wd_start = max(emp["joined_on"], first_str)
    working_days = count_working_days(wd_start, last_str) if wd_start <= last_str else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "records": {
                    "$push": {
                        "status": "$status",
                        "date": "$date",
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            }
        },
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))

    present_days = 0.0
    leave_days = late_count = total_late = total_ot = 0

    if rows:
        r = rows[0]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
        for rec in r["records"]:
            if rec["status"] in PRESENCE:
                d = date_type.fromisoformat(rec["date"])
                if d.weekday() < 5:  # Mon-Fri only for present_days (R7)
                    present_days += 0.5 if rec["half_day"] else 1.0

    att_pct = None if working_days == 0 else half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_str, last_str = month_bounds(month)

    emp_match: dict = {"joined_on": {"$lte": last_str}}
    if department:
        emp_match["department"] = department

    # Step 1: headcount per department (employees joined on or before month end)
    hc_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "emp_codes": {"$addToSet": "$emp_code"}}},
    ]
    hc_by_dept = {r["_id"]: set(r["emp_codes"]) for r in db.employees.aggregate(hc_pipeline)}

    if not hc_by_dept:
        return {"month": month, "items": []}

    # Step 2: fetch all relevant logs grouped by emp_code
    all_emp_codes = [ec for codes in hc_by_dept.values() for ec in codes]
    logs_pipeline = [
        {"$match": {"emp_code": {"$in": all_emp_codes}, "date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$group": {
                "_id": "$emp_code",
                "logs": {
                    "$push": {
                        "date": "$date",
                        "status": "$status",
                        "half_day": {"$ifNull": ["$half_day", False]},
                        "work_hours": "$work_hours",
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    }
                },
            }
        },
    ]
    logs_by_emp = {r["_id"]: r["logs"] for r in db.attendance_logs.aggregate(logs_pipeline)}

    dept_list = [department] if department else sorted(hc_by_dept.keys())
    items = []
    for dept in dept_list:
        if dept not in hc_by_dept:
            continue
        emp_codes = hc_by_dept[dept]
        hc = len(emp_codes)
        if hc == 0:
            continue

        present_days = 0.0
        wh_values = []
        late_count = total_late = leave_count = on_duty_count = 0

        for ec in emp_codes:
            for log in logs_by_emp.get(ec, []):
                st = log["status"]
                d = date_type.fromisoformat(log["date"])
                if st in PRESENCE:
                    if d.weekday() < 5:
                        present_days += 0.5 if log["half_day"] else 1.0
                    if log.get("work_hours") is not None:
                        wh_values.append(log["work_hours"])
                if log["late_minutes"] > 0:
                    late_count += 1
                    total_late += log["late_minutes"]
                if st == "LEAVE":
                    leave_count += 1
                if st == "ON_DUTY":
                    on_duty_count += 1

        avg_wh = half_up(sum(wh_values) / len(wh_values), 2) if wh_values else None
        items.append({
            "department": dept,
            "headcount": hc,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_str, last_str = month_bounds(month)

    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    # Start from employees to exclude orphan logs
    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": first_str, "$lte": last_str},
                            "late_minutes": {"$gt": 0},
                        }
                    }
                ],
                "as": "late_logs",
            }
        },
        {"$match": {"late_logs.0": {"$exists": True}}},
        {
            "$project": {
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": {"$sum": "$late_logs.late_minutes"},
                "late_count": {"$size": "$late_logs"},
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    rows = list(db.employees.aggregate(pipeline))

    # Standard competition ranking (1, 2, 2, 4)
    ranked = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        ranked.append({
            "rank": rank,
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    # Include all rows whose rank <= limit (ties at cutoff are all included)
    return {"month": month, "items": [r for r in ranked if r["rank"] <= limit]}


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    if not DATE_RE.match(from_date):
        raise HTTPException(422, "from must be YYYY-MM-DD")
    if not DATE_RE.match(to_date):
        raise HTTPException(422, "to must be YYYY-MM-DD")
    if to_date < from_date:
        raise HTTPException(422, "to must not be earlier than from")

    from_d = date_type.fromisoformat(from_date)
    to_d = date_type.fromisoformat(to_date)
    if (to_d - from_d).days > 91:
        raise HTTPException(422, "date range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    emp_docs = list(db.employees.find(
        {"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))
    emp_codes = [e["emp_code"] for e in emp_docs]

    # Use $densify to guarantee one document per calendar date in the range,
    # filling gaps with zero counts (MongoDB 5.1+, confirmed 8.3.1 here).
    # $densify works on numeric/date fields; we use a sentinel UTC midnight
    # datetime for the densify field, then project back to ISO date string.
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Convert ISO date string to UTC midnight datetime for $densify
        {
            "$addFields": {
                "_date_dt": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                }
            }
        },
        {
            "$group": {
                "_id": "$_date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        # $densify fills missing dates with zero-valued documents
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    densified = {}
    for row in db.attendance_logs.aggregate(pipeline):
        d_str = row["_id"].strftime("%Y-%m-%d")
        densified[d_str] = {
            "present_count": row["present_count"],
            "late_count": row["late_count"],
        }

    rows = []
    cur = from_d
    while cur <= to_d:
        cur_str = cur.isoformat()
        is_wd = cur.weekday() < 5
        hc = sum(1 for e in emp_docs if e["joined_on"] <= cur_str)
        agg = densified.get(cur_str, {"present_count": 0, "late_count": 0})
        present_count = agg["present_count"]
        late_count = agg["late_count"]
        att_rate = half_up(present_count / hc, 4) if (is_wd and hc > 0) else None
        rows.append({
            "date": cur_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "_r": att_rate,
        })
        cur += timedelta(days=1)

    rates = [row.pop("_r") for row in rows]
    for i, row in enumerate(rows):
        window = [rates[j] for j in range(max(0, i - 6), i + 1) if rates[j] is not None]
        row["moving_avg_7d"] = half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    def run_explain(coll_name: str, pipeline: list) -> dict:
        raw = db.command("aggregate", coll_name, pipeline=pipeline, explain=True, cursor={})
        return json.loads(json_util.dumps(raw))

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        pipeline = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": run_explain("attendance_logs", pipeline)}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_str, last_str = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
            {"$group": {"_id": None, "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": run_explain("attendance_logs", pipeline)}

    elif endpoint == "department_summary":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_str, last_str = month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": last_str}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"ec": "$emp_code"},
                    "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}, "date": {"$gte": first_str, "$lte": last_str}}}],
                    "as": "logs",
                }
            },
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        return {"endpoint": endpoint, "collection": "employees", "explain": run_explain("employees", pipeline)}

    elif endpoint == "late_leaderboard":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "month is required and must be YYYY-MM")
        first_str, last_str = month_bounds(month)
        emp_match = {"department": department} if department else {}
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"ec": "$emp_code"},
                    "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}, "date": {"$gte": first_str, "$lte": last_str}, "late_minutes": {"$gt": 0}}}],
                    "as": "late_logs",
                }
            },
            {"$match": {"late_logs.0": {"$exists": True}}},
            {"$project": {"emp_code": 1, "total_late_minutes": {"$sum": "$late_logs.late_minutes"}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {"$limit": limit},
        ]
        return {"endpoint": endpoint, "collection": "employees", "explain": run_explain("employees", pipeline)}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")
        if not DATE_RE.match(from_date) or not DATE_RE.match(to_date):
            raise HTTPException(422, "from and to must be YYYY-MM-DD")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
        pipeline = [
            {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": run_explain("attendance_logs", pipeline)}
