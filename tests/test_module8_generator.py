import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import (
    Acknowledgement,
    Approval,
    AuditLog,
    ChangeRequest,
    InventoryPlan,
    POLine,
    PurchaseOrder,
    Supplier,
    create_app,
    db,
)
from module8_generator import (
    DatasetGenerationError,
    EXPORT_FILES,
    _batch_id,
    _model_rows,
    _acknowledgement_specs,
    _change_line_index,
    _deterministic_id,
    generate_synthetic_dataset,
)


class Module8GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.output_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_root.cleanup)
        self.database_path = Path(self.output_root.name, "yottaflex-test.db")
        database_uri = f"sqlite:///{self.database_path.as_posix()}"
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": database_uri,
            "SECRET_KEY": "module8-test-only",
            "GENERATED_DATA_ROOT": str(Path(self.output_root.name, "generated_data")),
        })
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.addCleanup(self.app_context.pop)
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)

        original_database_path = (
            Path(__file__).resolve().parents[1] / "instance" / "yottaflex.db"
        ).resolve()
        active_database_path = Path(db.engine.url.database).resolve()
        self.assertEqual(active_database_path, self.database_path.resolve())
        self.assertNotEqual(active_database_path, original_database_path)
        self.assertEqual(
            self.app.config["SQLALCHEMY_DATABASE_URI"],
            database_uri,
        )

        db.create_all()
        self.client = self.app.test_client()

    def generate(self, seed=20261022, purchase_orders=8, events=16):
        return self.client.post(
            "/data-generator",
            data={
                "purchase_order_count": str(purchase_orders),
                "event_count": str(events),
                "seed": str(seed),
            },
        )

    def batch_directory(self, response):
        batch_id = response.get_data(as_text=True).split("Batch: ", 1)[1].split("<", 1)[0].strip()
        return batch_id, Path(
            self.output_root.name,
            "generated_data",
            f"module8_{batch_id}",
        )

    def generator_models(self):
        return {
            "db": db,
            "Supplier": Supplier,
            "PurchaseOrder": PurchaseOrder,
            "POLine": POLine,
            "Acknowledgement": Acknowledgement,
            "ChangeRequest": ChangeRequest,
            "Approval": Approval,
            "InventoryPlan": InventoryPlan,
        }

    def test_deterministic_ids_are_stable_positive_sqlite_integers(self):
        for model in (
            Supplier,
            PurchaseOrder,
            POLine,
            Acknowledgement,
            ChangeRequest,
            Approval,
            InventoryPlan,
        ):
            first = _deterministic_id(
                20261022,
                10000,
                15000,
                model.__tablename__,
                0,
            )
            repeated = _deterministic_id(
                20261022,
                10000,
                15000,
                model.__tablename__,
                0,
            )
            self.assertEqual(first, repeated)
            self.assertIsInstance(first, int)
            self.assertGreaterEqual(first, 1)
            self.assertLessEqual(first, (1 << 63) - 1)

    def test_identical_exports_in_independent_databases_with_unrelated_records(self):
        original_database_path = (
            Path(__file__).resolve().parents[1] / "instance" / "yottaflex.db"
        ).resolve()

        def generate_in_isolated_database(root, suffix, contact):
            root.mkdir(parents=True, exist_ok=True)
            database_path = (root / "isolated.db").resolve()
            generated_root = root / "generated_data"
            database_uri = f"sqlite:///{database_path.as_posix()}"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": database_uri,
                "SECRET_KEY": "module8-reproducibility-test-only",
                "GENERATED_DATA_ROOT": str(generated_root),
            })

            with app.app_context():
                active_path = Path(db.engine.url.database).resolve()
                self.assertEqual(active_path, database_path)
                self.assertNotEqual(active_path, original_database_path)
                db.create_all()
                supplier = Supplier(
                    supplier_code=f"SUP-UNRELATED-{suffix}",
                    supplier_name=f"{contact} Precision Components Pvt Ltd",
                    email=f"{suffix.lower()}@supplier.example",
                    phone="+91-80-23456789",
                    status="Active",
                )
                db.session.add(supplier)
                db.session.flush()
                db.session.add(PurchaseOrder(
                    po_number=f"PO-UNRELATED-{suffix}",
                    supplier_id=supplier.id,
                    order_date=date(2025, 6, 15),
                    status="Created",
                ))
                db.session.commit()

                def unrelated_state():
                    suppliers = tuple(
                        (
                            row.id, row.supplier_code, row.supplier_name,
                            row.email, row.phone, row.status,
                        )
                        for row in Supplier.query.order_by(Supplier.id).all()
                        if row.supplier_code.startswith("SUP-UNRELATED-")
                    )
                    orders = tuple(
                        (
                            row.id, row.po_number, row.supplier_id,
                            row.order_date, row.status,
                        )
                        for row in PurchaseOrder.query.order_by(PurchaseOrder.id).all()
                        if row.po_number.startswith("PO-UNRELATED-")
                    )
                    return suppliers, orders

                before = unrelated_state()
                response = app.test_client().post(
                    "/data-generator",
                    data={
                        "purchase_order_count": "8",
                        "event_count": "16",
                        "seed": "20261022",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"was generated, validated, saved, and exported", response.data)
                batch_id = _batch_id(20261022, 8, 16)
                batch_directory = generated_root / f"module8_{batch_id}"
                self.assertEqual(
                    {path.name for path in batch_directory.iterdir()},
                    set(EXPORT_FILES),
                )
                hashes = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in batch_directory.iterdir()
                }
                after = unrelated_state()
                self.assertEqual(before, after)
                db.session.remove()
                db.engine.dispose()
                return database_path, hashes

        first_path, first_hashes = generate_in_isolated_database(
            Path(self.output_root.name) / "database-a",
            "A",
            "Akshu Sharma",
        )
        second_path, second_hashes = generate_in_isolated_database(
            Path(self.output_root.name) / "database-b",
            "B",
            "Rekha Iyer",
        )
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_hashes, second_hashes)

    def test_database_records_relationships_and_expected_counts(self):
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"was generated, validated, saved, and exported", response.data)
        self.assertEqual(Supplier.query.count(), 4)
        self.assertEqual(PurchaseOrder.query.count(), 8)
        self.assertEqual(POLine.query.count(), 8)
        self.assertEqual(Acknowledgement.query.count(), 8)
        self.assertEqual(ChangeRequest.query.count(), 8)
        self.assertEqual(InventoryPlan.query.count(), 8)
        self.assertEqual(
            Acknowledgement.query.count() + ChangeRequest.query.count(),
            16,
        )
        self.assertGreater(Approval.query.count(), 0)

        supplier_ids = {row.id for row in Supplier.query.all()}
        order_ids = {row.id for row in PurchaseOrder.query.all()}
        line_ids = {row.id for row in POLine.query.all()}
        change_ids = {row.id for row in ChangeRequest.query.all()}
        self.assertTrue(all(row.supplier_id in supplier_ids for row in PurchaseOrder.query.all()))
        self.assertTrue(all(row.po_id in order_ids for row in POLine.query.all()))
        self.assertTrue(all(row.po_line_id in line_ids for row in Acknowledgement.query.all()))
        self.assertTrue(all(row.po_id in order_ids for row in Acknowledgement.query.all()))
        self.assertTrue(all(row.po_line_id in line_ids for row in ChangeRequest.query.all()))
        self.assertTrue(all(row.change_request_id in change_ids for row in Approval.query.all()))
        self.assertTrue(all(row.po_line_id in line_ids for row in InventoryPlan.query.all()))
        self.assertEqual(
            db.session.execute(db.text("PRAGMA foreign_key_check")).all(),
            [],
        )

        def expected_ids(model, count):
            return [
                _deterministic_id(
                    20261022,
                    8,
                    16,
                    model.__tablename__,
                    index,
                )
                for index in range(count)
            ]

        for index, supplier in enumerate(
            sorted(Supplier.query.all(), key=lambda row: row.supplier_code)
        ):
            self.assertEqual(supplier.id, expected_ids(Supplier, 4)[index])
        orders_by_number = sorted(
            PurchaseOrder.query.all(), key=lambda row: row.po_number
        )
        for index, order in enumerate(orders_by_number):
            self.assertEqual(order.id, expected_ids(PurchaseOrder, 8)[index])
            self.assertEqual(
                order.supplier_id,
                expected_ids(Supplier, 4)[index % 4],
            )
        for line in POLine.query.all():
            index = int(line.item_code.rsplit("-", 1)[1]) - 1
            self.assertEqual(line.id, expected_ids(POLine, 8)[index])
            self.assertEqual(line.po_id, expected_ids(PurchaseOrder, 8)[index])
        self.assertEqual(
            {row.id for row in Acknowledgement.query.all()},
            set(expected_ids(Acknowledgement, 8)),
        )
        for index, (line_index, revision, scenario, _) in enumerate(
            _acknowledgement_specs(8, 8)
        ):
            acknowledgement = db.session.get(
                Acknowledgement,
                expected_ids(Acknowledgement, 8)[index],
            )
            expected_po_id = (
                expected_ids(PurchaseOrder, 8)[3]
                if scenario == "wrong_po"
                else expected_ids(PurchaseOrder, 8)[line_index]
            )
            self.assertEqual(
                acknowledgement.po_line_id,
                expected_ids(POLine, 8)[line_index],
            )
            self.assertEqual(acknowledgement.po_id, expected_po_id)
            self.assertEqual(acknowledgement.revision_number, revision)
        self.assertEqual(
            {row.id for row in ChangeRequest.query.all()},
            set(expected_ids(ChangeRequest, 8)),
        )
        for index, change in enumerate(
            sorted(ChangeRequest.query.all(), key=lambda row: row.change_number)
        ):
            self.assertEqual(change.id, expected_ids(ChangeRequest, 8)[index])
            self.assertEqual(
                change.po_line_id,
                expected_ids(POLine, 8)[_change_line_index(index, 8)],
            )
        self.assertEqual(
            {row.id for row in Approval.query.all()},
            set(expected_ids(Approval, Approval.query.count())),
        )
        self.assertEqual(
            {row.id for row in InventoryPlan.query.all()},
            set(expected_ids(InventoryPlan, 8)),
        )
        line_indexes = {
            line_id: index
            for index, line_id in enumerate(expected_ids(POLine, 8))
        }
        for plan in InventoryPlan.query.all():
            index = line_indexes[plan.po_line_id]
            self.assertEqual(plan.id, expected_ids(InventoryPlan, 8)[index])

        self.assertTrue(any("Akshu" in row.supplier_name for row in Supplier.query.all()))
        self.assertTrue(any("Rekha" in row.supplier_name for row in Supplier.query.all()))
        self.assertTrue(all(row.status == "Active" for row in Supplier.query.all()))
        self.assertEqual(
            {row.status for row in ChangeRequest.query.all()},
            {"Pending", "Approved", "Rejected"},
        )
        self.assertTrue(all(row.accepted_quantity > 0 for row in Acknowledgement.query.all()))
        self.assertTrue(any(
            ack.accepted_quantity + ack.rejected_quantity
            < ack.purchase_order_line.requested_quantity
            for ack in Acknowledgement.query.all()
        ))

    def test_ground_truth_labels_match_database_scenarios_and_are_separate(self):
        response = self.generate()
        _, directory = self.batch_directory(response)
        labels = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
        issue_types = {row["issue_type"] for row in labels}
        self.assertEqual(len(labels), 4)
        self.assertEqual(
            issue_types,
            {
                "Unapproved Change Incorrectly Updated Inventory Plan",
                "Acknowledgement Linked to Wrong Purchase Order",
                "Promised Date Before Purchase Order Date",
                "Older Acknowledgement Revision Overwrote Newer Revision",
            },
        )
        self.assertTrue(all(row["ground_truth"] == 1 for row in labels))

        for label in labels:
            if label["scenario_key"] == "unapproved_change_updated_plan":
                plan = db.session.get(InventoryPlan, int(label["record_id"]))
                change = ChangeRequest.query.filter_by(
                    po_line_id=plan.po_line_id,
                    status="Pending",
                    new_quantity=plan.projected_quantity,
                ).one()
                self.assertIsNotNone(change)
            elif label["scenario_key"] == "wrong_po_line":
                ack = Acknowledgement.query.filter_by(
                    acknowledgement_number=label["record_id"]
                ).one()
                self.assertNotEqual(
                    ack.po_id,
                    ack.purchase_order_line.po_id,
                )
            elif label["scenario_key"] == "early_promised_date":
                ack = Acknowledgement.query.filter_by(
                    acknowledgement_number=label["record_id"]
                ).one()
                self.assertLess(
                    ack.promised_date,
                    ack.purchase_order_line.purchase_order.order_date,
                )
            elif label["scenario_key"] == "older_revision_overwrite":
                plan = db.session.get(InventoryPlan, int(label["record_id"]))
                matching_older = Acknowledgement.query.filter_by(
                    po_line_id=plan.po_line_id,
                    validation_status="Valid",
                    accepted_quantity=plan.projected_quantity,
                    promised_date=plan.expected_delivery_date,
                ).one()
                latest = (
                    Acknowledgement.query.filter_by(
                        po_line_id=plan.po_line_id,
                        validation_status="Valid",
                    )
                    .order_by(Acknowledgement.revision_number.desc())
                    .first()
                )
                self.assertLess(matching_older.revision_number, latest.revision_number)

        dataset = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
        self.assertNotIn("ground_truth", dataset)
        for rows in dataset["records"].values():
            for record in rows:
                self.assertTrue(
                    {
                        "validation_status",
                        "ground_truth",
                        "issue_type",
                        "scenario_key",
                    }.isdisjoint(record)
                )

    def test_module7_route_renders_all_injected_scenarios(self):
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        anomaly_page = self.client.get("/anomaly-detection")
        self.assertEqual(anomaly_page.status_code, 200)
        for issue in (
            b"Incorrect PO Line Mapping",
            b"Invalid Promised Date",
            b"Inventory Plan Does Not Match Acknowledgement",
        ):
            self.assertIn(issue, anomaly_page.data)

    def test_exports_include_csv_json_sql_summary_and_download_links(self):
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        batch_id, directory = self.batch_directory(response)
        for filename in (
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
        ):
            self.assertTrue((directory / filename).is_file(), filename)
            self.assertIn(filename.encode(), response.data)

        with (directory / "purchase_orders.csv").open(
            encoding="utf-8", newline=""
        ) as input_file:
            self.assertEqual(len(list(csv.DictReader(input_file))), 8)
        with (directory / "acknowledgements.csv").open(
            encoding="utf-8", newline=""
        ) as input_file:
            acknowledgement_rows = list(csv.DictReader(input_file))
            self.assertEqual(len(acknowledgement_rows), 8)
            self.assertNotIn("Validation Status", acknowledgement_rows[0])

        with (directory / "acknowledgement_diagnostics.csv").open(
            encoding="utf-8", newline=""
        ) as input_file:
            diagnostic_rows = list(csv.DictReader(input_file))
            self.assertEqual(len(diagnostic_rows), 8)
            self.assertIn("Validation Status", diagnostic_rows[0])
            self.assertIn(
                "Invalid",
                {row["Validation Status"] for row in diagnostic_rows},
            )
        with (directory / "change_requests.csv").open(
            encoding="utf-8", newline=""
        ) as input_file:
            self.assertEqual(len(list(csv.DictReader(input_file))), 8)

        sql_seed = (directory / "dataset_seed.sql").read_text(encoding="utf-8")
        self.assertIn("Database seed only; not a model-feature export.", sql_seed)
        self.assertIn('INSERT INTO "supplier"', sql_seed)
        self.assertIn('INSERT INTO "purchase_order"', sql_seed)
        self.assertIn('INSERT INTO "acknowledgement"', sql_seed)
        self.assertIn("BEGIN TRANSACTION;", sql_seed)
        validation = json.loads(
            (directory / "validation_summary.json").read_text(encoding="utf-8")
        )
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["errors"], [])
        generation_summary = json.loads(
            (directory / "generation_summary.json").read_text(encoding="utf-8")
        )
        generation_event = generation_summary["generation_event"]
        self.assertEqual(generation_event["event_type"], "Dataset")
        self.assertEqual(
            generation_event["description"],
            "Synthetic Dataset Generated",
        )
        self.assertEqual(generation_event["batch_id"], batch_id)
        self.assertEqual(generation_event["seed"], 20261022)
        self.assertEqual(
            generation_event["generated_counts"],
            generation_summary["generated_counts"],
        )
        self.assertNotIn("timestamp", generation_event)

        download = self.client.get(
            f"/data-generator/exports/{batch_id}/dataset.json"
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, (directory / "dataset.json").read_bytes())

    def test_valid_completed_batch_is_reused_with_identical_exports(self):
        first_response = self.generate(seed=20261022)
        batch_id, directory = self.batch_directory(first_response)
        first_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir()
        }
        first_counts = (
            Supplier.query.count(),
            PurchaseOrder.query.count(),
            Acknowledgement.query.count(),
            ChangeRequest.query.count(),
            AuditLog.query.count(),
        )

        second_response = self.generate(seed=20261022)
        second_batch_id, second_directory = self.batch_directory(second_response)
        second_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in second_directory.iterdir()
        }

        self.assertEqual(batch_id, second_batch_id)
        self.assertIn(b"no duplicate records were added", second_response.data)
        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(
            first_counts,
            (
                Supplier.query.count(),
                PurchaseOrder.query.count(),
                Acknowledgement.query.count(),
                ChangeRequest.query.count(),
                AuditLog.query.count(),
            ),
        )

    def test_different_seed_produces_different_dataset(self):
        first_response = self.generate(seed=111)
        _, first_directory = self.batch_directory(first_response)
        first_data = (first_directory / "dataset.json").read_bytes()

        second_response = self.generate(seed=222)
        _, second_directory = self.batch_directory(second_response)
        second_data = (second_directory / "dataset.json").read_bytes()

        self.assertNotEqual(first_data, second_data)
        self.assertEqual(PurchaseOrder.query.count(), 16)

    def test_deterministic_id_collision_stops_before_generated_rows_are_added(self):
        seed = 20261022
        purchase_orders = 8
        events = 16
        collision_id = _deterministic_id(
            seed,
            purchase_orders,
            events,
            Supplier.__tablename__,
            0,
        )
        db.session.add(Supplier(
            id=collision_id,
            supplier_code="SUP-UNRELATED-COLLISION",
            supplier_name="Rekha Industrial Components",
            status="Active",
        ))
        db.session.commit()

        output_root = Path(self.output_root.name, "collision-exports")
        with self.assertRaisesRegex(
            DatasetGenerationError,
            rf"supplier.*{collision_id}",
        ):
            generate_synthetic_dataset(
                db=db,
                models=self.generator_models(),
                purchase_order_count=purchase_orders,
                event_count=events,
                seed=seed,
                output_root=output_root,
            )

        self.assertEqual(Supplier.query.count(), 1)
        self.assertEqual(PurchaseOrder.query.count(), 0)
        self.assertEqual(POLine.query.count(), 0)
        self.assertEqual(Acknowledgement.query.count(), 0)
        self.assertEqual(ChangeRequest.query.count(), 0)
        self.assertEqual(Approval.query.count(), 0)
        self.assertEqual(InventoryPlan.query.count(), 0)
        self.assertFalse(any(output_root.glob("module8_*")))
        self.assertFalse(output_root.exists())

    def test_unique_business_key_collision_stops_before_generated_rows_are_added(self):
        seed = 20261022
        purchase_orders = 8
        events = 16
        batch_id = _batch_id(seed, purchase_orders, events)
        supplier_code = f"SUP-{batch_id[:8].upper()}-0001"
        db.session.add(Supplier(
            supplier_code=supplier_code,
            supplier_name="Srinu Industrial Systems Pvt Ltd",
            email="srinu@unrelated-supplier.example",
            phone="+91-80-23456789",
            status="Active",
        ))
        db.session.commit()

        output_root = Path(self.output_root.name, "business-key-collision-exports")
        with self.assertRaisesRegex(DatasetGenerationError, "supplier_code"):
            generate_synthetic_dataset(
                db=db,
                models=self.generator_models(),
                purchase_order_count=purchase_orders,
                event_count=events,
                seed=seed,
                output_root=output_root,
            )

        self.assertEqual(Supplier.query.count(), 1)
        self.assertEqual(PurchaseOrder.query.count(), 0)
        self.assertEqual(POLine.query.count(), 0)
        self.assertEqual(Acknowledgement.query.count(), 0)
        self.assertEqual(ChangeRequest.query.count(), 0)
        self.assertEqual(Approval.query.count(), 0)
        self.assertEqual(InventoryPlan.query.count(), 0)
        self.assertFalse(any(output_root.glob("module8_*")))
        self.assertFalse(output_root.exists())

    def test_mismatched_existing_batch_is_not_reused_or_modified(self):
        seed = 20261022
        purchase_orders = 8
        events = 16
        first_response = self.generate(seed, purchase_orders, events)
        batch_id, batch_directory = self.batch_directory(first_response)
        supplier = Supplier.query.order_by(Supplier.id).first()
        supplier.supplier_name = "Mismatched Existing Supplier"
        db.session.commit()
        record_counts = (
            Supplier.query.count(),
            PurchaseOrder.query.count(),
            POLine.query.count(),
            Acknowledgement.query.count(),
            ChangeRequest.query.count(),
            Approval.query.count(),
            InventoryPlan.query.count(),
        )
        export_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in batch_directory.iterdir()
        }

        output_root = Path(self.output_root.name, "generated_data")
        with self.assertRaisesRegex(
            DatasetGenerationError,
            "does not match the deterministic batch content",
        ):
            generate_synthetic_dataset(
                db=db,
                models=self.generator_models(),
                purchase_order_count=purchase_orders,
                event_count=events,
                seed=seed,
                output_root=output_root,
            )

        self.assertEqual(
            record_counts,
            (
                Supplier.query.count(),
                PurchaseOrder.query.count(),
                POLine.query.count(),
                Acknowledgement.query.count(),
                ChangeRequest.query.count(),
                Approval.query.count(),
                InventoryPlan.query.count(),
            ),
        )
        self.assertEqual(supplier.supplier_name, "Mismatched Existing Supplier")
        self.assertEqual(
            export_hashes,
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in batch_directory.iterdir()
            },
        )
        self.assertEqual(batch_directory.name, f"module8_{batch_id}")

    def test_partial_batch_is_rejected_without_changing_records_or_exports(self):
        response = self.generate()
        self.assertEqual(response.status_code, 200)
        batch_id, batch_directory = self.batch_directory(response)

        missing_acknowledgement_id = _deterministic_id(
            20261022,
            8,
            16,
            Acknowledgement.__tablename__,
            7,
        )
        partial_record = db.session.get(
            Acknowledgement,
            missing_acknowledgement_id,
        )
        self.assertIsNotNone(partial_record)
        db.session.delete(partial_record)
        db.session.commit()

        models = {
            "suppliers": Supplier,
            "purchase_orders": PurchaseOrder,
            "po_lines": POLine,
            "acknowledgements": Acknowledgement,
            "change_requests": ChangeRequest,
            "approvals": Approval,
            "inventory_plans": InventoryPlan,
        }

        def database_snapshot():
            return {
                name: tuple(
                    tuple(row.items())
                    for row in _model_rows(
                        model.query.order_by(model.id).all()
                    )
                )
                for name, model in models.items()
            }

        before_records = database_snapshot()
        before_exports = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in batch_directory.iterdir()
        }

        with self.assertRaisesRegex(
            DatasetGenerationError,
            "Existing records conflict with the generated batch",
        ):
            generate_synthetic_dataset(
                db=db,
                models=self.generator_models(),
                purchase_order_count=8,
                event_count=16,
                seed=20261022,
                output_root=Path(self.output_root.name, "generated_data"),
            )

        self.assertEqual(database_snapshot(), before_records)
        self.assertEqual(
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in batch_directory.iterdir()
            },
            before_exports,
        )
        self.assertEqual(batch_directory.name, f"module8_{batch_id}")

    def test_export_publication_failure_is_recovered_by_retry(self):
        with patch(
            "module8_generator.os.replace",
            side_effect=OSError("simulated interruption"),
        ):
            response = self.generate()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The database batch committed", response.data)
        self.assertEqual(PurchaseOrder.query.count(), 8)
        batch_id = _batch_id(20261022, 8, 16)
        batch_directory = (
            Path(self.output_root.name)
            / "generated_data"
            / f"module8_{batch_id}"
        )
        self.assertFalse(batch_directory.exists())
        counts_before_retry = (
            Supplier.query.count(),
            PurchaseOrder.query.count(),
            POLine.query.count(),
            Acknowledgement.query.count(),
            ChangeRequest.query.count(),
            Approval.query.count(),
            InventoryPlan.query.count(),
        )
        self.assertEqual(counts_before_retry, (4, 8, 8, 8, 8, 4, 8))

        retry_response = self.generate()
        self.assertEqual(retry_response.status_code, 200)
        self.assertEqual(
            {path.name for path in batch_directory.iterdir()},
            set(EXPORT_FILES),
        )
        self.assertEqual(
            (
                Supplier.query.count(),
                PurchaseOrder.query.count(),
                POLine.query.count(),
                Acknowledgement.query.count(),
                ChangeRequest.query.count(),
                Approval.query.count(),
                InventoryPlan.query.count(),
            ),
            counts_before_retry,
        )

    def test_validation_failure_rolls_back_all_rows_and_exports(self):
        from module8_generator import _validate_batch

        with patch(
            "module8_generator._validate_batch",
            return_value={
                "passed": False,
                "errors": ["Forced validation failure for rollback test."],
                "checks": {},
                "record_count": 0,
            },
        ):
            response = self.generate()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Forced validation failure", response.data)
        self.assertEqual(Supplier.query.count(), 0)
        self.assertEqual(PurchaseOrder.query.count(), 0)
        self.assertEqual(POLine.query.count(), 0)
        self.assertEqual(Acknowledgement.query.count(), 0)
        self.assertEqual(ChangeRequest.query.count(), 0)
        self.assertEqual(Approval.query.count(), 0)
        self.assertEqual(InventoryPlan.query.count(), 0)
        self.assertEqual(AuditLog.query.count(), 0)
        generated_root = Path(self.output_root.name, "generated_data")
        self.assertFalse(any(generated_root.glob("module8_*")))

    def test_frontend_controls_defaults_and_success_summary(self):
        page = self.client.get("/data-generator")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="purchase_order_count"', page.data)
        self.assertIn(b'name="event_count"', page.data)
        self.assertIn(b'name="seed"', page.data)
        self.assertIn(b'value="10000"', page.data)
        self.assertIn(b'value="15000"', page.data)
        self.assertIn(b'value="20261022"', page.data)

        response = self.generate(purchase_orders=4, events=8)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Generation Summary", response.data)
        self.assertIn(b"Validation", response.data)
        self.assertIn(b"Passed", response.data)
        self.assertIn(b"Purchase Orders", response.data)


if __name__ == "__main__":
    unittest.main()
