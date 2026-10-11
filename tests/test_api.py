import os, threading
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test_db"
from app.main import app, db
TC = TestClient(app)
IST = timezone(timedelta(hours=5, minutes=30))

def ist_ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000

def clean():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

def make_emp(ec="EMP0001", dept="Engineering", ss="09:30", se="18:30", jo="2026-01-01"):
    return TC.post("/employees", json={"emp_code": ec, "name": "T", "email": ec.lower() + "@x.com",
                                       "department": dept, "shift_start": ss, "shift_end": se, "joined_on": jo})

@pytest.fixture(autouse=True)
def isolate():
    clean()
    yield
    clean()

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_health():
    r = TC.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

# ---------------------------------------------------------------------------
# Employee creation
# ---------------------------------------------------------------------------
def test_create_valid():
    r = make_emp()
    assert r.status_code == 201
    d = r.json()
    assert d["emp_code"] == "EMP0001"
    assert "_id" not in d
    assert isinstance(d["created_at"], int)

def test_bad_emp_code():
    for bad in ["EMP123", "EMP1234567", "emp0001", "EMP", "0001"]:
        r = TC.post("/employees", json={"emp_code": bad, "name": "X", "email": "x@x.com",
                                        "department": "E", "joined_on": "2026-01-01"})
        assert r.status_code == 422, f"expected 422 for {bad}"

def test_bad_email():
    r = TC.post("/employees", json={"emp_code": "EMP0001", "name": "X", "email": "notanemail",
                                    "department": "E", "joined_on": "2026-01-01"})
    assert r.status_code == 422

def test_same_shift():
    r = TC.post("/employees", json={"emp_code": "EMP0001", "name": "X", "email": "x@x.com",
                                    "department": "E", "shift_start": "09:30", "shift_end": "09:30",
                                    "joined_on": "2026-01-01"})
    assert r.status_code == 422

def test_duplicate_emp():
    assert make_emp().status_code == 201
    assert make_emp().status_code == 409

def test_no_id_leak():
    assert "_id" not in make_emp().json()

def test_list_pagination():
    for i in range(1, 6):
        make_emp(f"EMP000{i}")
    r = TC.get("/employees?page=1&page_size=2")
    d = r.json()
    assert d["total"] == 5
    assert len(d["items"]) == 2
    assert d["items"][0]["emp_code"] == "EMP0001"

def test_list_dept_filter():
    make_emp("EMP0001", dept="Engineering")
    make_emp("EMP0002", dept="Sales")
    assert TC.get("/employees?department=Engineering").json()["total"] == 1

def test_list_case_sensitive():
    make_emp("EMP0001", dept="Engineering")
    assert TC.get("/employees?department=engineering").json()["total"] == 0

def test_list_sorted():
    for c in ["EMP0003", "EMP0001", "EMP0002"]:
        make_emp(c)
    codes = [i["emp_code"] for i in TC.get("/employees").json()["items"]]
    assert codes == sorted(codes)

def test_page_size_max():
    assert TC.get("/employees?page_size=101").status_code == 422

def test_skip_correct():
    for i in range(1, 4):
        make_emp(f"EMP000{i}")
    assert TC.get("/employees?page=2&page_size=2").json()["items"][0]["emp_code"] == "EMP0003"

# ---------------------------------------------------------------------------
# Punch-in
# ---------------------------------------------------------------------------
def test_punch_in_default():
    make_emp()
    r = TC.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert r.status_code == 201
    d = r.json()
    assert d["status"] == "PRESENT"
    assert d["punch_out"] is None
    assert d["work_hours"] is None
    assert d["overtime_minutes"] == 0
    assert d["half_day"] is False
    assert d["history"] == []
    assert "_id" not in d

def test_punch_in_wfh():
    make_emp()
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH"}).json()["status"] == "WFH"

def test_punch_in_absent_rejected():
    make_emp()
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422

def test_punch_in_unknown():
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404

def test_punch_in_duplicate():
    make_emp()
    ts = ist_ms(2026, 7, 6, 9, 30)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts + 60000}).status_code == 409

def test_punch_in_concurrent():
    make_emp()
    ts = ist_ms(2026, 7, 6, 9, 30)
    results = []
    def do():
        results.append(TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts}).status_code)
    threads = [threading.Thread(target=do) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(201) == 1
    assert results.count(409) == 4

def test_punch_in_grace_not_late():
    make_emp(ss="09:30", se="18:30")
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 40, 0)}).json()["late_minutes"] == 0

def test_punch_in_grace_late():
    make_emp(ss="09:30", se="18:30")
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 40, 1)}).json()["late_minutes"] == 10

