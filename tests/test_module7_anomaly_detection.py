import re
import tempfile
import unittest
from datetime import date
from html import unescape
from pathlib import Path

from app import (
    Acknowledgement,
    Approval,
    ChangeRequest,
    InventoryPlan,
    POLine,
    ProductionOrder,
    PurchaseOrder,
    Supplier,
    create_app,
    db,
)


class AnomalyDetectionTests(unittest.TestCase):
    def setUp(self):
        self.database_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.database_directory.cleanup)
        self.database_path = Path(self.database_directory.name, "yottaflex-test.db")
        database_uri = f"sqlite:///{self.database_path.as_posix()}"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "module7-test-only",
            "SQLALCHEMY_DATABASE_URI": database_uri,
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

    def create_line(self, suffix="1", order_date=date(2026, 1, 10)):
        supplier = Supplier(
            supplier_code=f"SUP-{suffix}",
            supplier_name="Akshu Components",
            status="Active",
        )
        db.session.add(supplier)
        db.session.flush()

        order = PurchaseOrder(
            po_number=f"PO-{suffix}",
            supplier_id=supplier.id,
            order_date=order_date,
            status="Created",
        )
        db.session.add(order)
        db.session.flush()

        line = POLine(
            po_id=order.id,
            item_code=f"ITEM-{suffix}",
            description="Industrial valve",
            requested_quantity=10,
            unit_price=12.5,
            requested_delivery_date=date(2026, 2, 20),
        )
        db.session.add(line)
        db.session.flush()
        return order, line

    def create_ack(
        self,
        order,
        line,
        number,
        revision,
        quantity,
        promised_date=date(2026, 2, 1),
    ):
        acknowledgement = Acknowledgement(
            acknowledgement_number=number,
            po_id=order.id,
            po_line_id=line.id,
            revision_number=revision,
            accepted_quantity=quantity,
            rejected_quantity=0,
            promised_date=promised_date,
            validation_status="Valid",
        )
        db.session.add(acknowledgement)
        db.session.flush()
        return acknowledgement

    def create_change(self, number, line, status, quantity=15):
        change = ChangeRequest(
            change_number=number,
            po_line_id=line.id,
            old_quantity=line.requested_quantity,
            new_quantity=quantity,
            old_price=line.unit_price,
            new_price=line.unit_price,
            old_delivery_date=line.requested_delivery_date,
            new_delivery_date=date(2026, 3, 1),
            reason="Supplier requested a revised schedule",
            status=status,
        )
        db.session.add(change)
        db.session.flush()
        return change

    def production_impact_rows(self, response):
        rendered = response.get_data(as_text=True)
        rows = []
        for row_markup in re.findall(r"<tr>(.*?)</tr>", rendered, re.DOTALL):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_markup, re.DOTALL)
            values = [
                " ".join(unescape(re.sub(r"<[^>]+>", "", cell)).split())
                for cell in cells
            ]
            if len(values) == 9 and values[0].startswith("PROD-"):
                rows.append(values)
        return rows

    def test_pending_change_does_not_update_inventory_plan(self):
        order, line = self.create_line()
        self.create_ack(order, line, "ACK-1-01", 1, 8)
        change = self.create_change("CHG-PENDING", line, "Pending")
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=15,
            expected_delivery_date=change.new_delivery_date,
            supply_status="Confirmed",
        ))
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )
        self.assertIn(b"projected quantity is 15", response.data)
        self.assertNotIn(b"CHG-PENDING", response.data)
        self.assertNotIn(b"Unapproved Change Reflected", response.data)

        refresh_response = self.client.post("/inventory-planning/refresh")
        self.assertEqual(refresh_response.status_code, 302)
        plan = InventoryPlan.query.filter_by(po_line_id=line.id).one()
        self.assertEqual(plan.projected_quantity, 8)
        self.assertEqual(plan.expected_delivery_date, date(2026, 2, 1))

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )

    def test_acknowledgement_linked_to_wrong_po_line_is_reported(self):
        first_order, _ = self.create_line("1")
        _, second_line = self.create_line("2")
        self.create_ack(
            first_order,
            second_line,
            "ACK-WRONG-LINE",
            1,
            5,
        )
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Incorrect PO Line Mapping", response.data)
        self.assertIn(b"ACK-WRONG-LINE", response.data)
        self.assertIn(b"belongs to PO", response.data)

    def test_acknowledgement_with_matching_po_line_relationship_is_not_flagged(self):
        order, line = self.create_line()
        self.create_ack(order, line, "ACK-CORRECT-LINE", 1, 5)
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Incorrect PO Line Mapping", response.data)
        self.assertNotIn(b"ACK-CORRECT-LINE", response.data)

    def test_early_promised_date_is_reported_and_null_date_is_safe(self):
        order, line = self.create_line()
        self.create_ack(
            order,
            line,
            "ACK-EARLY",
            1,
            5,
            promised_date=date(2026, 1, 9),
        )
        second_order, second_line = self.create_line("2")
        self.create_ack(
            second_order,
            second_line,
            "ACK-NO-DATE",
            1,
            5,
            promised_date=None,
        )
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid Promised Date", response.data)
        self.assertIn(b"ACK-EARLY", response.data)
        self.assertNotIn(b"ACK-NO-DATE", response.data)

    def test_older_acknowledgement_does_not_replace_newer_revision(self):
        order, line = self.create_line()
        self.create_ack(
            order,
            line,
            "ACK-OLD",
            1,
            4,
            promised_date=date(2026, 2, 1),
        )
        self.create_ack(
            order,
            line,
            "ACK-NEW",
            2,
            9,
            promised_date=date(2026, 2, 10),
        )
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=4,
            expected_delivery_date=date(2026, 2, 1),
            supply_status="Confirmed",
        ))
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )
        self.assertIn(b"revision 1", response.data)
        self.assertIn(b"latest valid revision is 2", response.data)

        refresh_response = self.client.post("/inventory-planning/refresh")
        self.assertEqual(refresh_response.status_code, 302)
        plan = InventoryPlan.query.filter_by(po_line_id=line.id).one()
        self.assertEqual(plan.projected_quantity, 9)
        self.assertEqual(plan.expected_delivery_date, date(2026, 2, 10))

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )

    def test_rejected_change_values_are_not_attributed_as_mismatch_cause(self):
        order, line = self.create_line()
        self.create_ack(order, line, "ACK-REJECTED-CASE", 1, 8)
        change = self.create_change("CHG-REJECTED", line, "Rejected")
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=change.new_quantity,
            expected_delivery_date=change.new_delivery_date,
            supply_status="Confirmed",
        ))
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )
        self.assertNotIn(b"CHG-REJECTED", response.data)
        self.assertNotIn(b"Rejected change request", response.data)

    def test_approved_change_is_not_mistaken_for_supplier_acknowledgement(self):
        order, line = self.create_line()
        self.create_ack(order, line, "ACK-APPROVED-CASE", 1, 8)
        change = self.create_change("CHG-APPROVED", line, "Approved")
        line.requested_quantity = change.new_quantity
        line.requested_delivery_date = change.new_delivery_date
        db.session.add(Approval(
            change_request_id=change.id,
            decision="Approved",
            reviewer="Rekha",
            comments="Approved schedule update",
        ))
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=change.new_quantity,
            expected_delivery_date=change.new_delivery_date,
            supply_status="Confirmed",
        ))
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )
        self.assertNotIn(b"CHG-APPROVED", response.data)

    def test_unrelated_change_does_not_flag_matching_inventory_plan(self):
        order, line = self.create_line("1")
        self.create_ack(order, line, "ACK-UNRELATED-CASE", 1, 8)
        _, unrelated_line = self.create_line("2")
        self.create_change("CHG-UNRELATED", unrelated_line, "Pending")
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=8,
            expected_delivery_date=date(2026, 2, 1),
            supply_status="Confirmed",
        ))
        db.session.commit()

        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No Anomalies Found!", response.data)
        self.assertNotIn(
            b"Inventory Plan Does Not Match Acknowledgement",
            response.data,
        )

    def test_empty_database_renders_anomaly_page(self):
        response = self.client.get("/anomaly-detection")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No issues were found", response.data)
        self.assertIn(b"Total Anomalies", response.data)

    def test_inventory_allocation_does_not_double_count_confirmed_supply(self):
        _, line = self.create_line()
        db.session.add(InventoryPlan(
            po_line_id=line.id,
            projected_quantity=10,
            expected_delivery_date=date(2026, 2, 1),
            supply_status="Confirmed",
        ))
        db.session.add_all([
            ProductionOrder(
                production_number="PROD-EARLY",
                po_line_id=line.id,
                required_quantity=6,
                required_date=date(2026, 2, 5),
                status="Planned",
            ),
            ProductionOrder(
                production_number="PROD-LATE",
                po_line_id=line.id,
                required_quantity=6,
                required_date=date(2026, 2, 10),
                status="Planned",
            ),
        ])
        db.session.commit()

        response = self.client.get("/inventory-planning")
        self.assertEqual(response.status_code, 200)
        rows = self.production_impact_rows(response)
        self.assertEqual(
            rows,
            [
                [
                    "PROD-EARLY",
                    "ITEM-1",
                    "10",
                    "6",
                    "6",
                    "No Supply Shortage",
                    "2026-02-01",
                    "2026-02-05",
                    "On Time",
                ],
                [
                    "PROD-LATE",
                    "ITEM-1",
                    "10",
                    "4",
                    "6",
                    "Supply Shortage: 2",
                    "2026-02-01",
                    "2026-02-10",
                    "On Time",
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
