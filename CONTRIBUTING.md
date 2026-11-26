# Contributing

Thank you for your interest in contributing to **SlotGuard Booking API**.

## Development Setup

```bash
# Prerequisites: Python 3.13+, Docker (for integration tests)
git clone https://github.com/bachiep/slotguard-booking-api.git
cd slotguard-booking-api
python -m venv .venv && .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # configure database credentials
alembic upgrade head              # run migrations
pytest -v                         # run full test suite (57 tests)
```

## Workflow

1. **Open an Issue** describing the bug or feature.
2. **Create a branch** from `main`: `feat/issue-<n>-<slug>` or `fix/issue-<n>-<slug>`.
3. **Write tests first** for any new logic.
4. **Follow Conventional Commits**: `feat(booking): add recurring slot support (#8)`.
5. **Ensure CI passes**: `ruff check . && ruff format --check . && pytest -v`.
6. **Open a Pull Request** referencing the issue: `Closes #<n>`.

## Code Standards

- Python 3.13, async FastAPI handlers, SQLAlchemy 2.0 `async_sessionmaker`.
- Ruff linter and formatter — zero warnings.
- pytest coverage ≥ 80% on new code.
- Alembic migration for any schema change.

## Docker Smoke Tests

```bash
docker compose up -d --build
python3 benchmarks/run_load_benchmark.py --base-url http://127.0.0.1:8102
docker compose down -v
```