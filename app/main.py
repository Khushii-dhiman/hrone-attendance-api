import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
import pymongo.errors

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup_db_client():
    try:
        db.employees.create_index("emp_code", unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", 1), ("status", 1)])
        print("Database indexes ready.")
    except Exception as e:
        print(f"Index error: {e}")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def ms_to_dt(ms: Optional[int]) -> datetime:
    """Converts epoch milliseconds to UTC datetime."""
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def get_ist_date_str(dt: datetime) -> str:
    """Returns YYYY-MM-DD in IST timezone."""
    return dt.astimezone(IST).strftime("%Y-%m-%d")

def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    year, month, day = map(int, date_str.split("-"))
    start_ist = datetime(year, month, day, h, m, 0, tzinfo=IST)
    
    punch_in_ist = punch_in_utc.astimezone(IST)
    delay_minutes = int((punch_in_ist - start_ist).total_seconds() / 60)
    return delay_minutes if delay_minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)

def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    year, month, day = map(int, date_str.split("-"))
    
    end_ist = datetime(year, month, day, h_end, m_end, 0, tzinfo=IST)
    if (h_end, m_end) <= (h_start, m_start):
        # Overnight shift: ends next day
        end_ist += timedelta(days=1)
        
    punch_out_ist = punch_out_utc.astimezone(IST)
    extra_minutes = int((punch_out_ist - end_ist).total_seconds() / 60)
    return max(0, extra_minutes)

def serialize_log(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc["id"] = str(doc.pop("_id", ""))
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    
    if "history" in doc and isinstance(doc["history"], list):
        for h in doc["history"]:
            if isinstance(h.get("at"), datetime):
                h["at"] = dt_to_ms(h["at"])
            changes = h.get("changes", {})
            for field in ("punch_in", "punch_out"):
                if field in changes:
                    if isinstance(changes[field].get("from"), datetime):
                        changes[field]["from"] = dt_to_ms(changes[field]["from"])
                    if isinstance(changes[field].get("to"), datetime):
                        changes[field]["to"] = dt_to_ms(changes[field]["to"])
    return doc

# --------------------------------------------------------------------------- #
# Request / Input Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendanceCorrectionIn(BaseModel):
    by: str
    reason: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    status: Optional[str] = None

# --------------------------------------------------------------------------- #
# Endpoints: Basic CRUD & Attendance
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = dt_to_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at)
    d = get_ist_date_str(ts)
    
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
        
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
        doc["_id"] = res.inserted_id
        return serialize_log(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at)
    d = get_ist_date_str(ts)
    
    # Check current day log
    log = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not log:
        # Check overnight shift started yesterday
        yesterday_str = (ts.astimezone(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        log = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": yesterday_str, "punch_out": None})
        if not log:
            raise HTTPException(404, "No punch-in found for today")
            
    if log.get("punch_out") is not None:
        raise HTTPException(400, "Already punched out")
        
    punch_in_ts = log["punch_in"]
    if ts < punch_in_ts:
        raise HTTPException(400, "punch_out cannot be earlier than punch_in")
        
    work_hours = compute_work_hours(punch_in_ts, ts)
    ot = compute_overtime(ts, emp["shift_start"], emp["shift_end"], log["date"])
    half_day = True if work_hours < 4.0 else False
    
    update_data = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": ot,
        "half_day": half_day
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
        {"$set": update_data},
        return_document=True
    )
    return serialize_log(updated)

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "Attendance record not found")
        
    changes = {}
    new_vals = {}
    
    if body.status is not None and body.status != log.get("status"):
        changes["status"] = {"from": log.get("status"), "to": body.status}
        new_vals["status"] = body.status
        
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else log.get("punch_in")
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else log.get("punch_out")
    
    if body.punch_in is not None and new_in != log.get("punch_in"):
        changes["punch_in"] = {"from": log.get("punch_in"), "to": new_in}
        new_vals["punch_in"] = new_in
        
    if body.punch_out is not None and new_out != log.get("punch_out"):
        changes["punch_out"] = {"from": log.get("punch_out"), "to": new_out}
        new_vals["punch_out"] = new_out
        
    if new_in and ("punch_in" in changes or "punch_out" in changes):
        new_late = compute_late_minutes(new_in, emp["shift_start"], date)
        if new_late != log.get("late_minutes"):
            changes["late_minutes"] = {"from": log.get("late_minutes"), "to": new_late}
            new_vals["late_minutes"] = new_late
            
    if new_in and new_out and ("punch_in" in changes or "punch_out" in changes):
        new_wh = compute_work_hours(new_in, new_out)
        if new_wh != log.get("work_hours"):
            changes["work_hours"] = {"from": log.get("work_hours"), "to": new_wh}
            new_vals["work_hours"] = new_wh
            
        new_ot = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date)
        if new_ot != log.get("overtime_minutes"):
            changes["overtime_minutes"] = {"from": log.get("overtime_minutes"), "to": new_ot}
            new_vals["overtime_minutes"] = new_ot
            
        new_hd = True if new_wh < 4.0 else False
        if new_hd != log.get("half_day"):
            changes["half_day"] = {"from": log.get("half_day"), "to": new_hd}
            new_vals["half_day"] = new_hd

    if not changes:
        return serialize_log(log)
        
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.by,
        "reason": body.reason,
        "changes": changes
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
        {
            "$set": new_vals,
            "$push": {"history": history_entry}
        },
        return_document=True
    )
    return serialize_log(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
):
    q = {}
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
    cursor = db.attendance_logs.find(q).sort("date", -1).skip(skip).limit(page_size)
    items = [serialize_log(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# --------------------------------------------------------------------------- #
# Endpoints: Analytics & Explain
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": None,
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_days": {"$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}},
                "wfh_days": {"$sum": {"$cond": [{"$eq": ["$status", "WFH"]}, 1, 0]}},
                "absent_days": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},
            }
        }
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        return {
            "emp_code": emp_code,
            "month": month,
            "total_work_hours": 0.0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "present_days": 0,
            "wfh_days": 0,
            "absent_days": 0,
            "leave_days": 0,
            "half_days": 0
        }
    stats = res[0]
    stats.pop("_id", None)
    stats["emp_code"] = emp_code
    stats["month"] = month
    stats["total_work_hours"] = round(stats["total_work_hours"], 2)
    return stats

