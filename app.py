
from flask import (
    Flask, abort, current_app, flash, redirect, render_template,
    request, send_from_directory, url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Engine
from datetime import date, datetime, timezone
import csv
import os
# =====================================================
# APPLICATION CONFIGURATION
# =====================================================

db = SQLAlchemy()
route_definitions = []


@sqlalchemy_event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite does not enforce declared FKs unless this pragma is enabled."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def route(rule, **options):
    def register(view_func):
        route_definitions.append((rule, view_func, options.copy()))
        return view_func

    return register


def create_app(config=None):
    application = Flask(__name__)
    application.config.from_mapping(
        SECRET_KEY=os.environ.get("YOTTAFLEX_SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI="sqlite:///yottaflex.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        QTY_CHANGE_APPROVAL_PERCENT=10,
        PRICE_CHANGE_APPROVAL_PERCENT=5,
        DATE_CHANGE_APPROVAL_DAYS=7,
    )
    if config:
        application.config.update(config)
    db.init_app(application)
    for rule, view_func, options in route_definitions:
        application.add_url_rule(rule, view_func=view_func, **options)
    return application


# =====================================================
# MODULE 1: SUPPLIER MANAGEMENT
# =====================================================

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_code = db.Column(db.String(50), unique=True, nullable=False)
    supplier_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(30))
    status = db.Column(db.String(30), default="Active")


# =====================================================
# MODULE 2: PURCHASE ORDER MANAGEMENT
# =====================================================

class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False)
    order_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), default="Created")

    supplier = db.relationship("Supplier", backref="purchase_orders")
    lines = db.relationship("POLine", backref="purchase_order", lazy=True)


class POLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    item_code = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    requested_quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    requested_delivery_date = db.Column(db.Date, nullable=False)


# =====================================================
# MODULE 3: SUPPLIER ACKNOWLEDGEMENTS
# =====================================================

class Acknowledgement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    acknowledgement_number = db.Column(db.String(100), unique=True, nullable=False)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    po_line_id = db.Column(db.Integer, db.ForeignKey("po_line.id"), nullable=False)

    revision_number = db.Column(db.Integer, nullable=False)
    accepted_quantity = db.Column(db.Integer, nullable=False)
    rejected_quantity = db.Column(db.Integer, nullable=False)
    promised_date = db.Column(db.Date, nullable=True)

    supplier_note = db.Column(db.Text)
    validation_status = db.Column(db.String(30), default="Valid")
    recorded_date = db.Column(db.DateTime, default=datetime.utcnow)

    purchase_order = db.relationship("PurchaseOrder")
    purchase_order_line = db.relationship("POLine")


class PromiseDate(db.Model):
    """Immutable promise-date snapshot associated with an acknowledgement revision."""
    id = db.Column(db.Integer, primary_key=True)
    po_line_id = db.Column(db.Integer, db.ForeignKey("po_line.id"), nullable=False, index=True)
    acknowledgement_id = db.Column(
        db.Integer, db.ForeignKey("acknowledgement.id"), nullable=False,
        unique=True, index=True,
    )
    promised_date = db.Column(db.Date, nullable=False)
    effective_from = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    effective_to = db.Column(db.DateTime)
    lifecycle_status = db.Column(db.String(20), nullable=False, default="Current")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    po_line = db.relationship("POLine", backref="promise_date_history")
    acknowledgement = db.relationship("Acknowledgement", backref="promise_date_snapshot")


# =====================================================
# MODULE 4: CHANGE REQUESTS AND APPROVALS
# =====================================================

class ChangeRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    change_number = db.Column(db.String(100), unique=True, nullable=False)

    po_line_id = db.Column(db.Integer, db.ForeignKey("po_line.id"), nullable=False)

    old_quantity = db.Column(db.Integer)
    new_quantity = db.Column(db.Integer)

    old_price = db.Column(db.Float)
    new_price = db.Column(db.Float)

    old_delivery_date = db.Column(db.Date)
    new_delivery_date = db.Column(db.Date)

    reason = db.Column(db.Text)
    status = db.Column(db.String(30), default="Pending")
    created_date = db.Column(db.DateTime, default=datetime.utcnow)

    purchase_order_line = db.relationship("POLine")


class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    change_request_id = db.Column(
        db.Integer, db.ForeignKey("change_request.id"), nullable=False
    )

    decision = db.Column(db.String(30), nullable=False)
    reviewer = db.Column(db.String(100))
    comments = db.Column(db.Text)
    decision_date = db.Column(db.DateTime, default=datetime.utcnow)

    change_request = db.relationship("ChangeRequest")


# =====================================================
# MODULE 5: INVENTORY PLANNING
# =====================================================

class InventoryPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_line_id = db.Column(
        db.Integer, db.ForeignKey("po_line.id"), unique=True, nullable=False
    )

    projected_quantity = db.Column(db.Integer, default=0)
    expected_delivery_date = db.Column(db.Date, nullable=True)
    supply_status = db.Column(
        db.String(50),
        nullable=False,
        default="Awaiting Acknowledgement"
    )
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

    po_line = db.relationship("POLine")


class ProductionOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    production_number = db.Column(db.String(50), unique=True, nullable=False)
    po_line_id = db.Column(db.Integer, db.ForeignKey("po_line.id"), nullable=False)

    required_quantity = db.Column(db.Integer, nullable=False)
    required_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), default="Planned")

    po_line = db.relationship("POLine")


# =====================================================
# MODULE 6: NOTIFICATIONS
# =====================================================

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    notification_number = db.Column(
        db.String(50), unique=True, nullable=False
    )

    notification_type = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)

    related_po_id = db.Column(
        db.Integer, db.ForeignKey("purchase_order.id")
    )

    related_change_id = db.Column(
        db.Integer, db.ForeignKey("change_request.id")
    )

    status = db.Column(db.String(30), default="Unread")
    created_date = db.Column(db.DateTime, default=datetime.utcnow)


# =====================================================
# MODULE 6: AUDIT HISTORY
# =====================================================

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    event_number = db.Column(
        db.String(50), unique=True, nullable=False
    )

    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)

    action = db.Column(db.String(100), nullable=False)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)

    performed_by = db.Column(db.String(100), default="System")
    event_date = db.Column(db.DateTime, default=datetime.utcnow)


# =====================================================
# MODULE 7: DATA VALIDATION AND ANOMALY DETECTION
# =====================================================