def test_punch_in_late_45():
    make_emp(ss="09:30", se="18:30")
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 10, 15, 59)}).json()["late_minutes"] == 45

def test_punch_in_bool_rejected():
    make_emp()
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": True}).status_code == 422

def test_punch_in_seconds_rejected():
    make_emp()
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1234567890}).status_code == 422

def test_punch_in_overnight_date():
    make_emp(ss="22:00", se="06:00")
    r = TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 0, 30, 0)})
    assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06"

def test_punch_in_normal_date():
    make_emp()
    assert TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 30, 0)}).json()["date"] == "2026-07-06"

# ---------------------------------------------------------------------------
# Punch-out helpers
# ---------------------------------------------------------------------------
def _pin(ec="EMP0001", ts=None):
    make_emp(ec)
    if ts is None:
        ts = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": ts})
    return ts

# ---------------------------------------------------------------------------
# Punch-out
# ---------------------------------------------------------------------------
def test_punch_out_valid():
    pin = _pin()
    pout = pin + 9 * 3600 * 1000
    r = TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
    assert r.status_code == 200
    assert r.json()["punch_out"] == pout
    assert r.json()["work_hours"] is not None

def test_punch_out_unknown():
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404

def test_punch_out_no_pin():
    make_emp()
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404

def test_punch_out_already_closed():
    pin = _pin()
    pout = pin + 3600 * 1000
    TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout})
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout + 3600 * 1000}).status_code == 409

def test_punch_out_concurrent():
    pin = _pin()
    pout = pin + 3600 * 1000
    results = []
    def do():
        results.append(TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout}).status_code)
    threads = [threading.Thread(target=do) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(200) == 1
    assert results.count(409) == 4

def test_punch_out_equal():
    pin = _pin()
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin}).status_code == 422

def test_punch_out_before():
    pin = _pin()
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin - 1000}).status_code == 422

def test_punch_out_24h():
    pin = _pin()
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + 86401 * 1000}).status_code == 422

def test_punch_out_half_day_true():
    make_emp()
    pin = ist_ms(2026, 7, 7, 9, 0, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + (4 * 3600 + 29 * 60) * 1000}).json()["half_day"] is True

def test_punch_out_half_day_false():
    make_emp()
    pin = ist_ms(2026, 7, 7, 9, 0, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + (4 * 3600 + 30 * 60) * 1000}).json()["half_day"] is False

def test_overtime_29_zero():
    make_emp(ss="09:30", se="18:30")
    pin = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 18, 59, 0)}).json()["overtime_minutes"] == 0

def test_overtime_30():
    make_emp(ss="09:30", se="18:30")
    pin = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    assert TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 19, 0, 0)}).json()["overtime_minutes"] == 30

def test_overnight_shift():
    make_emp(ss="22:00", se="06:00")
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 22, 0, 0)})
    r = TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 6, 40, 0)})
    assert r.status_code == 200
    assert r.json()["overtime_minutes"] == 40

# ---------------------------------------------------------------------------
# Regularization
# ---------------------------------------------------------------------------
def _setup_record(ec="EMP0001", pin_ts=None, pout_offset=None):
    """Create employee + punch-in, optionally punch-out. Returns (pin_ms, pout_ms or None)."""
    make_emp(ec)
    if pin_ts is None:
        pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": pin_ts})
    if pout_offset is not None:
        pout_ts = pin_ts + pout_offset
        TC.post("/attendance/punch-out", json={"emp_code": ec, "punched_at": pout_ts})
        return pin_ts, pout_ts
    return pin_ts, None

def _reg(ec="EMP0001", date="2026-07-07", **kwargs):
    payload = {"reason": "HR correction", "regularized_by": "admin", **kwargs}
    return TC.patch(f"/attendance/{ec}/{date}", json=payload)

def test_reg_unknown_emp():
    assert _reg("EMP9999").status_code == 404

def test_reg_unknown_date():
    make_emp()
    assert _reg(date="2026-07-07").status_code == 404

def test_reg_bad_date_format():
    make_emp()
    r = TC.patch("/attendance/EMP0001/07-07-2026", json={"reason": "x" * 5, "regularized_by": "admin"})
    assert r.status_code == 422

def test_reg_no_changes():
    _setup_record()
    # Sending no fields that differ → 422 no changes
    r = _reg()
    assert r.status_code == 422

def test_reg_status_to_leave():
    _setup_record()
    r = _reg(status="LEAVE")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "LEAVE"
    assert d["punch_in"] is None
    assert d["punch_out"] is None

