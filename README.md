# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance (punch in/out, regularize) and analytics
(monthly summary, department summary, late leaderboard, daily trend). All application code
lives in `app/main.py`; the HTTP contract and business rules R1-R10 are in `openapi.yaml`.

## Requirements
- Python 3.11+ (developed on 3.14)
- MongoDB 6.0+ (developed on 7.0)

## Setup
```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Configuration comes from real environment variables (a local .env works too; env wins),
# e.g. MONGO_URI=mongodb://localhost:27017 and MONGO_DB=attendance_db.
python sample_seed.py        # optional: load the six sample employees / nine logs
```

## Run
```bash
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup. `GET /health` returns 200 when MongoDB answers
a ping, otherwise 503.

## Endpoints
- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Notes
- Employees are identified by `emp_code`; MongoDB `_id` never appears in a request or response.
- Instants are integer epoch **milliseconds**; dates and times are `YYYY-MM-DD` / `HH:MM` strings in IST.
- `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are server-computed; analytics read the stored values.