@route("/anomaly-detection")
def anomaly_detection():
    anomalies = []
    acknowledgements = Acknowledgement.query.all()
    po_lines = POLine.query.all()
    changes = ChangeRequest.query.all()
    approvals = Approval.query.all()
    plans = InventoryPlan.query.all()

    lines_by_id = {line.id: line for line in po_lines}
    plans_by_line = {plan.po_line_id: plan for plan in plans}
    acknowledgements_by_line = {}
    approvals_by_change = {}

    for ack in acknowledgements:
        if ack.validation_status == "Valid":
            acknowledgements_by_line.setdefault(ack.po_line_id, []).append(ack)

    for revisions in acknowledgements_by_line.values():
        revisions.sort(
            key=lambda ack: (ack.revision_number, ack.id),
            reverse=True
        )

    for approval in approvals:
        approvals_by_change.setdefault(
            approval.change_request_id, []
        ).append(approval)

    for ack in acknowledgements:
        line = lines_by_id.get(ack.po_line_id)

        if line is None:
            anomalies.append({
                "type": "Missing PO Line",
                "record": ack.acknowledgement_number,
                "description": "Acknowledgement is connected to a missing PO line.",
                "severity": "High"
            })
            continue

        if ack.accepted_quantity < 0 or ack.rejected_quantity < 0:
            anomalies.append({
                "type": "Invalid Quantity",
                "record": ack.acknowledgement_number,
                "description": "Accepted or rejected quantity is negative.",
                "severity": "High"
            })

        if ack.accepted_quantity + ack.rejected_quantity > line.requested_quantity:
            anomalies.append({
                "type": "Quantity Mismatch",
                "record": ack.acknowledgement_number,
                "description": "Accepted and rejected quantities exceed the requested quantity.",
                "severity": "High"
            })

        line_purchase_order = db.session.get(PurchaseOrder, line.po_id)
        if (
            line_purchase_order is not None
            and ack.po_id != line_purchase_order.id
        ):
            anomalies.append({
                "type": "Incorrect PO Line Mapping",
                "record": ack.acknowledgement_number,
                "description": (
                    f"Acknowledgement is linked to PO {ack.po_id}, but PO line "
                    f"{line.id} belongs to PO "
                    f"{line_purchase_order.po_number} "
                    f"(ID {line_purchase_order.id})."
                ),
                "severity": "High"
            })

        if (
            line_purchase_order
            and ack.promised_date is not None
            and ack.promised_date < line_purchase_order.order_date
        ):
            anomalies.append({
                "type": "Invalid Promised Date",
                "record": ack.acknowledgement_number,
                "description": (
                    f"Promised date {ack.promised_date} is earlier than "
                    f"purchase order {line_purchase_order.po_number}'s order "
                    f"date {line_purchase_order.order_date}."
                ),
                "severity": "High"
            })

    revision_groups = {}
    for ack in acknowledgements:
        revision_groups.setdefault(
            (ack.po_line_id, ack.revision_number), []
        ).append(ack)

    for (po_line_id, revision), records in revision_groups.items():
        if len(records) > 1:
            anomalies.append({
                "type": "Duplicate Acknowledgement Revision",
                "record": f"PO line {po_line_id}",
                "description": (
                    f"Revision {revision} appears {len(records)} times for "
                    f"this PO line."
                ),
                "severity": "High"
            })

    for change in changes:
        change_approvals = approvals_by_change.get(change.id, [])

        if change.status == "Approved" and not change_approvals:
            anomalies.append({
                "type": "Missing Approval",
                "record": change.change_number,
                "description": (
                    "This change request is approved but has no approval record."
                ),
                "severity": "High"
            })

        if change.status == "Pending" and change_approvals:
            anomalies.append({
                "type": "Approval Status Mismatch",
                "record": change.change_number,
                "description": (
                    "This change request is still pending but already has an "
                    "approval record."
                ),
                "severity": "Medium"
            })

    for line in po_lines:
        if line.requested_quantity <= 0:
            anomalies.append({
                "type": "Invalid PO Quantity",
                "record": line.item_code,
                "description": "Purchase order line has zero or negative requested quantity.",
                "severity": "High"
            })

        if line.unit_price < 0:
            anomalies.append({
                "type": "Invalid Unit Price",
                "record": line.item_code,
                "description": "Purchase order line has a negative unit price.",
                "severity": "High"
            })

        if db.session.get(PurchaseOrder, line.po_id) is None:
            anomalies.append({
                "type": "Missing Purchase Order",
                "record": line.item_code,
                "description": "Purchase order line references a missing purchase order.",
                "severity": "High"
            })

    for plan in plans:
        line = lines_by_id.get(plan.po_line_id)
        revisions = acknowledgements_by_line.get(plan.po_line_id, [])
        latest_ack = revisions[0] if revisions else None
        expected_quantity = latest_ack.accepted_quantity if latest_ack else 0
        expected_date = latest_ack.promised_date if latest_ack else None
        expected_status = "Awaiting Acknowledgement"
        if latest_ack:
            expected_status = (
                "Needs Review"
                if line and latest_ack.accepted_quantity > line.requested_quantity
                else "Confirmed"
            )

        quantity_mismatch = plan.projected_quantity != expected_quantity
        date_mismatch = plan.expected_delivery_date != expected_date
        status_mismatch = plan.supply_status != expected_status

        if not (quantity_mismatch or date_mismatch or status_mismatch):
            continue

        older_matching_ack = next(
            (
                ack for ack in revisions[1:]
                if (
                    ack.accepted_quantity == plan.projected_quantity
                    and ack.promised_date == plan.expected_delivery_date
                )
            ),
            None
        )
        mismatch_details = (
            f"projected quantity is {plan.projected_quantity} "
            f"(expected {expected_quantity}); expected delivery date is "
            f"{plan.expected_delivery_date or 'Not Available'} "
            f"(expected {expected_date or 'Not Available'}); supply status is "
            f"{plan.supply_status or 'Not Available'} "
            f"(expected {expected_status})."
        )
        if older_matching_ack and latest_ack:
            description = (
                f"The inventory plan for PO line {plan.po_line_id} matches "
                f"acknowledgement revision {older_matching_ack.revision_number}, "
                f"but the latest valid revision is "
                f"{latest_ack.revision_number}; {mismatch_details}"
            )
        else:
            description = (
                f"The inventory plan for PO line {plan.po_line_id} does not "
                f"match the latest valid acknowledgement-derived projection; "
                f"{mismatch_details}"
            )

        anomalies.append({
            "type": "Inventory Plan Does Not Match Acknowledgement",
            "record": f"Inventory plan for PO line {plan.po_line_id}",
            "description": description,
            "severity": "High"
        })

    total_anomalies = len(anomalies)
    high_count = sum(
        1 for anomaly in anomalies if anomaly["severity"] == "High"
    )
    medium_count = sum(
        1 for anomaly in anomalies if anomaly["severity"] == "Medium"
    )

    return render_template(
        "anomaly_detection.html",
        anomalies=anomalies,
        total_anomalies=total_anomalies,
        high_count=high_count,
        medium_count=medium_count
    )




# =====================================================
# MODULE 8: SYNTHETIC DATA GENERATION AND GROUND TRUTH
# =====================================================