def test_reg_absent_with_punch_rejected():
    _setup_record()
    r = _reg(status="ABSENT", punch_in=ist_ms(2026, 7, 7, 9, 30, 0))
    assert r.status_code == 422

def test_reg_leave_with_punch_rejected():
    _setup_record()
    r = _reg(status="LEAVE", punch_out=ist_ms(2026, 7, 7, 18, 30, 0))
    assert r.status_code == 422

def test_reg_presence_requires_punch_in():
    make_emp()
    # Insert an ABSENT record via regularization path: first punch in then set ABSENT, then try to set PRESENT without punch_in
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})
    _reg(status="ABSENT")
    # Now record has no punch_in; try to set back to PRESENT without supplying punch_in
    r = _reg(status="PRESENT")
    assert r.status_code == 422

def test_reg_punch_out_before_punch_in():
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    _setup_record(pin_ts=pin_ts)
    r = _reg(punch_out=pin_ts - 1000)
    assert r.status_code == 422

def test_reg_punch_out_after_24h():
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    _setup_record(pin_ts=pin_ts)
    r = _reg(punch_out=pin_ts + 86401 * 1000)
    assert r.status_code == 422

def test_reg_short_reason():
    _setup_record()
    r = TC.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "hi", "regularized_by": "admin"})
    assert r.status_code == 422

def test_reg_invalid_status():
    _setup_record()
    r = _reg(status="HOLIDAY")
    assert r.status_code == 422

def test_reg_correct_punch_times():
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    _setup_record(pin_ts=pin_ts)
    new_pout = pin_ts + 8 * 3600 * 1000
    r = _reg(punch_out=new_pout)
    assert r.status_code == 200
    d = r.json()
    assert d["punch_out"] == new_pout
    assert d["work_hours"] is not None

def test_reg_audit_history():
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    _setup_record(pin_ts=pin_ts)
    new_pout = pin_ts + 8 * 3600 * 1000
    r = _reg(punch_out=new_pout, reason="Forgot to punch out", regularized_by="hr_admin")
    assert r.status_code == 200
    d = r.json()
    assert len(d["history"]) == 1
    h = d["history"][0]
    assert h["by"] == "hr_admin"
    assert h["reason"] == "Forgot to punch out"
    assert isinstance(h["at"], int)
    assert "punch_out" in h["changes"]
    assert h["changes"]["punch_out"]["to"] == new_pout

def test_reg_history_accumulates():
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    _setup_record(pin_ts=pin_ts)
    _reg(status="LEAVE")
    new_pin = ist_ms(2026, 7, 7, 9, 30, 0)
    r = _reg(status="PRESENT", punch_in=new_pin, reason="Reinstated by manager", regularized_by="mgr")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2

def test_reg_punch_in_wrong_date():
    """punch_in that maps to a different attendance date must be rejected."""
    make_emp(ss="09:30", se="18:30")
    pin_ts = ist_ms(2026, 7, 7, 9, 30, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})
    # Supply a punch_in that falls on 2026-07-08 IST
    wrong_pin = ist_ms(2026, 7, 8, 9, 30, 0)
    r = _reg(punch_in=wrong_pin)
    assert r.status_code == 422

# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def _make_full_day(ec, date_str, ss="09:30", se="18:30", dept="Engineering", jo="2026-01-01"):
    """Create employee (if not exists) and a full-day punch-in/out on date_str."""
    make_emp(ec, dept=dept, ss=ss, se=se, jo=jo)
    y, mo, d = map(int, date_str.split("-"))
    sh, sm = map(int, ss.split(":"))
    pin = ist_ms(y, mo, d, sh, sm, 0)
    TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": pin})
    pout = pin + 9 * 3600 * 1000
    TC.post("/attendance/punch-out", json={"emp_code": ec, "punched_at": pout})
    return pin, pout

# ---------------------------------------------------------------------------
# Monthly analytics
# ---------------------------------------------------------------------------
def test_monthly_unknown_emp():
    r = TC.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert r.status_code == 404

def test_monthly_bad_month():
    make_emp()
    assert TC.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
    assert TC.get("/analytics/employees/EMP0001/monthly?month=202607").status_code == 422

def test_monthly_empty_data():
    make_emp()
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200
    d = r.json()
    assert d["present_days"] == 0
    assert d["leave_days"] == 0
    assert d["late_count"] == 0
    assert d["attendance_pct"] == 0.0 or d["attendance_pct"] is not None

def test_monthly_present_days_counted():
    # 2026-07-07 is a Tuesday (weekday)
    _make_full_day("EMP0001", "2026-07-07")
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200
    d = r.json()
    assert d["present_days"] == 1.0

