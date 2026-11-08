# YottaFlex Data Dictionary

All IDs below are immutable database primary keys unless a business key is explicitly identified. Dates use ISO `YYYY-MM-DD`; timestamps use UTC. Quantities are integer units; prices are decimal currency amounts (the project currently stores prices as SQL `FLOAT`, so use two decimal places in UI/export formatting).

| Entity | Field | Type | Required | Allowed values / unit | Uniqueness / validation |
|---|---|---|---|---|---|
| Supplier | id | Integer PK | Yes | Positive integer | Primary key |
| Supplier | supplier_code | String(50) | Yes | `SUP-...` | Unique, non-empty |
| Supplier | supplier_name | String(150) | Yes | Text | Non-empty |
| Supplier | email | String(150) | No | Email | Validate format when supplied |
| Supplier | phone | String(30) | No | Phone text | Normalize; do not use as key |
| Supplier | status | String(30) | Yes | Active, Inactive, On Hold | Default Active |
| PurchaseOrder | id | Integer PK | Yes | Positive integer | Primary key |
| PurchaseOrder | po_number | String(50) | Yes | `PO-YYYY-000001` | Unique |
| PurchaseOrder | supplier_id | Integer FK | Yes | Supplier.id | Must reference existing supplier |
| PurchaseOrder | order_date | Date | Yes | ISO date | Valid date |
| PurchaseOrder | status | String(30) | Yes | Created, Open, Closed, Cancelled | Default Created |
| POLine | id | Integer PK | Yes | Positive integer | Primary key |
| POLine | po_id | Integer FK | Yes | PurchaseOrder.id | Must reference existing PO |
| POLine | item_code | String(50) | Yes | Product code | Non-empty |
| POLine | description | String(200) | No | Text | Optional description |
| POLine | requested_quantity | Integer | Yes | Units | Greater than zero |
| POLine | unit_price | Float | Yes | Currency/unit | Zero or greater |
| POLine | requested_delivery_date | Date | Yes | ISO date | On/after PO order date |
| Acknowledgement | id | Integer PK | Yes | Positive integer | Primary key |
| Acknowledgement | acknowledgement_number | String(100) | Yes | `ACK-<line>-<revision>` | Unique |
| Acknowledgement | po_id | Integer FK | Yes | PurchaseOrder.id | Must match the PO owning po_line_id |
| Acknowledgement | po_line_id | Integer FK | Yes | POLine.id | Must reference existing line |
| Acknowledgement | revision_number | Integer | Yes | 1, 2, ... | Unique per PO line; strictly increasing |
| Acknowledgement | accepted_quantity | Integer | Yes | Units | Zero or greater |
| Acknowledgement | rejected_quantity | Integer | Yes | Units | Zero or greater; accepted + rejected must not exceed requested quantity |
| Acknowledgement | promised_date | Date | No | ISO date | On/after PO order date for valid records |
| Acknowledgement | validation_status | String(30) | Yes | Valid, Invalid | Invalid retained only for labelled test scenarios |
| Acknowledgement | recorded_date | DateTime | Yes | UTC timestamp | Defaults to creation time |
| PromiseDate | id | Integer PK | Yes | Positive integer | Primary key |
| PromiseDate | po_line_id | Integer FK | Yes | POLine.id | Must reference existing line; indexed |
| PromiseDate | acknowledgement_id | Integer FK | Yes | Acknowledgement.id | Unique; one snapshot per acknowledgement |
| PromiseDate | promised_date | Date | Yes | ISO date | Date promised by supplier |
| PromiseDate | effective_from | DateTime | Yes | UTC timestamp | Start of validity |
| PromiseDate | effective_to | DateTime | No | UTC timestamp | Null while current; set when superseded |
| PromiseDate | lifecycle_status | String(20) | Yes | Current, Superseded | One current snapshot per line in normal workflow |
| ChangeRequest | id | Integer PK | Yes | Positive integer | Primary key |
| ChangeRequest | change_number | String(100) | Yes | `CHG-...` | Unique |
| ChangeRequest | po_line_id | Integer FK | Yes | POLine.id | Must reference existing line |
| ChangeRequest | old_quantity/new_quantity | Integer | Yes | Units | New quantity greater than zero |
| ChangeRequest | old_price/new_price | Float | Yes | Currency/unit | New price zero or greater |
| ChangeRequest | old_delivery_date/new_delivery_date | Date | Yes | ISO date | New date on/after PO order date |
| ChangeRequest | status | String(30) | Yes | Pending, Approved, Rejected | Default Pending; transition only from Pending |
| ChangeRequest | reason | Text | No | Text | Store threshold triggers and business reason |
| ChangeRequest | created_date | DateTime | Yes | UTC timestamp | Defaults to creation time |
| Approval | id | Integer PK | Yes | Positive integer | Primary key |
| Approval | change_request_id | Integer FK | Yes | ChangeRequest.id | Must reference existing change |
| Approval | decision | String(30) | Yes | Approved, Rejected | One final decision per processed request |
| Approval | reviewer | String(100) | No | User / role label | Capture responsible reviewer |
| Approval | comments | Text | No | Text | Decision rationale |
| Approval | decision_date | DateTime | Yes | UTC timestamp | Defaults to decision time |
| InventoryPlan | id | Integer PK | Yes | Positive integer | Primary key |
| InventoryPlan | po_line_id | Integer FK | Yes | POLine.id | Unique; one plan per PO line |
| InventoryPlan | projected_quantity | Integer | Yes | Units | Zero or greater; derived from valid supplier acknowledgement |
| InventoryPlan | expected_delivery_date | Date | No | ISO date | Derived from current valid promise |
| InventoryPlan | supply_status | String(50) | Yes | Awaiting Acknowledgement, Confirmed, Needs Review | Derived, not independently randomized |
| InventoryPlan | last_updated | DateTime | Yes | UTC timestamp | Update only when projection changes |
| ProductionOrder | id | Integer PK | Yes | Positive integer | Primary key |
| ProductionOrder | production_number | String(50) | Yes | `PROD-...` | Unique |
| ProductionOrder | po_line_id | Integer FK | Yes | POLine.id | Must reference existing line |
| ProductionOrder | required_quantity | Integer | Yes | Units | Greater than zero |
| ProductionOrder | required_date | Date | Yes | ISO date | Valid date |
| ProductionOrder | status | String(30) | Yes | Planned, Released, Completed, Cancelled | Default Planned |
| Notification | id | Integer PK | Yes | Positive integer | Primary key |
| Notification | notification_number | String(50) | Yes | `NOT-YYYY-000001` | Unique |
| Notification | notification_type | String(100) | Yes | Workflow event category | Non-empty |
| Notification | message | Text | Yes | Text | Non-empty |
| Notification | related_po_id | Integer FK | No | PurchaseOrder.id | Optional valid PO reference |
| Notification | related_change_id | Integer FK | No | ChangeRequest.id | Optional valid change reference |
| Notification | status | String(30) | Yes | Unread, Read | Default Unread |
| Notification | created_date | DateTime | Yes | UTC timestamp | Defaults to creation time |