@route("/data-generator", methods=["GET", "POST"])
def data_generator():
    from module8_generator import (
        DEFAULT_EVENT_COUNT,
        DEFAULT_PURCHASE_ORDER_COUNT,
        DEFAULT_SEED,
        DatasetGenerationError,
        DatasetValidationError,
        generate_synthetic_dataset,
    )

    form_values = {
        "purchase_order_count": DEFAULT_PURCHASE_ORDER_COUNT,
        "event_count": DEFAULT_EVENT_COUNT,
        "seed": DEFAULT_SEED,
    }
    generation_result = None
    generation_error = None

    if request.method == "POST":
        try:
            form_values["purchase_order_count"] = int(
                request.form.get(
                    "purchase_order_count",
                    request.form.get("total_records", DEFAULT_PURCHASE_ORDER_COUNT),
                )
            )
            form_values["event_count"] = int(
                request.form.get("event_count", DEFAULT_EVENT_COUNT)
            )
            form_values["seed"] = int(request.form.get("seed", DEFAULT_SEED))

            generation_result = generate_synthetic_dataset(
                db=db,
                models={
                    "db": db,
                    "Supplier": Supplier,
                    "PurchaseOrder": PurchaseOrder,
                    "POLine": POLine,
                    "Acknowledgement": Acknowledgement,
                    "ChangeRequest": ChangeRequest,
                    "Approval": Approval,
                    "InventoryPlan": InventoryPlan,
                },
                purchase_order_count=form_values["purchase_order_count"],
                event_count=form_values["event_count"],
                seed=form_values["seed"],
                output_root=current_app.config.get(
                    "GENERATED_DATA_ROOT",
                    os.path.join(current_app.root_path, "generated_data"),
                ),
            )
            if generation_result["created"]:
                generation_event = dict(
                    generation_result["summary"]["generation_event"]
                )
                generation_event["timestamp"] = datetime.now(
                    timezone.utc
                ).isoformat()
                current_app.logger.info(
                    "Dataset generation event: %s",
                    generation_event,
                )
        except DatasetValidationError as error:
            db.session.rollback()
            current_app.logger.error("Synthetic dataset validation failed: %s", error)
            generation_error = (
                "Validation failed. No generated records were saved. "
                + "; ".join(error.errors)
            )
        except DatasetGenerationError as error:
            db.session.rollback()
            current_app.logger.error("Synthetic dataset generation stopped: %s", error)
            generation_error = str(error)
        except (ValueError, KeyError) as error:
            db.session.rollback()
            generation_error = str(error)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Synthetic dataset generation failed.")
            generation_error = (
                "Dataset generation could not complete; please review the server "
                "log. If database records committed before export publication "
                "failed, retry with the same configuration to recover exports."
            )

    return render_template(
        "data-generator.html",
        form_values=form_values,
        generation_result=generation_result,
        generation_error=generation_error,
    )


@route("/data-generator/exports/<batch_id>/<filename>")
def download_generated_export(batch_id, filename):
    allowed_files = {
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
    }
    if (
        len(batch_id) != 12
        or any(character not in "0123456789abcdef" for character in batch_id)
        or filename not in allowed_files
    ):
        abort(404)
    directory = os.path.join(
        current_app.config.get(
            "GENERATED_DATA_ROOT",
            os.path.join(current_app.root_path, "generated_data"),
        ),
        f"module8_{batch_id}",
    )
    return send_from_directory(directory, filename, as_attachment=True)



# =====================================================
# HELPER FUNCTIONS
# =====================================================

def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def generate_number(model, prefix):
    next_id = (db.session.query(db.func.max(model.id)).scalar() or 0) + 1
    return f"{prefix}-{date.today().year}-{next_id:06d}"


def create_notification(
    notification_type,
    message,
    related_po_id=None,
    related_change_id=None
):
    notification = Notification(
        notification_number=generate_number(Notification, "NOT"),
        notification_type=notification_type,
        message=message,
        related_po_id=related_po_id,
        related_change_id=related_change_id,
        status="Unread"
    )

    db.session.add(notification)


def create_audit_log(
    entity_type,
    entity_id,
    action,
    old_value=None,
    new_value=None,
    performed_by="System"
):
    event = AuditLog(
        event_number=generate_number(AuditLog, "EVT"),
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        performed_by=performed_by
    )

    db.session.add(event)


# =====================================================
# DASHBOARD
# =====================================================

@route("/")
def index():
    return render_template(
        "index.html",
        suppliers=Supplier.query.count(),
        purchase_orders=PurchaseOrder.query.count(),
        acknowledgements=Acknowledgement.query.count(),
        change_requests=ChangeRequest.query.count(),
        pending_changes=ChangeRequest.query.filter_by(status="Pending").count(),
        production_orders=ProductionOrder.query.count(),
        unread_notifications=Notification.query.filter_by(status="Unread").count(),
        recent_orders=PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).limit(6).all()
    )


# =====================================================
# MODULE 1: SUPPLIERS
# =====================================================

