# YottaFlex — Supplier Management & Acknowledgement Control

A Flask + SQLite application for supplier management, purchase orders, supplier acknowledgements, promise-date revisions, change approvals, inventory planning, notifications, audit history, anomaly detection, and reproducible synthetic-data generation.

## Challenge workflow

1. Maintain suppliers, purchase orders, and PO lines.
2. Capture supplier acknowledgement revisions with accepted/rejected quantities and promised dates.
3. Store every promised date as a separate `PromiseDate` snapshot linked to its acknowledgement. Previous snapshots are retained as `Superseded`; they are not overwritten.
4. Submit change requests for quantity, price, and delivery-date changes. Requests remain `Pending` until an explicit planner decision creates an `Approval` record.
5. Configurable approval triggers are evaluated when a change is submitted. Defaults: quantity change greater than 10%, price change greater than 5%, or delivery-date movement greater than 7 days. All changes remain subject to explicit approval, including changes below these thresholds.
6. Refresh inventory projections from valid acknowledgement revisions. Pending/rejected changes do not update projected supply. Approved changes update the PO line only after an approval record is created.
7. Record notifications and audit events for traceability.
8. Run anomaly detection and synthetic dataset validation. Known anomalies are labelled in a separate ground-truth export, not mixed into model input features.

## Requirements

- Python 3.10+
- Flask
- Flask-SQLAlchemy
- pytest (for tests)

## Setup (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
$env:YOTTAFLEX_SECRET_KEY = "replace-with-a-long-random-secret"
python app.py
```

Open http://127.0.0.1:5000 in your browser. The app creates missing SQLite tables on first local startup. Existing database files are not included in the public source repository; a fresh local database is created automatically.

## Run tests

```powershell
python -m pytest -q
```

The test suite uses isolated temporary SQLite databases and does not intentionally modify the developer's local database.

## Generate the challenge dataset

1. Start the application.
2. Open **Data Generator** in the navigation.
3. Use `10000` purchase orders, `15000` combined acknowledgement/change events, and seed `20261022`.
4. Submit generation and wait for the validation/export confirmation.
5. The generated batch directory is created under `generated_data/` and contains CSV, JSON, SQL seed data, ground truth, generation summary, and validation summary. The interactive generator is the supported entry point; it persists the batch to the configured SQLite database and exports the matching files.
6. Repeat the exact same configuration to verify that the batch is reproducible. The generator verifies existing deterministic batch contents rather than silently overwriting conflicting data.

The generator's event count is the combined count: 7,500 acknowledgement records + 7,500 change requests for the 15,000-event baseline. The exact generated counts are recorded in `generation_summary.json` and `dataset_summary.csv`.

## Configuration

| Environment/config key | Default | Meaning |
|---|---:|---|
| `YOTTAFLEX_SECRET_KEY` | `dev-only-change-me` | Flask session signing key; set a strong random value outside development |
| `QTY_CHANGE_APPROVAL_PERCENT` | `10` | Quantity percentage change that triggers an approval-review reason |
| `PRICE_CHANGE_APPROVAL_PERCENT` | `5` | Price percentage change that triggers an approval-review reason |
| `DATE_CHANGE_APPROVAL_DAYS` | `7` | Delivery-date movement in days that triggers an approval-review reason |

All change requests remain pending until approved or rejected, regardless of threshold size.

## Database and traceability

- SQLite database: `instance/yottaflex.db` (local runtime only; ignored by Git).
- `AuditLog` stores append-only event descriptions and before/after values for key operations.
- `Acknowledgement` stores supplier revision history.
- `PromiseDate` stores immutable, acknowledgement-linked promise-date snapshots with effective dates and lifecycle state.
- `Approval` stores each decision independently from the change request.
- Foreign-key constraints are declared in SQLAlchemy models. The synthetic-data validator runs SQLite `PRAGMA foreign_key_check`.

## Data exports and ground truth

The generated batch contains `dataset.json` and tabular source files. `ground_truth.csv` / `ground_truth.json` are separate evaluation labels. Do not merge ground-truth columns into model input features. Review `validation_summary.json`, `generation_summary.json`, and `dataset_seed.sql` with each generated batch.

## Public GitHub checklist

- [ ] Install dependencies in a clean virtual environment.
- [ ] Run the app and manually verify each module.
- [ ] Run `python -m pytest -q` and record the result.
- [ ] Generate the full 10,000-PO / 15,000-event dataset.
- [ ] Verify all four required anomaly labels in `ground_truth.csv`.
- [ ] Confirm generated checksums match on a second run.
- [ ] Ensure `.env`, database files, backups, virtual environments, and credentials are not committed.

## Data provenance

The supplied generator creates synthetic company, supplier, order, acknowledgement, change, and approval records. It does not require scraping or confidential third-party data. The fixed seed and generator source provide reproducibility.