## Relationships and cardinality

- Supplier 1:N PurchaseOrder (each PO has exactly one supplier).
- PurchaseOrder 1:N POLine (each line belongs to exactly one PO).
- PurchaseOrder 1:N Acknowledgement and POLine 1:N Acknowledgement (each acknowledgement belongs to one PO and one line; PO/line consistency is validated).
- Acknowledgement 1:0..1 PromiseDate (one immutable promise snapshot per acknowledgement); POLine 1:N PromiseDate across revisions.
- POLine 1:N ChangeRequest; each change request belongs to one line.
- ChangeRequest 1:0..1 Approval for a final decision; pending requests have no approval record.
- POLine 1:0..1 InventoryPlan (unique PO-line FK).
- POLine 1:N ProductionOrder.
- PurchaseOrder 1:N Notification and ChangeRequest 1:N Notification (both notification references are optional).
- AuditLog is a polymorphic event reference (`entity_type`, `entity_id`) and intentionally does not use a single FK because it spans multiple entity tables.

## Lifecycle and effective dates

- Acknowledgement revisions are append-only. A newer revision does not delete an older revision.
- PromiseDate snapshots are append-only. The former current row receives `effective_to` and `Superseded`; the new row becomes `Current`.
- ChangeRequest: Pending -> Approved OR Pending -> Rejected. Terminal requests cannot be decided again.
- Approval is a separate record; a change is not approved solely by changing a status field.
- InventoryPlan is a projection, not source-of-truth history. Its changes are captured in AuditLog.
- Approval review triggers default to quantity movement >10%, price movement >5%, or delivery-date movement >7 days. All changes require explicit approval in the current workflow.

## Known anomaly labels

`unapproved_change_updated_plan`, `wrong_po_line`, `early_promised_date`, and `older_revision_overwrite` are deliberate, labelled negative scenarios. Keep their issue type and ground-truth label in the separate ground-truth export. Never inject these labels into input feature files.