@route("/suppliers", methods=["GET", "POST"])
def suppliers():

    if request.method == "POST":
        supplier_code = request.form.get("supplier_code", "").strip()
        supplier_name = request.form.get("supplier_name", "").strip()

        if not supplier_code or not supplier_name:
            flash("Supplier code and name are required.", "error")
            return redirect(url_for("suppliers"))

        if Supplier.query.filter_by(supplier_code=supplier_code).first():
            flash("Supplier code already exists.", "error")
            return redirect(url_for("suppliers"))

        supplier = Supplier(
            supplier_code=supplier_code,
            supplier_name=supplier_name,
            email=request.form.get("email"),
            phone=request.form.get("phone"),
            status="Active"
        )

        db.session.add(supplier)
        db.session.flush()

        create_audit_log(
            "Supplier", supplier.id, "Supplier Created",
            new_value=supplier.supplier_name
        )

        db.session.commit()
        flash("Supplier added successfully.", "success")
        return redirect(url_for("suppliers"))

    suppliers_list = Supplier.query.order_by(Supplier.id.desc()).all()

    return render_template(
        "suppliers.html",
        suppliers=suppliers_list
    )


# =====================================================
# MODULE 2: PURCHASE ORDERS
# =====================================================

@route("/purchase-orders", methods=["GET", "POST"])
def purchase_orders():

    if request.method == "POST":
        try:
            supplier_id = int(request.form["supplier_id"])
            quantity = int(request.form["quantity"])
            price = float(request.form["price"])
            order_date = parse_date(request.form["order_date"])
            delivery_date = parse_date(request.form["delivery_date"])

            supplier = db.session.get(Supplier, supplier_id)

            if not supplier or supplier.status != "Active":
                flash("Select an active supplier.", "error")
                return redirect(url_for("purchase_orders"))

            if quantity <= 0 or price < 0:
                flash("Quantity must be positive and price cannot be negative.", "error")
                return redirect(url_for("purchase_orders"))

            if delivery_date < order_date:
                flash("Delivery date cannot be before order date.", "error")
                return redirect(url_for("purchase_orders"))

            po = PurchaseOrder(
                po_number=generate_number(PurchaseOrder, "PO"),
                supplier_id=supplier_id,
                order_date=order_date,
                status="Created"
            )

            db.session.add(po)
            db.session.flush()

            line = POLine(
                po_id=po.id,
                item_code=request.form["item_code"].strip(),
                description=request.form.get("description"),
                requested_quantity=quantity,
                unit_price=price,
                requested_delivery_date=delivery_date
            )

            db.session.add(line)
            db.session.flush()

            create_audit_log(
                "PurchaseOrder", po.id, "Purchase Order Created",
                new_value=po.po_number
            )

            db.session.commit()
            flash("Purchase order created successfully.", "success")

        except (ValueError, KeyError):
            db.session.rollback()
            flash("Please enter valid purchase order details.", "error")

        return redirect(url_for("purchase_orders"))

    orders = PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).all()
    suppliers_list = Supplier.query.filter_by(status="Active").all()

    return render_template(
        "purchase_orders.html",
        orders=orders,
        suppliers=suppliers_list
    )


# =====================================================
# MODULE 3: ACKNOWLEDGEMENTS
# =====================================================

@route("/acknowledgements", methods=["GET", "POST"])
def acknowledgements():

    if request.method == "POST":
        try:
            po_line_id = int(request.form["po_line_id"])
            accepted = int(request.form["accepted_quantity"])
            rejected = int(request.form["rejected_quantity"])
            promised_date = parse_date(request.form["promised_date"])

            line = db.session.get(POLine, po_line_id)

            if not line:
                flash("Invalid purchase order line.", "error")
                return redirect(url_for("acknowledgements"))

            if accepted < 0 or rejected < 0:
                flash("Accepted and rejected quantities cannot be negative.", "error")
                return redirect(url_for("acknowledgements"))

            if accepted + rejected > line.requested_quantity:
                flash("Accepted plus rejected quantity exceeds requested quantity.", "error")
                return redirect(url_for("acknowledgements"))

            if promised_date < line.purchase_order.order_date:
                flash("Promised date cannot be before purchase order date.", "error")
                return redirect(url_for("acknowledgements"))

            latest_revision = db.session.query(
                db.func.max(Acknowledgement.revision_number)
            ).filter_by(po_line_id=po_line_id).scalar() or 0

            revision = latest_revision + 1

            acknowledgement = Acknowledgement(
                acknowledgement_number=f"ACK-{line.id}-{revision:02d}",
                po_id=line.po_id,
                po_line_id=line.id,
                revision_number=revision,
                accepted_quantity=accepted,
                rejected_quantity=rejected,
                promised_date=promised_date,
                supplier_note=request.form.get("supplier_note"),
                validation_status="Valid"
            )

            db.session.add(acknowledgement)
            db.session.flush()

            # Preserve each supplier promise as an immutable, revision-linked event.
            current_promises = PromiseDate.query.filter_by(
                po_line_id=line.id, lifecycle_status="Current"
            ).all()
            for previous_promise in current_promises:
                previous_promise.lifecycle_status = "Superseded"
                previous_promise.effective_to = datetime.utcnow()
            db.session.add(PromiseDate(
                po_line_id=line.id,
                acknowledgement_id=acknowledgement.id,
                promised_date=promised_date,
                lifecycle_status="Current",
            ))

            create_audit_log(
                "Acknowledgement", acknowledgement.id,
                "Supplier Acknowledgement Recorded",
                new_value={
                    "accepted_quantity": accepted,
                    "rejected_quantity": rejected,
                    "promised_date": str(promised_date)
                }
            )

            create_notification(
                "New Acknowledgement",
                f"New acknowledgement received for {line.item_code}. Review the updated supply information.",
                related_po_id=line.po_id
            )

            db.session.commit()
            flash("Acknowledgement recorded successfully.", "success")

        except (ValueError, KeyError):
            db.session.rollback()
            flash("Please enter valid acknowledgement details.", "error")

        return redirect(url_for("acknowledgements"))

    acknowledgement_list = Acknowledgement.query.order_by(
        Acknowledgement.id.desc()
    ).all()

    lines = POLine.query.order_by(POLine.id.desc()).all()

    return render_template(
        "acknowledgements.html",
        acknowledgements=acknowledgement_list,
        lines=lines
    )


