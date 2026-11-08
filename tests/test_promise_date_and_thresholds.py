import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import (
    Acknowledgement,
    POLine,
    PromiseDate,
    PurchaseOrder,
    Supplier,
    create_app,
    db,
    evaluate_change_thresholds,
)


class PromiseDateAndThresholdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name, "promise-test.db")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path.as_posix()}",
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(db.session.remove)
        self.addCleanup(db.engine.dispose)
        db.create_all()

    def test_promise_date_is_linked_to_acknowledgement_and_line(self):
        supplier = Supplier(supplier_code="SUP-T", supplier_name="Test Supplier")
        db.session.add(supplier)
        db.session.flush()
        order = PurchaseOrder(
            po_number="PO-TEST-000001", supplier_id=supplier.id,
            order_date=date(2026, 1, 1),
        )
        db.session.add(order)
        db.session.flush()
        line = POLine(
            po_id=order.id, item_code="ITEM-1", requested_quantity=10,
            unit_price=2.5, requested_delivery_date=date(2026, 2, 1),
        )
        db.session.add(line)
        db.session.flush()
        ack = Acknowledgement(
            acknowledgement_number="ACK-1-01", po_id=order.id,
            po_line_id=line.id, revision_number=1, accepted_quantity=8,
            rejected_quantity=2, promised_date=date(2026, 2, 3),
            validation_status="Valid",
        )
        db.session.add(ack)
        db.session.flush()
        promise = PromiseDate(
            po_line_id=line.id, acknowledgement_id=ack.id,
            promised_date=date(2026, 2, 3), lifecycle_status="Current",
        )
        db.session.add(promise)
        db.session.commit()
        self.assertEqual(PromiseDate.query.count(), 1)
        self.assertEqual(PromiseDate.query.one().acknowledgement_id, ack.id)
        self.assertEqual(PromiseDate.query.one().po_line_id, line.id)

    def test_change_threshold_policy_flags_large_changes(self):
        class Line:
            requested_quantity = 100
            unit_price = 20.0
            requested_delivery_date = date(2026, 3, 1)

        reasons = evaluate_change_thresholds(
            Line(), 120, 22.0, date(2026, 3, 12)
        )
        self.assertEqual(len(reasons), 3)
        self.assertTrue(any("quantity" in reason for reason in reasons))
        self.assertTrue(any("price" in reason for reason in reasons))
        self.assertTrue(any("delivery date" in reason for reason in reasons))

    def test_change_within_threshold_has_no_escalation_reason(self):
        class Line:
            requested_quantity = 100
            unit_price = 20.0
            requested_delivery_date = date(2026, 3, 1)

        self.assertEqual(
            evaluate_change_thresholds(Line(), 105, 20.5, date(2026, 3, 5)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
