# HROne Attendance Management API

A backend application built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and workforce analytics.

## Features

- Employee creation and listing with filtering and pagination.
- Attendance punch-in and punch-out.
- Attendance regularization with history tracking.
- Employee monthly attendance summaries.
- Department-level attendance summaries.
- Late-arrival leaderboard with ranking.
- Department attendance trends and seven-day moving averages.
- MongoDB aggregation pipelines for analytics.
- Query execution-plan diagnostics.
- Input validation and meaningful HTTP error responses.
- MongoDB indexes for efficient queries and data integrity.

## Technology Stack

- **Language:** Python 3.11+
- **Backend Framework:** FastAPI
- **Database:** MongoDB 7.0+
- **Database Driver:** PyMongo
- **ASGI Server:** Uvicorn
- **API Documentation:** OpenAPI / Swagger UI

## Prerequisites

Install the following before running the project:

- Python 3.11 or newer
- MongoDB 7.0 or a compatible MongoDB deployment
- Git

## Project Structure

```text
candidate_kit/
├── app/
│   └── main.py
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── .env
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── PROBLEM_STATEMENT.docx
├── README.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Installation and Setup

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd candidate_kit
```

If you already have the project locally, open a terminal in the project root.

### 2. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=hrone_attendance
```

Ensure MongoDB is running before starting the application.

**Security:** Never commit `.env` files, database credentials, or other secrets to version control.

## Running the Application

From the project root, execute:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

The API will be available at:

- **API Base URL:** `http://127.0.0.1:8000`
- **Swagger UI:** `http://127.0.0.1:8000/docs`
- **ReDoc:** `http://127.0.0.1:8000/redoc`
- **Health Check:** `http://127.0.0.1:8000/health`

Use Swagger UI to inspect request schemas, submit API requests, and review responses.

## API Capabilities

The application supports the following operations:

### Employee Management

- Create employee records.
- List employees with filtering and pagination.
- Validate employee identifiers and request parameters.

### Attendance Management

- Record employee punch-in and punch-out events.
- Prevent duplicate daily attendance entries.
- Retrieve attendance records with filtering and pagination.
- Regularize attendance records while preserving change history.

### Analytics

- Generate employee monthly attendance summaries.
- Generate department-level attendance summaries.
- Rank employees by late-arrival metrics.
- Generate department attendance trends and seven-day moving averages.

### Query Diagnostics

- Inspect query execution plans and execution statistics for supported endpoints.
- Review index usage to help identify inefficient query patterns.

Refer to `openapi.yaml` and `/docs` for the exact endpoint paths, parameters, request bodies, and response schemas.

## Database Design

The application uses two MongoDB collections:

### `employees`

Stores employee information, including employee code, department, shift details, joining date, and creation timestamp.

### `attendance_logs`

Stores daily attendance information, including employee code, attendance date, status, punch timestamps, work hours, late-arrival minutes, overtime minutes, half-day information, and regularization history.

MongoDB indexes support employee lookups, department filtering, joining-date queries, attendance-date queries, status-based queries, and employee/date uniqueness.

See `DATA_MODEL.md` for additional details about the database schema.

## Sample Data

The `sample_data/` directory contains JSON datasets for employees and attendance records.

The `sample_seed.py` script is provided for loading the candidate kit's sample data. Review its behavior before running it against a database containing existing records.

When loading sample data:

1. Ensure employee records exist before importing attendance records.
2. Check for existing employee codes and employee/date attendance combinations.
3. Avoid importing duplicate records into an existing database.
4. Use synthetic data for development and testing.

## Documentation

| File | Description |
|---|---|
| `PROBLEM_STATEMENT.docx` | Original assignment requirements |
| `openapi.yaml` | API contract and business rules |
| `DATA_MODEL.md` | MongoDB data model |
| `DECISIONS.md` | Implementation decisions and trade-offs |
| `REVIEW.md` | Code review findings and resolutions |

## Testing and Verification

Start the application and use Swagger UI at `/docs` to exercise the API.

Verify important scenarios, including:

- Valid and invalid request parameters.
- Duplicate employee creation.
- Duplicate attendance punch-in attempts.
- Punch-out for unknown employees or unavailable attendance records.
- Attendance regularization and history preservation.
- Pagination and filtering.
- Monthly, department, leaderboard, and trend analytics.
- Invalid date ranges and unknown departments.
- Query execution-plan diagnostics.

## Git and Submission

Before submitting the project, verify that:

- The application starts successfully.
- Dependencies are documented in `requirements.txt`.
- `README.md`, `DATA_MODEL.md`, `DECISIONS.md`, and `REVIEW.md` are included.
- `openapi.yaml` reflects the implemented API.
- `.env`, `.venv/`, Python cache files, and secrets are excluded from version control.
- Sample datasets contain only appropriate synthetic test data.
- The repository follows the assignment's submission requirements.