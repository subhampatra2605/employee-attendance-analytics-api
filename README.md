# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee management, attendance punch-in/punch-out, manual attendance corrections, and attendance analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB Atlas is supported)

## Setup

Open PowerShell in the project root and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root with your MongoDB configuration:

```dotenv
MONGO_URI="your-mongodb-connection-string"
MONGO_DB="attendance_db"
```

Do not commit `.env` or expose database credentials.

## Run the API

```powershell
python -m uvicorn app.main:app --port 8000
```

- Health check: `GET /health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`

## Implemented Features

- Employee creation and paginated employee listing
- Attendance punch-in and punch-out
- Paginated attendance listing
- Manual attendance corrections with audit history
- Monthly employee attendance analytics
- Department monthly summary
- Late-arrival leaderboard
- Department date-range trend
- MongoDB query execution explanations

Indexes are created automatically when the application starts.

## Sample Data

`sample_data/` contains sample documents, and `sample_seed.py` loads them into MongoDB. **Use the seed script only in a disposable development database:** it deletes existing documents in the relevant collections before inserting the sample data.

## Project and Submission Notes

- API implementation: `app/main.py`
- API contract: `openapi.yaml`
- Data model: `DATA_MODEL.md`
- Review notes: `REVIEW.md`
- Design decisions: `DECISIONS.md`

Set `MONGO_URI` and `MONGO_DB` through environment variables or `.env`. Never commit credentials, `.env`, or the virtual environment.