# =====================================================
# MODULE 4: CHANGE REQUESTS
# =====================================================

def evaluate_change_thresholds(line, new_quantity, new_price, new_delivery_date):
    """Return approval triggers using documented, configurable policy thresholds.

    Changes are always held in Pending until a planner records an Approval.
    These thresholds make the reason for review explicit and auditable.
    """
    from datetime import timedelta
    quantity_limit = float(current_app.config.get("QTY_CHANGE_APPROVAL_PERCENT", 10))
    price_limit = float(current_app.config.get("PRICE_CHANGE_APPROVAL_PERCENT", 5))
    date_limit = int(current_app.config.get("DATE_CHANGE_APPROVAL_DAYS", 7))
    reasons = []
    if line.requested_quantity and abs(new_quantity - line.requested_quantity) / line.requested_quantity * 100 > quantity_limit:
        reasons.append(f"quantity change exceeds {quantity_limit:g}%")
    if line.unit_price and abs(new_price - line.unit_price) / line.unit_price * 100 > price_limit:
        reasons.append(f"price change exceeds {price_limit:g}%")
    if abs((new_delivery_date - line.requested_delivery_date).days) > date_limit:
        reasons.append(f"delivery date changes by more than {date_limit} days")
    return reasons


@route("/change-requests", methods=["GET", "POST"])
def change_requests():

    if request.method == "POST":
        try:
            po_line_id = int(request.form["po_line_id"])
            line = db.session.get(POLine, po_line_id)

            if not line:
                flash("Invalid purchase order line.", "error")
                return redirect(url_for("change_requests"))

            new_quantity = request.form.get("new_quantity")
            new_price = request.form.get("new_price")
            new_delivery_date = request.form.get("new_delivery_date")

            if new_quantity:
                new_quantity = int(new_quantity)
                if new_quantity <= 0:
                    raise ValueError()

            if new_price:
                new_price = float(new_price)
                if new_price < 0:
                    raise ValueError()

            if new_delivery_date:
                new_delivery_date = parse_date(new_delivery_date)

            threshold_reasons = evaluate_change_thresholds(
                line,
                new_quantity if new_quantity is not None else line.requested_quantity,
                new_price if new_price is not None else line.unit_price,
                new_delivery_date if new_delivery_date is not None else line.requested_delivery_date,
            )
            submitted_reason = (request.form.get("reason") or "Supplier change request").strip()
            if threshold_reasons:
                submitted_reason += " | Approval triggers: " + "; ".join(threshold_reasons)

            change = ChangeRequest(
                change_number=generate_number(ChangeRequest, "CHG"),
                po_line_id=line.id,
                old_quantity=line.requested_quantity,
                new_quantity=new_quantity if new_quantity is not None else line.requested_quantity,
                old_price=line.unit_price,
                new_price=new_price if new_price is not None else line.unit_price,
                old_delivery_date=line.requested_delivery_date,
                new_delivery_date=new_delivery_date if new_delivery_date is not None else line.requested_delivery_date,
                reason=submitted_reason,
                status="Pending"
            )

            db.session.add(change)
            db.session.flush()

            create_audit_log(
                "ChangeRequest", change.id, "Change Request Created",
                new_value=change.change_number
            )

            db.session.commit()
            flash("Change request submitted for approval.", "success")

        except (ValueError, KeyError):
            db.session.rollback()
            flash("Please enter valid change request details.", "error")

        return redirect(url_for("change_requests"))

    changes = ChangeRequest.query.order_by(ChangeRequest.id.desc()).all()
    lines = POLine.query.all()
    approvals = Approval.query.order_by(Approval.decision_date.desc()).all()

    return render_template(
        "change_requests.html",
        changes=changes,
        lines=lines,
        approvals=approvals
    )