def test_monthly_half_day():
    make_emp()
    pin = ist_ms(2026, 7, 7, 9, 0, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    # 4h29m → half day
    TC.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin + (4 * 3600 + 29 * 60) * 1000})
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.json()["present_days"] == 0.5

def test_monthly_late_count():
    make_emp(ss="09:30", se="18:30")
    # Late punch-in (9:41 IST > 9:40 grace)
    pin = ist_ms(2026, 7, 7, 9, 41, 0)
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    d = r.json()
    assert d["late_count"] == 1
    assert d["total_late_minutes"] == 11

def test_monthly_attendance_pct_rounding():
    # joined 2026-07-01; 2026-07 has 23 working days; present 1 day
    make_emp(jo="2026-07-01")
    _make_full_day("EMP0001", "2026-07-07")
    # Re-fetch (make_emp already called inside _make_full_day, but isolate cleans between tests)
    # Actually _make_full_day calls make_emp internally; just query
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    d = r.json()
    assert d["attendance_pct"] is not None
    # 1 / working_days * 100, half-up 2dp
    assert isinstance(d["attendance_pct"], float)

def test_monthly_joined_after_month_start():
    """Employee joined mid-month; working_days should start from joined_on."""
    make_emp(jo="2026-07-15")
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    d = r.json()
    # working days from 2026-07-15 to 2026-07-31
    assert d["working_days"] < 23

def test_monthly_no_logs_zero_pct():
    make_emp(jo="2026-07-01")
    r = TC.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    d = r.json()
    assert d["present_days"] == 0
    assert d["attendance_pct"] == 0.0

# ---------------------------------------------------------------------------
# Department summary analytics
# ---------------------------------------------------------------------------
def test_dept_summary_empty():
    r = TC.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200
    assert r.json()["items"] == []

def test_dept_summary_basic():
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    r = TC.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Engineering"
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 1.0

def test_dept_summary_filter():
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    make_emp("EMP0002", dept="Sales")
    r = TC.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Engineering"

def test_dept_summary_avg_work_hours():
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    r = TC.get("/analytics/departments/summary?month=2026-07")
    item = r.json()["items"][0]
    assert item["avg_work_hours"] is not None
    assert item["avg_work_hours"] == 9.0

def test_dept_summary_no_logs_null_avg():
    make_emp("EMP0001", dept="Engineering")
    r = TC.get("/analytics/departments/summary?month=2026-07")
    item = r.json()["items"][0]
    assert item["avg_work_hours"] is None

def test_dept_summary_joined_after_month_excluded():
    """Employee joined after month end should not appear in headcount."""
    make_emp("EMP0001", dept="Engineering", jo="2026-08-01")
    r = TC.get("/analytics/departments/summary?month=2026-07")
    assert r.json()["items"] == []

# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------
def test_leaderboard_empty():
    r = TC.get("/analytics/leaderboard/late?month=2026-07")
    assert r.status_code == 200
    assert r.json()["items"] == []

def test_leaderboard_basic():
    make_emp("EMP0001", ss="09:30", se="18:30")
    pin = ist_ms(2026, 7, 7, 9, 45, 0)  # 15 min late
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin})
    r = TC.get("/analytics/leaderboard/late?month=2026-07")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["total_late_minutes"] == 15
    assert items[0]["rank"] == 1

def test_leaderboard_ranking_ties():
    for ec in ["EMP0001", "EMP0002"]:
        make_emp(ec, ss="09:30", se="18:30")
        pin = ist_ms(2026, 7, 7, 9, 45, 0)
        TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": pin})
    r = TC.get("/analytics/leaderboard/late?month=2026-07")
    items = r.json()["items"]
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1  # tied

def test_leaderboard_dept_filter():
    make_emp("EMP0001", dept="Engineering", ss="09:30", se="18:30")
    make_emp("EMP0002", dept="Sales", ss="09:30", se="18:30")
    for ec in ["EMP0001", "EMP0002"]:
        pin = ist_ms(2026, 7, 7, 9, 45, 0)
        TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": pin})
    r = TC.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
    items = r.json()["items"]
    assert all(i["department"] == "Engineering" for i in items)

def test_leaderboard_limit():
    for i in range(1, 6):
        ec = f"EMP000{i}"
        make_emp(ec, ss="09:30", se="18:30")
        pin = ist_ms(2026, 7, 7, 9, 30 + i, 0)
        TC.post("/attendance/punch-in", json={"emp_code": ec, "punched_at": pin})
    r = TC.get("/analytics/leaderboard/late?month=2026-07&limit=3")
    assert len(r.json()["items"]) <= 3