@app.get("/analytics/departments/summary")
def departments_summary(date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$")):
    pipeline = [
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$in": ["$emp_code", "$$codes"]}, {"$eq": ["$date", date]}]}}}
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$in": ["$$l.status", ["PRESENT", "WFH"]]}
                        }
                    }
                },
                "absent_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ABSENT"]}
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                }
            }
        },
        {"$sort": {"department": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    return {"date": date, "items": items}

@app.get("/analytics/leaderboard/late")
def leaderboard_late(month: str = Query(..., regex=r"^\d{4}-\d{2}$"), limit: int = Query(5, ge=1)):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_days": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_days": 1
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$limit": limit}
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "log"
            }
        },
        {"$unwind": "$log"},
        {"$match": {"log.date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": "$log.date",
                "total_work_hours": {"$sum": {"$ifNull": ["$log.work_hours", 0]}},
                "present_count": {"$sum": {"$cond": [{"$in": ["$log.status", ["PRESENT", "WFH"]]}, 1, 0]}},
                "total_logs": {"$sum": 1}
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$present_count", 0]},
                        {"$round": [{"$divide": ["$total_work_hours", "$present_count"]}, 2]},
                        0.0
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "month": month, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    if endpoint == "leaderboard_late":
        pipeline = [
            {"$match": {"late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1}}
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "stages": explain_res.get("stages", []), "execution_stats": explain_res.get("executionStats", {})}
    elif endpoint == "list_attendance":
        explain_res = db.command("explain", {"find": "attendance_logs", "filter": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "execution_stats": explain_res.get("executionStats", {})}
    else:
        raise HTTPException(400, "Unknown endpoint for explain")