# =====================================================
# MODULE 4: APPROVE OR REJECT CHANGE REQUEST
# =====================================================

@route("/change-requests/<int:change_id>/decision", methods=["POST"])
def change_requests_decision(change_id):

    change = db.session.get(ChangeRequest, change_id)

    if not change:
        flash("Change request not found.", "error")
        return redirect(url_for("change_requests"))

    if change.status != "Pending":
        flash("This change request has already been processed.", "error")
        return redirect(url_for("change_requests"))

    line = db.session.get(POLine, change.po_line_id)

    if not line:
        flash("Purchase order line not found.", "error")
        return redirect(url_for("change_requests"))

    # Check whether the original values are still current.
    if (
        line.requested_quantity != change.old_quantity
        or line.unit_price != change.old_price
        or line.requested_delivery_date != change.old_delivery_date
    ):
        flash("Stale change request. The purchase order line has changed.", "error")
        return redirect(url_for("change_requests"))

    decision = request.form.get("decision", "").strip()
    reviewer = request.form.get("reviewer", "Planner").strip()
    comments = request.form.get("comments", "")

    if decision not in ["Approved", "Rejected"]:
        flash("Invalid approval decision.", "error")
        return redirect(url_for("change_requests"))

    old_values = {
        "quantity": line.requested_quantity,
        "price": line.unit_price,
        "delivery_date": str(line.requested_delivery_date)
    }

    if decision == "Approved":

        line.requested_quantity = change.new_quantity
        line.unit_price = change.new_price
        line.requested_delivery_date = change.new_delivery_date

        change.status = "Approved"

    else:
        change.status = "Rejected"

    approval = Approval(
        change_request_id=change.id,
        decision=decision,
        reviewer=reviewer,
        comments=comments
    )

    db.session.add(approval)
    db.session.flush()

    create_audit_log(
        "ChangeRequest",
        change.id,
        f"Change {decision}",
        old_value=old_values,
        new_value={
            "quantity": change.new_quantity,
            "price": change.new_price,
            "delivery_date": str(change.new_delivery_date)
        },
        performed_by=reviewer
    )

    if decision == "Approved":
        create_notification(
            "Approved Change",
            f"Change {change.change_number} was approved. Review the affected inventory plan.",
            related_po_id=line.po_id,
            related_change_id=change.id
        )

    db.session.commit()

    flash(f"Change request {decision.lower()} successfully.", "success")
    return redirect(url_for("change_requests"))


# =====================================================
# MODULE 5: INVENTORY PLANNING
# =====================================================

@route("/inventory-planning")
def inventory_planning():

    plans = InventoryPlan.query.order_by(InventoryPlan.id.desc()).all()

    production_orders = ProductionOrder.query.order_by(
        ProductionOrder.required_date.asc(),
        ProductionOrder.id.asc()
    ).all()
    plans_by_line = {plan.po_line_id: plan for plan in plans}
    remaining_supply_by_line = {
        plan.po_line_id: max(0, plan.projected_quantity or 0)
        for plan in plans
    }

    results = []

    for production in production_orders:
        plan = plans_by_line.get(production.po_line_id)
        projected = plan.projected_quantity if plan else 0
        expected_date = plan.expected_delivery_date if plan else None

        if expected_date is None:
            delivery_timing = "Unknown"
        elif expected_date > production.required_date:
            delivery_timing = "Delivery Delay"
        else:
            delivery_timing = "On Time"

        remaining_supply = remaining_supply_by_line.get(
            production.po_line_id, 0
        )
        if expected_date is not None and expected_date <= production.required_date:
            allocated_quantity = min(
                remaining_supply,
                max(0, production.required_quantity)
            )
            remaining_supply_by_line[production.po_line_id] = (
                remaining_supply - allocated_quantity
            )
        else:
            allocated_quantity = 0

        shortage = max(
            0,
            production.required_quantity - allocated_quantity
        )

        results.append({
            "production": production,
            "plan": plan,
            "projected_quantity": projected,
            "allocated_quantity": allocated_quantity,
            "shortage": shortage,
            "expected_delivery_date": expected_date,
            "delivery_timing": delivery_timing
        })

    lines = POLine.query.all()

    return render_template(
        "inventory_planning.html",
        plans=plans,
        production_orders=production_orders,
        results=results,
        lines=lines
    )


# =====================================================
# MODULE 5: REFRESH INVENTORY PLAN
# =====================================================

