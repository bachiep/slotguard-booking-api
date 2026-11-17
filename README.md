# SlotGuard Booking API

Async resource time-slot booking REST API built with Python 3.13, FastAPI, Async SQLAlchemy 2.0, and Alembic.

> Full documentation, architecture diagrams, and walkthroughs are added as core modules are completed.

## Quick Verification

```bash
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\ruff check .
.\.venv\Scripts\pytest -v
```
