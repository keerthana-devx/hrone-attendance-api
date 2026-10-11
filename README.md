# Employee Attendance & Analytics API

A FastAPI application that manages employee records, daily attendance punch-in/punch-out, manual regularization, and monthly/department analytics. Backed by MongoDB.

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (local Docker or MongoDB Atlas free tier)
- Git

## Windows PowerShell setup (VS Code)

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If you see an execution policy error, run this first (once per machine):
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set:
```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas, use your connection string:
```
MONGO_URI=mongodb+srv://<user>:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
```

### 4. Start MongoDB (local Docker)

```powershell
docker run -d -p 27017:27017 --name mongo7 mongo:7
```

Or use MongoDB Atlas (no local install needed).

### 5. Load sample data

```powershell
python sample_seed.py
```

### 6. Start the application

```powershell
uvicorn app.main:app --port 8000 --reload
```

### 7. Open Swagger UI

Navigate to: http://localhost:8000/docs

### 8. Health check

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Expected: `{ "status": "ok" }`

### 9. Run tests

```powershell
pytest tests/ -v
```

## Key implementation notes

- All instants (punch times, `created_at`, history timestamps) are stored as BSON UTC datetimes in MongoDB and exposed as integer epoch milliseconds in the API.
- Attendance dates are IST calendar dates. Overnight shifts (shift_end ≤ shift_start) are handled per R1.
- Unique constraints are enforced by MongoDB indexes, not application-level checks, so concurrent duplicate requests are handled safely.
- Analytics read stored derived values (`late_minutes`, `work_hours`, etc.) rather than recomputing from punch times.
- `_id` never appears in any API response.

## Example requests

```powershell
# Create employee
Invoke-RestMethod -Method Post -Uri http://localhost:8000/employees `
  -ContentType "application/json" `
  -Body '{"emp_code":"EMP0010","name":"Test User","email":"test@example.com","department":"Engineering","joined_on":"2026-01-01"}'

# Punch in
Invoke-RestMethod -Method Post -Uri http://localhost:8000/attendance/punch-in `
  -ContentType "application/json" `
  -Body '{"emp_code":"EMP0010"}'

# Monthly analytics
Invoke-RestMethod "http://localhost:8000/analytics/employees/EMP0010/monthly?month=2026-07"
```