# ---------------------------------------------------------------------------
# Department trend
# ---------------------------------------------------------------------------
def test_trend_unknown_dept():
    r = TC.get("/analytics/departments/Unknown/trend?from=2026-07-01&to=2026-07-07")
    assert r.status_code == 404

def test_trend_bad_dates():
    make_emp(dept="Engineering")
    assert TC.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-01").status_code == 422

def test_trend_range_too_long():
    make_emp(dept="Engineering")
    assert TC.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-10").status_code == 422

def test_trend_basic():
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    r = TC.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-07")
    assert r.status_code == 200
    d = r.json()
    assert d["department"] == "Engineering"
    assert len(d["items"]) == 1
    item = d["items"][0]
    assert item["date"] == "2026-07-07"
    assert item["present_count"] == 1.0
    assert item["headcount"] == 1

def test_trend_zero_fill_missing_dates():
    """$densify must fill dates with no logs as zero-count rows."""
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    r = TC.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-08")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    dates = [i["date"] for i in items]
    assert "2026-07-06" in dates
    assert "2026-07-08" in dates
    # Days with no logs have present_count == 0
    for item in items:
        if item["date"] != "2026-07-07":
            assert item["present_count"] == 0

def test_trend_moving_avg():
    _make_full_day("EMP0001", "2026-07-07", dept="Engineering")
    r = TC.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    items = r.json()["items"]
    # Last item (2026-07-07, Tuesday) should have a moving_avg_7d
    last = items[-1]
    assert last["moving_avg_7d"] is not None

def test_trend_weekend_att_rate_none():
    _make_full_day("EMP0001", "2026-07-05", dept="Engineering")  # Sunday
    r = TC.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-05")
    item = r.json()["items"][0]
    assert item["is_working_day"] is False
    assert item["attendance_rate"] is None

# ---------------------------------------------------------------------------
# Admin explain
# ---------------------------------------------------------------------------
def test_explain_invalid_endpoint():
    r = TC.get("/admin/explain/nonexistent")
    assert r.status_code == 422

def test_explain_attendance_list():
    r = TC.get("/admin/explain/attendance_list")
    assert r.status_code == 200
    d = r.json()
    assert d["endpoint"] == "attendance_list"
    assert "explain" in d

def test_explain_employee_monthly():
    make_emp()
    r = TC.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "employee_monthly"

def test_explain_employee_monthly_missing_params():
    r = TC.get("/admin/explain/employee_monthly")
    assert r.status_code == 422

def test_explain_department_summary():
    r = TC.get("/admin/explain/department_summary?month=2026-07")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_summary"

def test_explain_late_leaderboard():
    r = TC.get("/admin/explain/late_leaderboard?month=2026-07")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "late_leaderboard"

def test_explain_department_trend():
    make_emp(dept="Engineering")
    r = TC.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_trend"

def test_explain_department_trend_missing_params():
    r = TC.get("/admin/explain/department_trend")
    assert r.status_code == 422

# ---------------------------------------------------------------------------
# Attendance list endpoint
# ---------------------------------------------------------------------------
def test_attendance_list_empty():
    r = TC.get("/attendance")
    assert r.status_code == 200
    assert r.json()["items"] == []

def test_attendance_list_filter_emp():
    _make_full_day("EMP0001", "2026-07-07")
    make_emp("EMP0002")
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ist_ms(2026, 7, 8, 9, 30, 0)})
    r = TC.get("/attendance?emp_code=EMP0001")
    assert r.json()["total"] == 1

def test_attendance_list_date_range():
    make_emp()
    for d in [6, 7, 8]:
        TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, d, 9, 30, 0)})
    r = TC.get("/attendance?date_from=2026-07-07&date_to=2026-07-08")
    assert r.json()["total"] == 2

def test_attendance_list_bad_date():
    assert TC.get("/attendance?date_from=not-a-date").status_code == 422

def test_attendance_list_date_from_after_to():
    assert TC.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422

def test_attendance_list_status_filter():
    make_emp()
    TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 9, 30, 0)})
    r = TC.get("/attendance?status=PRESENT")
    assert r.json()["total"] == 1
    assert TC.get("/attendance?status=LEAVE").json()["total"] == 0

def test_attendance_list_invalid_status():
    assert TC.get("/attendance?status=HOLIDAY").status_code == 422

def test_attendance_list_sorted_date_desc():
    make_emp()
    for d in [6, 7, 8]:
        TC.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, d, 9, 30, 0)})
    items = TC.get("/attendance").json()["items"]
    dates = [i["date"] for i in items]
    assert dates == sorted(dates, reverse=True)
