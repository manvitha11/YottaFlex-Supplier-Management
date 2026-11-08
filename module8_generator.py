import csv
import hashlib
import json
import math
import os
import random
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text


DEFAULT_SEED = 20261022
DEFAULT_PURCHASE_ORDER_COUNT = 10000
DEFAULT_EVENT_COUNT = 15000
MIN_PURCHASE_ORDER_COUNT = 4
MIN_EVENT_COUNT = 8
MAX_PURCHASE_ORDER_COUNT = 10000
MAX_EVENT_COUNT = 100000

CONTACT_NAMES = (
    "Akshu Sharma",
    "Rekha Iyer",
    "Srinu Reddy",
    "Ravi Nair",
    "Maya Patel",
    "Kiran Rao",
    "Ananya Das",
    "Vikram Mehta",
    "Neha Kapoor",
    "Arjun Menon",
    "Priya Shah",
    "Devika Joshi",
    "Sanjay Kumar",
    "Meera Krishnan",
    "Aarav Singh",
    "Divya Pillai",
    "Rahul Verma",
    "Sahana Gowda",
    "Nikhil Bhat",
    "Aditi Kulkarni",
)

COMPANY_DESCRIPTORS = (
    "Precision Components",
    "Industrial Systems",
    "Engineering Works",
    "Advanced Materials",
    "Automation Products",
    "Manufacturing Group",
    "Electrical Industries",
    "Supply Solutions",
)

COMPANY_SUFFIXES = ("Pvt Ltd", "Industries", "Technologies", "Manufacturing")
PRODUCTS = (
    ("VALVE-100", "Stainless steel control valve"),
    ("MOTOR-220", "Three-phase industrial motor"),
    ("SENSOR-310", "Temperature and pressure sensor"),
    ("PUMP-415", "High-efficiency process pump"),
    ("CABLE-520", "Shielded industrial power cable"),
    ("FILTER-630", "Replaceable hydraulic filter assembly"),
    ("GEAR-740", "Precision-machined gearbox"),
    ("PANEL-850", "Industrial electrical control panel"),
)
CHANGE_REASONS = (
    "Supplier capacity adjustment",
    "Material specification update",
    "Revised production schedule",
    "Updated commercial quotation",
)
REVIEWERS = ("Rekha Iyer", "Srinu Reddy", "Ravi Nair", "Maya Patel")
APPROVAL_STATUSES = ("Pending", "Approved", "Rejected")
EXPORT_FILES = (
    "suppliers.csv",
    "purchase_orders.csv",
    "po_lines.csv",
    "acknowledgements.csv",
    "acknowledgement_diagnostics.csv",
    "change_requests.csv",
    "approvals.csv",
    "inventory_plans.csv",
    "ground_truth.csv",
    "dataset_summary.csv",
    "dataset.json",
    "ground_truth.json",
    "validation_summary.json",
    "generation_summary.json",
    "dataset_seed.sql",
)
MODEL_KEYS = {
    "suppliers": "Supplier",
    "purchase_orders": "PurchaseOrder",
    "po_lines": "POLine",
    "acknowledgements": "Acknowledgement",
    "change_requests": "ChangeRequest",
    "approvals": "Approval",
    "inventory_plans": "InventoryPlan",
}
UNIQUE_BUSINESS_KEYS = {
    "suppliers": ("supplier_code",),
    "purchase_orders": ("po_number",),
    "acknowledgements": ("acknowledgement_number",),
    "change_requests": ("change_number",),
    "inventory_plans": ("po_line_id",),
}


class DatasetValidationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class DatasetGenerationError(RuntimeError):
    pass