@route("/inventory-planning/refresh", methods=["POST"])
def refresh_inventory():

    try:
        lines = POLine.query.all()

        for line in lines:

            latest_ack = Acknowledgement.query.filter_by(
                po_line_id=line.id,
                validation_status="Valid"
            ).order_by(
                Acknowledgement.revision_number.desc()
            ).first()

            plan = InventoryPlan.query.filter_by(
                po_line_id=line.id
            ).first()

            if not plan:
                plan = InventoryPlan(
                    po_line_id=line.id
                )
                db.session.add(plan)

            old_quantity = plan.projected_quantity
            old_date = plan.expected_delivery_date
            old_status = plan.supply_status

            if latest_ack:
                plan.projected_quantity = latest_ack.accepted_quantity
                plan.expected_delivery_date = latest_ack.promised_date
                if latest_ack.accepted_quantity > line.requested_quantity:
                    plan.supply_status = "Needs Review"
                else:
                    plan.supply_status = "Confirmed"
            else:
                plan.projected_quantity = 0
                plan.expected_delivery_date = None
                plan.supply_status = "Awaiting Acknowledgement"

            plan.last_updated = datetime.utcnow()

            if (
                old_quantity != plan.projected_quantity
                or old_date != plan.expected_delivery_date
                or old_status != plan.supply_status
            ):
                create_audit_log(
                    "InventoryPlan",
                    line.id,
                    "Inventory Plan Refreshed",
                    old_value={
                        "quantity": old_quantity,
                        "date": str(old_date),
                        "status": old_status
                    },
                    new_value={
                        "quantity": plan.projected_quantity,
                        "date": str(plan.expected_delivery_date),
                        "status": plan.supply_status
                    }
                )

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Inventory planning refresh failed.")
        flash(
            "Inventory refresh failed because of a database error. "
            "No inventory changes were saved; please try again.",
            "error"
        )
        return redirect(url_for("inventory_planning"))

    flash("Inventory planning refreshed successfully.", "success")

    return redirect(url_for("inventory_planning"))


# =====================================================
# MODULE 5: CREATE PRODUCTION ORDER
# =====================================================

@route("/production-orders", methods=["POST"])
def create_production_order():

    try:
        po_line_id = int(request.form["po_line_id"])
        quantity = int(request.form["required_quantity"])
        required_date = parse_date(request.form["required_date"])

        line = db.session.get(POLine, po_line_id)

        if not line or quantity <= 0:
            flash("Select a valid item and positive quantity.", "error")
            return redirect(url_for("inventory_planning"))

        production = ProductionOrder(
            production_number=generate_number(ProductionOrder, "PROD"),
            po_line_id=po_line_id,
            required_quantity=quantity,
            required_date=required_date,
            status="Planned"
        )

        db.session.add(production)
        db.session.flush()

        create_audit_log(
            "ProductionOrder",
            production.id,
            "Production Order Created",
            new_value={
                "production_number": production.production_number,
                "po_line_id": line.id,
                "item_code": line.item_code,
                "required_quantity": production.required_quantity,
                "required_date": str(production.required_date),
                "status": production.status
            }
        )

        db.session.commit()
        flash("Production order created successfully.", "success")

    except (ValueError, KeyError):
        db.session.rollback()
        flash("Please enter valid production order details.", "error")
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Production order creation failed.")
        flash(
            "Production order could not be saved because of a database error. "
            "Please try again.",
            "error"
        )

    return redirect(url_for("inventory_planning"))


# =====================================================
# MODULE 6: NOTIFICATIONS PAGE
# =====================================================

@route("/notifications")
def notifications():

    notification_list = Notification.query.order_by(
        Notification.id.desc()
    ).all()

    return render_template(
        "notifications.html",
        notifications=notification_list
    )


# =====================================================
# MODULE 6: MARK NOTIFICATION AS READ
# =====================================================

@route("/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):

    notification = db.session.get(Notification, notification_id)

    if notification:
        old_status = notification.status
        notification.status = "Read"

        create_audit_log(
            "Notification",
            notification.id,
            "Notification Marked as Read",
            old_value=old_status,
            new_value="Read"
        )

        db.session.commit()
        flash("Notification marked as read.", "success")

    return redirect(url_for("notifications"))


# =====================================================
# MODULE 6: AUDIT HISTORY PAGE
# =====================================================

@route("/audit-history")
def audit_history():

    events = AuditLog.query.order_by(
        AuditLog.id.desc()
    ).all()

    return render_template(
        "audit_history.html",
        events=events
    )


# =====================================================
# CREATE DATABASE TABLES AND START APPLICATION
# =====================================================

app = create_app()

if __name__ == "__main__":

    with app.app_context():
        db.create_all()

    app.run(debug=True)