def _batch_id(seed, purchase_order_count, event_count):
    source = f"yottaflex-module8-v1:{seed}:{purchase_order_count}:{event_count}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _deterministic_id(seed, purchase_order_count, event_count, table_name, index):
    source = (
        f"yottaflex-module8-pk-v1:{seed}:{purchase_order_count}:"
        f"{event_count}:{table_name}:{index}"
    )
    value = int.from_bytes(
        hashlib.sha256(source.encode("utf-8")).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    return value or 1


def _build_id_maps(seed, purchase_order_count, event_count, models, counts):
    return {
        key: [
            _deterministic_id(
                seed,
                purchase_order_count,
                event_count,
                models[MODEL_KEYS[key]].__tablename__,
                index,
            )
            for index in range(count)
        ]
        for key, count in counts.items()
    }


def _chunks(values, size=500):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _identify_existing_batch(session, models, expected_records):
    candidates = {}
    found_any = False

    for key, model_name in MODEL_KEYS.items():
        model = models[model_name]
        expected = expected_records[key]
        expected_ids = [record.id for record in expected]
        if len(expected_ids) != len(set(expected_ids)):
            raise DatasetGenerationError(
                f"Generated IDs are not unique in {model.__tablename__}."
            )

        for field in UNIQUE_BUSINESS_KEYS.get(key, ()):
            values = [getattr(record, field) for record in expected]
            if len(values) != len(set(values)):
                raise DatasetGenerationError(
                    f"Generated {field} values are not unique."
                )

        found_by_id = {}
        for id_chunk in _chunks(expected_ids):
            for record in (
                session.query(model)
                .filter(model.id.in_(id_chunk))
                .all()
            ):
                found_by_id[record.id] = record

        for field in UNIQUE_BUSINESS_KEYS.get(key, ()):
            values = [getattr(record, field) for record in expected]
            for value_chunk in _chunks(values):
                for record in (
                    session.query(model)
                    .filter(getattr(model, field).in_(value_chunk))
                    .all()
                ):
                    found_by_id[record.id] = record

        candidates[key] = found_by_id
        found_any = found_any or bool(found_by_id)

    if not found_any:
        return None

    for key, expected in expected_records.items():
        model = models[MODEL_KEYS[key]]
        actual_by_id = candidates[key]
        expected_by_id = {record.id: record for record in expected}
        if set(actual_by_id) != set(expected_by_id):
            found_ids = sorted(actual_by_id)
            unexpected_ids = sorted(set(actual_by_id) - set(expected_by_id))
            expected_business_values = {
                field: {
                    getattr(expected_record, field)
                    for expected_record in expected
                }
                for field in UNIQUE_BUSINESS_KEYS.get(key, ())
            }
            matching_business_keys = [
                field
                for field, values in expected_business_values.items()
                if any(
                    getattr(record, field) in values
                    for record in actual_by_id.values()
                )
            ]
            collision_details = []
            if unexpected_ids:
                collision_details.append(f"existing ID {unexpected_ids[0]}")
            elif found_ids:
                collision_details.append(f"existing IDs {found_ids}")
            collision_details.extend(
                f"{field} business-key collision"
                for field in matching_business_keys
            )
            detail = (
                f" ({', '.join(collision_details)})"
                if collision_details
                else ""
            )
            raise DatasetGenerationError(
                f"Existing records conflict with the generated batch in "
                f"{model.__tablename__}{detail}; no records or exports were changed."
            )
        for record_id, expected_record in expected_by_id.items():
            if _model_rows([actual_by_id[record_id]])[0] != _model_rows(
                [expected_record]
            )[0]:
                raise DatasetGenerationError(
                    f"Existing {model.__tablename__} record {record_id} does "
                    "not match the deterministic batch content; no records or "
                    "exports were changed."
                )

    return {
        key: [
            candidates[key][record.id]
            for record in expected_records[key]
        ]
        for key in MODEL_KEYS
    }


def _publish_or_verify_exports(
    stage_directory, batch_directory, records, ground_truth, validation,
    summary,
):
    _export_records(
        stage_directory / "exports",
        {name: _model_rows(rows) for name, rows in records.items()},
        ground_truth,
        validation,
        summary,
    )
    staged_directory = stage_directory / "exports"
    if batch_directory.exists():
        mismatches = [
            name for name in EXPORT_FILES
            if not (batch_directory / name).is_file()
            or (batch_directory / name).read_bytes()
            != (staged_directory / name).read_bytes()
        ]
        if mismatches:
            raise DatasetGenerationError(
                "Existing exports do not match the verified database batch "
                f"({', '.join(mismatches)}); exports were not changed."
            )
        return
    try:
        os.replace(staged_directory, batch_directory)
    except OSError as error:
        raise DatasetGenerationError(
            "The verified database batch is intact, but export recovery failed. "
            "Retry generation with the same configuration."
        ) from error


def _acknowledgement_specs(purchase_order_count, acknowledgement_count):
    specs = [
        (0, 1, "latest", None),
        (0, 2, "latest", None),
        (1, 1, "wrong_po", None),
        (2, 1, "early_date", None),
    ]
    revisions_by_line = {}
    for line_index, _, _, _ in specs:
        revisions_by_line[line_index] = revisions_by_line.get(line_index, 0) + 1

    extra_lines = list(range(3, purchase_order_count))
    for index in range(acknowledgement_count - len(specs)):
        line_index = extra_lines[index % len(extra_lines)]
        revision = revisions_by_line.get(line_index, 0) + 1
        specs.append((line_index, revision, "latest", None))
        revisions_by_line[line_index] = revision
    return specs


def _change_line_index(index, purchase_order_count):
    if index == 0:
        return 3
    eligible_index = (index - 1) % (purchase_order_count - 1)
    return eligible_index if eligible_index < 3 else eligible_index + 1


def _supplier_count(purchase_order_count):
    return min(
        purchase_order_count,
        max(4, math.ceil(purchase_order_count / 20)),
    )


def _model_rows(records):
    rows = []
    for record in records:
        row = {}
        for column in record.__table__.columns:
            value = getattr(record, column.name)
            if isinstance(value, (date, datetime)):
                value = value.isoformat()
            row[column.name] = value
        rows.append(row)
    return rows


def _sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(headers)
        writer.writerows(rows)


def _record_value(record, field):
    if isinstance(record, dict):
        return record[field]
    return getattr(record, field)


def _write_acknowledgement_diagnostics(path, acknowledgements):
    _write_csv(
        path,
        ("Acknowledgement Number", "PO ID", "PO Line ID", "Validation Status"),
        (
            (
                _record_value(row, "acknowledgement_number"),
                _record_value(row, "po_id"),
                _record_value(row, "po_line_id"),
                _record_value(row, "validation_status"),
            )
            for row in acknowledgements
        ),
    )


def _export_records(
    destination,
    records,
    ground_truth,
    validation_summary,
    generation_summary,
):
    destination.mkdir(parents=True, exist_ok=False)
    records = {
        name: sorted(rows, key=lambda row: _record_value(row, "id"))
        for name, rows in records.items()
    }

    suppliers = records["suppliers"]
    orders = records["purchase_orders"]
    lines = records["po_lines"]
    acknowledgements = records["acknowledgements"]
    changes = records["change_requests"]
    approvals = records["approvals"]
    plans = records["inventory_plans"]

    suppliers_by_id = {row["id"]: row for row in suppliers}
    orders_by_id = {row["id"]: row for row in orders}
    feature_records = {
        name: [
            {
                key: value
                for key, value in row.items()
                if key not in {
                    "validation_status",
                    "ground_truth",
                    "issue_type",
                    "scenario_key",
                }
            }
            for row in rows
        ]
        for name, rows in records.items()
    }

    _write_csv(
        destination / "suppliers.csv",
        ("Supplier ID", "Supplier Code", "Supplier Name", "Email", "Phone", "Status"),
        (
            (
                row["id"], row["supplier_code"], row["supplier_name"],
                row["email"], row["phone"], row["status"],
            )
            for row in suppliers
        ),
    )
    _write_csv(
        destination / "purchase_orders.csv",
        ("PO Number", "Supplier", "Order Date", "Status"),
        (
            (
                row["po_number"],
                suppliers_by_id[row["supplier_id"]]["supplier_name"],
                row["order_date"],
                row["status"],
            )
            for row in orders
        ),
    )
    _write_csv(
        destination / "po_lines.csv",
        (
            "PO Line ID", "PO Number", "Item Code", "Description",
            "Requested Quantity", "Unit Price", "Requested Delivery Date",
        ),
        (
            (
                row["id"], orders_by_id[row["po_id"]]["po_number"],
                row["item_code"], row["description"], row["requested_quantity"],
                row["unit_price"], row["requested_delivery_date"],
            )
            for row in lines
        ),
    )
    _write_csv(
        destination / "acknowledgements.csv",
        (
            "Acknowledgement Number", "PO Number", "PO Line ID", "Revision",
            "Accepted Quantity", "Rejected Quantity", "Promised Date",
        ),
        (
            (
                row["acknowledgement_number"],
                orders_by_id[row["po_id"]]["po_number"],
                row["po_line_id"], row["revision_number"],
                row["accepted_quantity"], row["rejected_quantity"],
                row["promised_date"],
            )
            for row in acknowledgements
        ),
    )
    _write_acknowledgement_diagnostics(
        destination / "acknowledgement_diagnostics.csv",
        acknowledgements,
    )
    _write_csv(
        destination / "change_requests.csv",
        (
            "Change Number", "PO Line ID", "Old Quantity", "New Quantity",
            "Old Price", "New Price", "Old Delivery Date",
            "New Delivery Date", "Reason", "Status",
        ),
        (
            (
                row["change_number"], row["po_line_id"], row["old_quantity"],
                row["new_quantity"], row["old_price"], row["new_price"],
                row["old_delivery_date"], row["new_delivery_date"],
                row["reason"], row["status"],
            )
            for row in changes
        ),
    )
    _write_csv(
        destination / "approvals.csv",
        ("Approval ID", "Change Request ID", "Decision", "Reviewer", "Comments", "Decision Date"),
        (
            (
                row["id"], row["change_request_id"], row["decision"],
                row["reviewer"], row["comments"], row["decision_date"],
            )
            for row in approvals
        ),
    )
    _write_csv(
        destination / "inventory_plans.csv",
        (
            "Inventory Plan ID", "PO Line ID", "Projected Quantity",
            "Expected Delivery Date", "Supply Status", "Last Updated",
        ),
        (
            (
                row["id"], row["po_line_id"], row["projected_quantity"],
                row["expected_delivery_date"], row["supply_status"],
                row["last_updated"],
            )
            for row in plans
        ),
    )
    _write_csv(
        destination / "ground_truth.csv",
        ("Record Type", "Record ID", "Anomaly Type", "Ground Truth Label", "Reason"),
        (
            (
                row["record_type"], row["record_id"], row["issue_type"],
                row["ground_truth"], row["reason"],
            )
            for row in ground_truth
        ),
    )

    counts = generation_summary["generated_counts"]
    _write_csv(
        destination / "dataset_summary.csv",
        ("Metric", "Count"),
        ((name.replace("_", " ").title(), count) for name, count in counts.items()),
    )

    dataset = {
        "generation": {
            "batch_id": generation_summary["batch_id"],
            "seed": generation_summary["seed"],
            "purchase_order_count": generation_summary["purchase_order_count"],
            "event_count": generation_summary["event_count"],
        },
        "records": feature_records,
    }
    with (destination / "dataset.json").open("w", encoding="utf-8") as output_file:
        json.dump(dataset, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
    with (destination / "ground_truth.json").open("w", encoding="utf-8") as output_file:
        json.dump(ground_truth, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
    with (destination / "validation_summary.json").open("w", encoding="utf-8") as output_file:
        json.dump(validation_summary, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
    with (destination / "generation_summary.json").open("w", encoding="utf-8") as output_file:
        json.dump(generation_summary, output_file, indent=2, sort_keys=True)
        output_file.write("\n")

    model_order = (
        ("supplier", records["suppliers"]),
        ("purchase_order", records["purchase_orders"]),
        ("po_line", records["po_lines"]),
        ("acknowledgement", records["acknowledgements"]),
        ("change_request", records["change_requests"]),
        ("approval", records["approvals"]),
        ("inventory_plan", records["inventory_plans"]),
    )
    with (destination / "dataset_seed.sql").open("w", encoding="utf-8") as output_file:
        output_file.write(
            "-- Database seed only; not a model-feature export. "
            "Validation status is retained for faithful database reconstruction.\n"
        )
        output_file.write("PRAGMA foreign_keys = ON;\nBEGIN TRANSACTION;\n")
        for table_name, table_rows in model_order:
            for row in table_rows:
                columns = ", ".join(f'"{column}"' for column in row)
                values = ", ".join(_sql_literal(value) for value in row.values())
                output_file.write(
                    f'INSERT INTO "{table_name}" ({columns}) VALUES ({values});\n'
                )
        output_file.write("COMMIT;\n")


def _make_ground_truth(lines, acknowledgements, changes, plans):
    lines_by_id = {line.id: line for line in lines}
    plans_by_line = {plan.po_line_id: plan for plan in plans}
    acknowledgements_by_line = {}
    for acknowledgement in acknowledgements:
        acknowledgements_by_line.setdefault(
            acknowledgement.po_line_id, []
        ).append(acknowledgement)
    for line_acks in acknowledgements_by_line.values():
        line_acks.sort(
            key=lambda row: (row.revision_number, row.id),
            reverse=True,
        )

    candidates = []

    if len(lines) >= 4:
        revision_line = lines[0]
        revisions = [
            row for row in acknowledgements_by_line.get(revision_line.id, [])
            if row.validation_status == "Valid"
        ]
        plan = plans_by_line.get(revision_line.id)
        older_revision = next(
            (
                row for row in revisions[1:]
                if plan
                and row.accepted_quantity == plan.projected_quantity
                and row.promised_date == plan.expected_delivery_date
            ),
            None,
        )
        latest_revision = revisions[0] if revisions else None
        if older_revision and latest_revision:
            candidates.append({
                "scenario_key": "older_revision_overwrite",
                "record_type": "InventoryPlan",
                "record_id": str(plan.id),
                "issue_type": "Older Acknowledgement Revision Overwrote Newer Revision",
                "ground_truth": 1,
                "reason": (
                    f"The plan for PO line {revision_line.id} matches valid "
                    f"acknowledgement revision {older_revision.revision_number}; "
                    f"revision {latest_revision.revision_number} is newer."
                ),
            })

        changes_by_line = {}
        for change in changes:
            changes_by_line.setdefault(change.po_line_id, []).append(change)

        for line in lines:
            plan = plans_by_line.get(line.id)
            line_revisions = [
                row for row in acknowledgements_by_line.get(line.id, [])
                if row.validation_status == "Valid"
            ]
            latest = line_revisions[0] if line_revisions else None
            if plan is None:
                continue
            for change in changes_by_line.get(line.id, ()):
                if (
                    change.status in ("Pending", "Rejected")
                    and plan.projected_quantity == change.new_quantity
                    and (latest is None or plan.projected_quantity != latest.accepted_quantity)
                ):
                    candidates.append({
                        "scenario_key": "unapproved_change_updated_plan",
                        "record_type": "InventoryPlan",
                        "record_id": str(plan.id),
                        "issue_type": "Unapproved Change Incorrectly Updated Inventory Plan",
                        "ground_truth": 1,
                        "reason": (
                            f"The plan for PO line {line.id} contains quantity "
                            f"{plan.projected_quantity} from change request "
                            f"{change.change_number} ({change.status}); it differs "
                            "from the latest valid supplier acknowledgement."
                        ),
                    })
                    break

    for acknowledgement in acknowledgements:
        line = lines_by_id.get(acknowledgement.po_line_id)
        if line and acknowledgement.po_id != line.po_id:
            candidates.append({
                "scenario_key": "wrong_po_line",
                "record_type": "Acknowledgement",
                "record_id": acknowledgement.acknowledgement_number,
                "issue_type": "Acknowledgement Linked to Wrong Purchase Order",
                "ground_truth": 1,
                "reason": (
                    f"The acknowledgement references PO {acknowledgement.po_id}, "
                    f"but PO line {line.id} belongs to PO {line.po_id}."
                ),
            })

        if line:
            order = line.purchase_order
            if (
                order
                and acknowledgement.promised_date is not None
                and acknowledgement.promised_date < order.order_date
            ):
                candidates.append({
                    "scenario_key": "early_promised_date",
                    "record_type": "Acknowledgement",
                    "record_id": acknowledgement.acknowledgement_number,
                    "issue_type": "Promised Date Before Purchase Order Date",
                    "ground_truth": 1,
                    "reason": (
                        f"Promised date {acknowledgement.promised_date} is earlier "
                        f"than PO {order.po_number}'s order date {order.order_date}."
                    ),
                })

    expected_order = (
        "unapproved_change_updated_plan",
        "wrong_po_line",
        "early_promised_date",
        "older_revision_overwrite",
    )
    result = []
    for scenario_key in expected_order:
        match = next(
            (
                row for row in candidates
                if row["scenario_key"] == scenario_key
            ),
            None,
        )
        if match:
            result.append(match)
    return result


def _validate_batch(records, ground_truth, models, expected_event_count):
    errors = []
    suppliers = records["suppliers"]
    orders = records["purchase_orders"]
    lines = records["po_lines"]
    acknowledgements = records["acknowledgements"]
    changes = records["change_requests"]
    approvals = records["approvals"]
    plans = records["inventory_plans"]

    def fail(message):
        errors.append(message)

    all_records = (
        suppliers + orders + lines + acknowledgements + changes + approvals + plans
    )
    for model_name, model_records in (
        ("supplier", suppliers),
        ("purchase_order", orders),
        ("po_line", lines),
        ("acknowledgement", acknowledgements),
        ("change_request", changes),
        ("approval", approvals),
        ("inventory_plan", plans),
    ):
        ids = [row.id for row in model_records]
        if len(ids) != len(set(ids)):
            fail(f"{model_name} primary keys are not unique.")

    supplier_ids = {row.id for row in suppliers}
    order_ids = {row.id for row in orders}
    line_ids = {row.id for row in lines}
    change_ids = {row.id for row in changes}
    line_by_id = {row.id: row for row in lines}
    orders_by_id = {row.id: row for row in orders}
    approvals_by_change = {}
    for approval in approvals:
        approvals_by_change.setdefault(approval.change_request_id, []).append(approval)

    if len(acknowledgements) + len(changes) != expected_event_count:
        fail("Combined acknowledgement and change-request count is incorrect.")
    if len(orders) != len(lines) or len(plans) != len(lines):
        fail("Generated PO lines and inventory plans do not match PO count.")

    if any(row.status != "Active" for row in suppliers):
        fail("A generated supplier has an unsupported status.")
    if any(row.status != "Created" for row in orders):
        fail("A generated purchase order has an unsupported status.")

    if len(orders) != len(records["purchase_orders"]):
        fail("Generated purchase order count is inconsistent.")
    for order in orders:
        if order.supplier_id not in supplier_ids:
            fail(f"PO {order.po_number} references a missing supplier.")
    for line in lines:
        order = orders_by_id.get(line.po_id)
        if order is None:
            fail(f"PO line {line.id} references a missing purchase order.")
        elif line.requested_delivery_date < order.order_date:
            fail(f"PO line {line.id} has delivery date before its order date.")
        if line.requested_quantity <= 0 or line.unit_price < 0:
            fail(f"PO line {line.id} has invalid quantity or price.")

    expected_gt = {row["scenario_key"]: row for row in ground_truth}
    wrong_ack_numbers = {
        row["record_id"] for row in ground_truth
        if row["scenario_key"] == "wrong_po_line"
    }
    early_ack_numbers = {
        row["record_id"] for row in ground_truth
        if row["scenario_key"] == "early_promised_date"
    }
    for ack in acknowledgements:
        line = line_by_id.get(ack.po_line_id)
        if line is None or ack.po_id not in order_ids:
            fail(f"Acknowledgement {ack.acknowledgement_number} has a missing FK.")
            continue
        if (ack.po_id != line.po_id) != (ack.acknowledgement_number in wrong_ack_numbers):
            fail(f"Acknowledgement {ack.acknowledgement_number} has an unexpected PO/line relationship.")
        if ack.validation_status not in ("Valid", "Invalid"):
            fail(f"Acknowledgement {ack.acknowledgement_number} has an unsupported status.")
        if ack.accepted_quantity < 0 or ack.rejected_quantity < 0:
            fail(f"Acknowledgement {ack.acknowledgement_number} has a negative quantity.")
        if ack.accepted_quantity + ack.rejected_quantity > line.requested_quantity:
            fail(f"Acknowledgement {ack.acknowledgement_number} does not reconcile to its PO line.")
        if ack.promised_date is not None:
            order = orders_by_id.get(line.po_id)
            is_expected_early_date = ack.acknowledgement_number in early_ack_numbers
            if order and (ack.promised_date < order.order_date) != is_expected_early_date:
                fail(f"Acknowledgement {ack.acknowledgement_number} has unexpected date ordering.")

    revisions = set()
    for ack in acknowledgements:
        key = (ack.po_line_id, ack.revision_number)
        if key in revisions:
            fail(f"PO line {ack.po_line_id} has a duplicate acknowledgement revision.")
        revisions.add(key)

    for change in changes:
        line = line_by_id.get(change.po_line_id)
        if line is None:
            fail(f"Change request {change.change_number} references a missing PO line.")
            continue
        if change.status not in APPROVAL_STATUSES:
            fail(f"Change request {change.change_number} has an unsupported status.")
        if (
            change.new_quantity is None or change.new_quantity <= 0
            or change.new_price is None or change.new_price < 0
            or change.new_delivery_date is None
        ):
            fail(f"Change request {change.change_number} has invalid proposed values.")
        if change.new_delivery_date and change.new_delivery_date < line.purchase_order.order_date:
            fail(f"Change request {change.change_number} has delivery date before its PO date.")
        linked_approvals = approvals_by_change.get(change.id, [])
        if change.status in ("Approved", "Rejected"):
            if (
                len(linked_approvals) != 1
                or linked_approvals[0].decision != change.status
            ):
                fail(
                    f"Processed change request {change.change_number} lacks "
                    "exactly one matching approval."
                )
        elif linked_approvals:
            fail(f"Unapproved change request {change.change_number} has an approval record.")

    for approval in approvals:
        if approval.change_request_id not in change_ids:
            fail(f"Approval {approval.id} references a missing change request.")
        if approval.decision not in ("Approved", "Rejected"):
            fail(f"Approval {approval.id} has an unsupported decision.")

    plans_by_line = {}
    for plan in plans:
        if plan.po_line_id not in line_ids:
            fail(f"Inventory plan {plan.id} references a missing PO line.")
        if plan.po_line_id in plans_by_line:
            fail(f"PO line {plan.po_line_id} has duplicate inventory plans.")
        plans_by_line[plan.po_line_id] = plan
        if plan.projected_quantity is not None and plan.projected_quantity < 0:
            fail(f"Inventory plan {plan.id} has a negative quantity.")
        if plan.supply_status not in (
            "Awaiting Acknowledgement", "Confirmed", "Needs Review"
        ):
            fail(f"Inventory plan {plan.id} has an unsupported status.")
    if set(plans_by_line) != line_ids:
        fail("Every generated PO line must have exactly one inventory plan.")

    allowed_projection_exceptions = {}
    for key in ("unapproved_change_updated_plan", "older_revision_overwrite"):
        row = expected_gt.get(key)
        if row:
            plan_id = int(row["record_id"])
            allowed_projection_exceptions[plan_id] = key

    valid_acks_by_line = {}
    for ack in acknowledgements:
        if ack.validation_status == "Valid":
            valid_acks_by_line.setdefault(ack.po_line_id, []).append(ack)
    for line_id, plan in plans_by_line.items():
        line_acks = valid_acks_by_line.get(line_id, [])
        latest = max(
            line_acks,
            key=lambda row: (row.revision_number, row.id),
            default=None,
        )
        line = line_by_id[line_id]
        expected_quantity = latest.accepted_quantity if latest else 0
        expected_date = latest.promised_date if latest else None
        expected_status = (
            "Awaiting Acknowledgement"
            if latest is None
            else (
                "Needs Review"
                if latest.accepted_quantity > line.requested_quantity
                else "Confirmed"
            )
        )
        if plan.id in allowed_projection_exceptions:
            scenario_key = allowed_projection_exceptions[plan.id]
            if (
                scenario_key == "unapproved_change_updated_plan"
                and plan.projected_quantity == expected_quantity
            ):
                fail("The labeled unapproved-change projection is not anomalous.")
            if (
                scenario_key == "older_revision_overwrite"
                and not any(
                    ack.validation_status == "Valid"
                    and ack.po_line_id == line_id
                    and ack.revision_number
                    < max(
                        candidate.revision_number
                        for candidate in valid_acks_by_line.get(line_id, [])
                    )
                    and ack.accepted_quantity == plan.projected_quantity
                    and ack.promised_date == plan.expected_delivery_date
                    for ack in valid_acks_by_line.get(line_id, [])
                )
            ):
                fail("The labeled older-revision projection does not match an older acknowledgement.")
            continue
        if (
            plan.projected_quantity != expected_quantity
            or plan.expected_delivery_date != expected_date
            or plan.supply_status != expected_status
        ):
            fail(f"Inventory plan {plan.id} does not match the latest valid acknowledgement.")

    expected_scenarios = {
        "unapproved_change_updated_plan",
        "wrong_po_line",
        "early_promised_date",
        "older_revision_overwrite",
    }
    if {row["scenario_key"] for row in ground_truth} != expected_scenarios:
        fail("Ground truth does not contain exactly the four required verified scenarios.")
    if any(row["ground_truth"] != 1 or not row["issue_type"] for row in ground_truth):
        fail("Ground-truth labels are missing a positive label or issue type.")

    try:
        fk_rows = models["db"].session.execute(
            text("PRAGMA foreign_key_check")
        ).all()
    except Exception as error:
        fail(f"Could not run SQLite foreign-key validation: {error}")
    else:
        if fk_rows:
            fail(f"SQLite reported {len(fk_rows)} foreign-key violation(s).")

    return {
        "passed": not errors,
        "errors": errors,
        "checks": {
            "primary_keys_unique": not any("primary keys" in error for error in errors),
            "foreign_keys_valid": not any("foreign" in error.lower() for error in errors),
            "statuses_valid": not any("status" in error.lower() or "decision" in error.lower() for error in errors),
            "dates_valid_except_labeled_scenario": not any("date" in error.lower() for error in errors),
            "quantities_reconcile": not any("quantity" in error.lower() or "reconcile" in error.lower() for error in errors),
            "acknowledgement_revisions_unique": not any("revision" in error.lower() for error in errors),
            "inventory_projection_rules_checked": not any("inventory plan" in error.lower() for error in errors),
            "ground_truth_scenarios_verified": not any("ground truth" in error.lower() or "labeled" in error.lower() for error in errors),
        },
        "record_count": len(all_records),
    }


def generate_synthetic_dataset(
    db,
    models,
    purchase_order_count,
    event_count,
    seed,
    output_root,
):
    if not MIN_PURCHASE_ORDER_COUNT <= purchase_order_count <= MAX_PURCHASE_ORDER_COUNT:
        raise ValueError(
            f"Purchase order count must be between {MIN_PURCHASE_ORDER_COUNT} "
            f"and {MAX_PURCHASE_ORDER_COUNT}."
        )
    if not MIN_EVENT_COUNT <= event_count <= MAX_EVENT_COUNT:
        raise ValueError(
            f"Event count must be between {MIN_EVENT_COUNT} and {MAX_EVENT_COUNT}."
        )

    batch = _batch_id(seed, purchase_order_count, event_count)
    batch_prefix = batch[:8].upper()
    suppliers_model = models["Supplier"]
    order_model = models["PurchaseOrder"]
    line_model = models["POLine"]
    ack_model = models["Acknowledgement"]
    change_model = models["ChangeRequest"]
    approval_model = models["Approval"]
    plan_model = models["InventoryPlan"]
    supplier_count = _supplier_count(purchase_order_count)
    acknowledgement_count = (event_count + 1) // 2
    change_count = event_count // 2
    approval_count = sum(
        APPROVAL_STATUSES[(index - 1) % len(APPROVAL_STATUSES)] != "Pending"
        for index in range(1, change_count)
    )
    id_maps = _build_id_maps(
        seed,
        purchase_order_count,
        event_count,
        models,
        {
            "suppliers": supplier_count,
            "purchase_orders": purchase_order_count,
            "po_lines": purchase_order_count,
            "acknowledgements": acknowledgement_count,
            "change_requests": change_count,
            "approvals": approval_count,
            "inventory_plans": purchase_order_count,
        },
    )
    po_serial_start = 1 + (
        int(batch[:8], 16) % (1000000 - purchase_order_count)
    )
    po_numbers = [
        f"PO-2026-{po_serial_start + index:06d}"
        for index in range(purchase_order_count)
    ]
    supplier_codes = [
        f"SUP-{batch_prefix}-{index + 1:04d}"
        for index in range(supplier_count)
    ]
    change_serial_start = 1 + (
        int(batch[4:12], 16) % (1000000 - event_count)
    )
    change_numbers = [
        f"CHG-2026-{change_serial_start + index:06d}"
        for index in range(event_count - (event_count + 1) // 2)
    ]
    output_root = Path(output_root)
    batch_directory = output_root / f"module8_{batch}"
    stage_directory = None

    session = db.session
    try:
        session.execute(text("PRAGMA foreign_keys = ON"))
        rng = random.Random(seed)
        base_timestamp = datetime(2026, 1, 1, 9, 0)
        suppliers = []
        for index, code in enumerate(supplier_codes):
            contact = CONTACT_NAMES[index % len(CONTACT_NAMES)]
            descriptor = COMPANY_DESCRIPTORS[
                (index // len(CONTACT_NAMES)) % len(COMPANY_DESCRIPTORS)
            ]
            suffix = COMPANY_SUFFIXES[
                (index // (len(CONTACT_NAMES) * len(COMPANY_DESCRIPTORS)))
                % len(COMPANY_SUFFIXES)
            ]
            first, last = contact.lower().split()
            domain = f"{first}-{descriptor.split()[0].lower()}-{index + 1:04d}"
            suppliers.append(suppliers_model(
                id=id_maps["suppliers"][index],
                supplier_code=code,
                supplier_name=f"{contact.split()[0]} {descriptor} {suffix}",
                email=f"{first}.{last}@{domain}.example",
                phone=f"+91-80-{rng.randint(20000000, 99999999)}",
                status="Active",
            ))
        orders = []
        lines = []
        for index in range(purchase_order_count):
            order_date = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1000))
            requested_date = order_date + timedelta(days=rng.randint(35, 100))
            order = order_model(
                id=id_maps["purchase_orders"][index],
                po_number=po_numbers[index],
                supplier_id=id_maps["suppliers"][index % len(suppliers)],
                order_date=order_date,
                status="Created",
            )
            orders.append(order)
            product_code, description = PRODUCTS[index % len(PRODUCTS)]
            quantity = rng.randint(50, 900)
            price = round(rng.randint(125, 250000) / 100, 2)
            line = line_model(
                id=id_maps["po_lines"][index],
                po_id=id_maps["purchase_orders"][index],
                item_code=f"{product_code}-{index + 1:06d}",
                description=description,
                requested_quantity=quantity,
                unit_price=price,
                requested_delivery_date=requested_date,
            )
            line.purchase_order = order
            lines.append(line)

        ack_specs = _acknowledgement_specs(
            purchase_order_count,
            acknowledgement_count,
        )

        acknowledgements = []
        for event_index, (line_index, revision, scenario, _) in enumerate(ack_specs):
            line = lines[line_index]
            accepted = max(1, int(line.requested_quantity * (0.32 + rng.random() * 0.35)))
            rejected = max(
                1,
                min(
                    line.requested_quantity - accepted,
                    int(line.requested_quantity * rng.random() * 0.2),
                ),
            )
            promised_date = line.requested_delivery_date + timedelta(
                days=rng.randint(0, 25)
            )
            po_id = id_maps["purchase_orders"][line_index]
            validation_status = "Valid"
            if scenario == "wrong_po":
                po_id = id_maps["purchase_orders"][3]
                validation_status = "Invalid"
            elif scenario == "early_date":
                promised_date = orders[line_index].order_date - timedelta(days=1)
                validation_status = "Invalid"

            ack = ack_model(
                id=id_maps["acknowledgements"][event_index],
                acknowledgement_number=(
                    f"ACK-{id_maps['po_lines'][line_index]}-{revision:02d}"
                ),
                po_id=po_id,
                po_line_id=id_maps["po_lines"][line_index],
                revision_number=revision,
                accepted_quantity=accepted,
                rejected_quantity=rejected,
                promised_date=promised_date,
                validation_status=validation_status,
                supplier_note=(
                    f"Supplier contact: "
                    f"{CONTACT_NAMES[line_index % len(CONTACT_NAMES)]}"
                ),
                recorded_date=base_timestamp + timedelta(minutes=event_index),
            )
            acknowledgements.append(ack)

        changes = []
        approvals = []
        scenario_change = None
        for index in range(change_count):
            if index == 0:
                line_index = _change_line_index(index, purchase_order_count)
                status = "Pending"
                change_type = "quantity"
            else:
                line_index = _change_line_index(index, purchase_order_count)
                status = APPROVAL_STATUSES[(index - 1) % len(APPROVAL_STATUSES)]
                change_type = ("quantity", "price", "delivery_date")[
                    (index - 1) % 3
                ]

            line = lines[line_index]
            old_quantity = line.requested_quantity
            old_price = line.unit_price
            old_delivery_date = line.requested_delivery_date
            new_quantity = old_quantity
            new_price = old_price
            new_delivery_date = old_delivery_date
            if change_type == "quantity":
                new_quantity += rng.randint(10, 80)
            elif change_type == "price":
                new_price = round(old_price * (1 + rng.randint(2, 15) / 100), 2)
            else:
                new_delivery_date += timedelta(days=rng.randint(7, 45))

            change = change_model(
                id=id_maps["change_requests"][index],
                change_number=change_numbers[index],
                po_line_id=line.id,
                old_quantity=old_quantity,
                new_quantity=new_quantity,
                old_price=old_price,
                new_price=new_price,
                old_delivery_date=old_delivery_date,
                new_delivery_date=new_delivery_date,
                reason=CHANGE_REASONS[index % len(CHANGE_REASONS)],
                status=status,
                created_date=base_timestamp + timedelta(days=1, minutes=index),
            )
            changes.append(change)
            if index == 0:
                scenario_change = change
            if status in ("Approved", "Rejected"):
                approval = approval_model(
                    id=id_maps["approvals"][len(approvals)],
                    change_request_id=change.id,
                    decision=status,
                    reviewer=REVIEWERS[index % len(REVIEWERS)],
                    comments=f"{status} in the generated planning scenario.",
                    decision_date=base_timestamp + timedelta(days=2, minutes=index),
                )
                approvals.append(approval)
            if status == "Approved":
                line.requested_quantity = new_quantity
                line.unit_price = new_price
                line.requested_delivery_date = new_delivery_date

        acks_by_line = {}
        for ack in acknowledgements:
            acks_by_line.setdefault(ack.po_line_id, []).append(ack)
        plans = []
        for line_index, line in enumerate(lines):
            valid_acks = [
                ack for ack in acks_by_line.get(line.id, [])
                if ack.validation_status == "Valid"
            ]
            latest = max(
                valid_acks,
                key=lambda row: (row.revision_number, row.id),
                default=None,
            )
            plans.append(plan_model(
                id=id_maps["inventory_plans"][line_index],
                po_line_id=id_maps["po_lines"][line_index],
                projected_quantity=latest.accepted_quantity if latest else 0,
                expected_delivery_date=latest.promised_date if latest else None,
                supply_status=(
                    "Awaiting Acknowledgement"
                    if latest is None
                    else (
                        "Needs Review"
                        if latest.accepted_quantity > line.requested_quantity
                        else "Confirmed"
                    )
                ),
                last_updated=base_timestamp + timedelta(days=3),
            ))

        plans_by_line = {plan.po_line_id: plan for plan in plans}
        line3_plan = plans_by_line[id_maps["po_lines"][3]]
        line3_plan.projected_quantity = scenario_change.new_quantity
        line3_plan.expected_delivery_date = scenario_change.new_delivery_date
        line3_plan.supply_status = "Confirmed"

        line0_acks = sorted(
            acks_by_line[id_maps["po_lines"][0]],
            key=lambda row: (row.revision_number, row.id),
        )
        line0_plan = plans_by_line[id_maps["po_lines"][0]]
        line0_plan.projected_quantity = line0_acks[0].accepted_quantity
        line0_plan.expected_delivery_date = line0_acks[0].promised_date
        line0_plan.supply_status = "Confirmed"

        records = {
            "suppliers": suppliers,
            "purchase_orders": orders,
            "po_lines": lines,
            "acknowledgements": acknowledgements,
            "change_requests": changes,
            "approvals": approvals,
            "inventory_plans": plans,
        }
        existing_records = _identify_existing_batch(session, models, records)
        if existing_records is not None:
            records = existing_records
            ground_truth = _make_ground_truth(
                records["po_lines"],
                records["acknowledgements"],
                records["change_requests"],
                records["inventory_plans"],
            )
            validation = _validate_batch(
                records, ground_truth, models, event_count
            )
            if not validation["passed"]:
                raise DatasetValidationError(validation["errors"])
            summary = _generation_summary(
                batch, seed, purchase_order_count, event_count, records,
                ground_truth, validation, supplier_count,
            )
            output_root.mkdir(parents=True, exist_ok=True)
            stage_directory = Path(tempfile.mkdtemp(
                prefix=f".{batch_directory.name}_",
                dir=str(output_root),
            ))
            _publish_or_verify_exports(
                stage_directory,
                batch_directory,
                records,
                ground_truth,
                validation,
                summary,
            )
            return {
                "batch_id": batch,
                "directory": batch_directory.name,
                "files": list(EXPORT_FILES),
                "summary": summary,
                "created": False,
            }

        if batch_directory.exists():
            raise DatasetGenerationError(
                "Exports for this batch exist without a matching complete "
                "database batch; exports were not changed."
            )

        ground_truth = _make_ground_truth(
            lines, acknowledgements, changes, plans
        )
        validation = _validate_batch(
            records, ground_truth, models, event_count
        )
        if not validation["passed"]:
            raise DatasetValidationError(validation["errors"])

        output_root.mkdir(parents=True, exist_ok=True)
        session.add_all(suppliers)
        session.add_all(orders)
        session.add_all(lines)
        session.add_all(acknowledgements)
        session.add_all(changes)
        session.add_all(approvals)
        session.add_all(plans)
        session.flush()

        summary = _generation_summary(
            batch, seed, purchase_order_count, event_count, records,
            ground_truth, validation, supplier_count,
        )
        stage_directory = Path(tempfile.mkdtemp(
            prefix=f".{batch_directory.name}_",
            dir=str(output_root),
        ))
        _export_records(
            stage_directory / "exports",
            {name: _model_rows(rows) for name, rows in records.items()},
            ground_truth,
            validation,
            summary,
        )

        # Database commit and directory publication are not atomic. A retry
        # verifies complete batch contents and can publish the missing exports.
        session.commit()
        try:
            os.replace(stage_directory / "exports", batch_directory)
        except OSError as error:
            raise DatasetGenerationError(
                "The database batch committed, but export publication failed. "
                "Retry generation with the same configuration to recover exports."
            ) from error
        return {
            "batch_id": batch,
            "directory": batch_directory.name,
            "files": list(EXPORT_FILES),
            "summary": summary,
            "created": True,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        if stage_directory and stage_directory.exists():
            shutil.rmtree(stage_directory)


def _generation_summary(
    batch, seed, purchase_order_count, event_count, records,
    ground_truth, validation, supplier_count,
):
    counts = {
        "suppliers": len(records["suppliers"]),
        "purchase_orders": len(records["purchase_orders"]),
        "po_lines": len(records["po_lines"]),
        "acknowledgements": len(records["acknowledgements"]),
        "change_requests": len(records["change_requests"]),
        "approvals": len(records["approvals"]),
        "inventory_plans": len(records["inventory_plans"]),
        "combined_events": (
            len(records["acknowledgements"]) + len(records["change_requests"])
        ),
        "ground_truth_labels": len(ground_truth),
        "po_lines_without_acknowledgement": (
            purchase_order_count
            - len({
                row.po_line_id
                for row in records["acknowledgements"]
            })
        ),
    }
    return {
        "batch_id": batch,
        "seed": seed,
        "purchase_order_count": purchase_order_count,
        "event_count": event_count,
        "supplier_count": supplier_count,
        "generated_counts": counts,
        "validation": validation,
        "generation_event": {
            "event_type": "Dataset",
            "description": "Synthetic Dataset Generated",
            "batch_id": batch,
            "seed": seed,
            "generated_counts": counts,
        },
    }


def build_records_for_exports(records):
    return {name: _model_rows(rows) for name, rows in records.items()}


def export_existing_batch(
    records, ground_truth, validation_summary, generation_summary,
    output_root, batch_id,
):
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    batch_directory = output_root / f"module8_{batch_id}"
    stage_directory = Path(tempfile.mkdtemp(
        prefix=f".{batch_directory.name}_",
        dir=str(output_root),
    ))
    try:
        _publish_or_verify_exports(
            stage_directory,
            batch_directory,
            records,
            ground_truth,
            validation_summary,
            generation_summary,
        )
    finally:
        if stage_directory.exists():
            shutil.rmtree(stage_directory)
    return batch_directory
