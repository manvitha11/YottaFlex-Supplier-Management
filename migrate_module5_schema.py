"""Safely migrate the Module 5 SQLite columns after an explicit --apply."""

import argparse
from datetime import date, datetime
from pathlib import Path
import sqlite3
import sys


ALLOWED_SUPPLY_STATUSES = (
    "Awaiting Acknowledgement",
    "Needs Review",
    "Confirmed",
)


def open_read_only(database_path):
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def table_names(connection):
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def row_counts(connection):
    counts = {}
    for table in table_names(connection):
        escaped_name = table.replace('"', '""')
        counts[table] = connection.execute(
            f'SELECT COUNT(*) FROM "{escaped_name}"'
        ).fetchone()[0]
    return counts


def require_current_schema(connection):
    expected = {
        "acknowledgement": {
            "id": ("INTEGER", 1, 1),
            "acknowledgement_number": ("VARCHAR(50)", 1, 0),
            "po_id": ("INTEGER", 1, 0),
            "po_line_id": ("INTEGER", 1, 0),
            "revision_number": ("INTEGER", 1, 0),
            "accepted_quantity": ("INTEGER", 1, 0),
            "rejected_quantity": ("INTEGER", 1, 0),
            "promised_date": ("DATE", 1, 0),
            "supplier_note": ("VARCHAR(300)", 0, 0),
            "validation_status": ("VARCHAR(50)", 0, 0),
            "recorded_date": ("DATE", 0, 0),
        },
        "inventory_plan": {
            "id": ("INTEGER", 1, 1),
            "po_line_id": ("INTEGER", 1, 0),
            "projected_quantity": ("INTEGER", 1, 0),
            "expected_delivery_date": ("DATE", 1, 0),
            "supply_status": ("VARCHAR(40)", 0, 0),
            "last_updated": ("DATE", 0, 0),
        },
    }
    expected_foreign_keys = {
        "acknowledgement": {
            ("po_id", "purchase_order", "id"),
            ("po_line_id", "po_line", "id"),
        },
        "inventory_plan": {
            ("po_line_id", "po_line", "id"),
        },
    }
    expected_unique_columns = {
        "acknowledgement": {("acknowledgement_number",)},
        "inventory_plan": {("po_line_id",)},
    }

    for table, columns in expected.items():
        if table not in table_names(connection):
            raise RuntimeError(f"Required table is missing: {table}")

        actual = {
            row[1]: (row[2].upper(), row[3], row[5])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if actual != columns:
            raise RuntimeError(
                f"Unexpected columns or constraints for {table}: "
                f"found {actual!r}, expected {columns!r}. "
                "Review the database before migrating."
            )

        foreign_keys = {
            (row[3], row[2], row[4])
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            )
        }
        if foreign_keys != expected_foreign_keys[table]:
            raise RuntimeError(
                f"Unexpected foreign keys for {table}: {foreign_keys!r}"
            )

        unique_columns = set()
        for index in connection.execute(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            if index[2]:
                unique_columns.add(
                    tuple(
                        row[2]
                        for row in connection.execute(
                            f'PRAGMA index_info("{index[1]}")'
                        )
                    )
                )
        if unique_columns != expected_unique_columns[table]:
            raise RuntimeError(
                f"Unexpected unique indexes for {table}: {unique_columns!r}"
            )


def normalize_datetime(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as error:
            raise RuntimeError(
                f"Cannot safely convert inventory_plan.last_updated value "
                f"{value!r} to DATETIME."
            ) from error

    if parsed.tzinfo is not None:
        raise RuntimeError(
            "Timezone-aware last_updated values need an explicit conversion "
            "rule before this migration can continue."
        )
    return parsed.isoformat(sep=" ", timespec="microseconds")


def get_table_records(connection, table, columns):
    selected_columns = ", ".join(f'"{column}"' for column in columns)
    return connection.execute(
        f'SELECT {selected_columns} FROM "{table}" ORDER BY id'
    ).fetchall()


def capture_objects(connection, table):
    return connection.execute(
        "SELECT type, sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL ORDER BY type, name",
        (table,),
    ).fetchall()


def assert_no_external_references(connection):
    for table in ("acknowledgement", "inventory_plan"):
        for referencing_table in table_names(connection):
            if referencing_table == table:
                continue
            foreign_keys = connection.execute(
                f'PRAGMA foreign_key_list("{referencing_table}")'
            ).fetchall()
            if any(row[2] == table for row in foreign_keys):
                raise RuntimeError(
                    f"{referencing_table} references {table}. "
                    "The migration script must be reviewed for that schema."
                )


def validate_database(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Database integrity check failed: {integrity}")

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        raise RuntimeError(
            f"Database has foreign-key violations: {foreign_key_errors!r}"
        )

    require_current_schema(connection)
    assert_no_external_references(connection)

    unknown_statuses = connection.execute(
        "SELECT id, supply_status FROM inventory_plan "
        "WHERE supply_status IS NOT NULL "
        "AND TRIM(supply_status) <> '' "
        "AND supply_status NOT IN (?, ?, ?)",
        ALLOWED_SUPPLY_STATUSES,
    ).fetchall()
    if unknown_statuses:
        raise RuntimeError(
            "Inventory plans contain unrecognized supply statuses. "
            f"Review these rows before migrating: {unknown_statuses!r}"
        )


def validate_migrated_database(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Database integrity check failed: {integrity}")

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        raise RuntimeError(
            f"Database has foreign-key violations: {foreign_key_errors!r}"
        )

    expected = {
        "acknowledgement": {
            "id": ("INTEGER", 1, 1),
            "acknowledgement_number": ("VARCHAR(50)", 1, 0),
            "po_id": ("INTEGER", 1, 0),
            "po_line_id": ("INTEGER", 1, 0),
            "revision_number": ("INTEGER", 1, 0),
            "accepted_quantity": ("INTEGER", 1, 0),
            "rejected_quantity": ("INTEGER", 1, 0),
            "promised_date": ("DATE", 0, 0),
            "supplier_note": ("VARCHAR(300)", 0, 0),
            "validation_status": ("VARCHAR(50)", 0, 0),
            "recorded_date": ("DATE", 0, 0),
        },
        "inventory_plan": {
            "id": ("INTEGER", 1, 1),
            "po_line_id": ("INTEGER", 1, 0),
            "projected_quantity": ("INTEGER", 1, 0),
            "expected_delivery_date": ("DATE", 0, 0),
            "supply_status": ("VARCHAR(50)", 1, 0),
            "last_updated": ("DATETIME", 0, 0),
        },
    }
    expected_foreign_keys = {
        "acknowledgement": {
            ("po_id", "purchase_order", "id"),
            ("po_line_id", "po_line", "id"),
        },
        "inventory_plan": {
            ("po_line_id", "po_line", "id"),
        },
    }
    expected_unique_columns = {
        "acknowledgement": {("acknowledgement_number",)},
        "inventory_plan": {("po_line_id",)},
    }

    for table, columns in expected.items():
        if table not in table_names(connection):
            raise RuntimeError(f"Required table is missing: {table}")

        actual = {
            row[1]: (row[2].upper(), row[3], row[5])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if actual != columns:
            raise RuntimeError(
                f"Unexpected migrated columns or constraints for {table}: "
                f"found {actual!r}, expected {columns!r}."
            )

        foreign_keys = {
            (row[3], row[2], row[4])
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            )
        }
        if foreign_keys != expected_foreign_keys[table]:
            raise RuntimeError(
                f"Unexpected migrated foreign keys for {table}: "
                f"{foreign_keys!r}"
            )

        unique_columns = set()
        for index in connection.execute(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            if index[2]:
                unique_columns.add(
                    tuple(
                        row[2]
                        for row in connection.execute(
                            f'PRAGMA index_info("{index[1]}")'
                        )
                    )
                )
        if unique_columns != expected_unique_columns[table]:
            raise RuntimeError(
                f"Unexpected migrated unique indexes for {table}: "
                f"{unique_columns!r}"
            )

    invalid_statuses = connection.execute(
        "SELECT id, supply_status FROM inventory_plan "
        "WHERE supply_status IS NULL "
        "OR TRIM(supply_status) = '' "
        "OR supply_status NOT IN (?, ?, ?)",
        ALLOWED_SUPPLY_STATUSES,
    ).fetchall()
    if invalid_statuses:
        raise RuntimeError(
            "Migrated inventory plans contain missing or unrecognized "
            f"supply statuses: {invalid_statuses!r}"
        )

    assert_no_external_references(connection)


def create_pre_migration_backup(database_path, backup_directory):
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_directory / f"yottaflex_before_module5_{timestamp}.db"
    if backup_path.exists():
        raise RuntimeError(f"Refusing to overwrite backup: {backup_path}")

    source = open_read_only(database_path)
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

    verification = open_read_only(backup_path)
    try:
        integrity = verification.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(
                f"New pre-migration backup failed integrity check: {integrity}"
            )
    finally:
        verification.close()
    return backup_path


def rebuild_tables(connection):
    acknowledgement_columns = (
        "id",
        "acknowledgement_number",
        "po_id",
        "po_line_id",
        "revision_number",
        "accepted_quantity",
        "rejected_quantity",
        "promised_date",
        "supplier_note",
        "validation_status",
        "recorded_date",
    )
    inventory_columns = (
        "id",
        "po_line_id",
        "projected_quantity",
        "expected_delivery_date",
        "supply_status",
        "last_updated",
    )

    acknowledgement_records = get_table_records(
        connection, "acknowledgement", acknowledgement_columns
    )
    inventory_records = get_table_records(
        connection, "inventory_plan", inventory_columns
    )
    acknowledgement_objects = capture_objects(connection, "acknowledgement")
    inventory_objects = capture_objects(connection, "inventory_plan")
    before_counts = row_counts(connection)

    expected_inventory_records = []
    for record in inventory_records:
        values = list(record)
        status_index = inventory_columns.index("supply_status")
        date_index = inventory_columns.index("last_updated")
        if values[status_index] is None or not str(values[status_index]).strip():
            values[status_index] = "Awaiting Acknowledgement"
        values[date_index] = normalize_datetime(values[date_index])
        expected_inventory_records.append(tuple(values))

    connection.execute(
        """
        CREATE TABLE acknowledgement__module5_new (
            id INTEGER NOT NULL,
            acknowledgement_number VARCHAR(50) NOT NULL,
            po_id INTEGER NOT NULL,
            po_line_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            accepted_quantity INTEGER NOT NULL,
            rejected_quantity INTEGER NOT NULL,
            promised_date DATE,
            supplier_note VARCHAR(300),
            validation_status VARCHAR(50),
            recorded_date DATE,
            PRIMARY KEY (id),
            UNIQUE (acknowledgement_number),
            FOREIGN KEY(po_id) REFERENCES purchase_order (id),
            FOREIGN KEY(po_line_id) REFERENCES po_line (id)
        )
        """
    )
    connection.execute(
        f"""
        INSERT INTO acknowledgement__module5_new
        ({", ".join(f'"{column}"' for column in acknowledgement_columns)})
        SELECT {", ".join(f'"{column}"' for column in acknowledgement_columns)}
        FROM acknowledgement
        """
    )

    status_check = ", ".join(
        "'" + status.replace("'", "''") + "'"
        for status in ALLOWED_SUPPLY_STATUSES
    )
    connection.execute(
        f"""
        CREATE TABLE inventory_plan__module5_new (
            id INTEGER NOT NULL,
            po_line_id INTEGER NOT NULL,
            projected_quantity INTEGER NOT NULL,
            expected_delivery_date DATE,
            supply_status VARCHAR(50) NOT NULL
                DEFAULT 'Awaiting Acknowledgement'
                CHECK (supply_status IN ({status_check})),
            last_updated DATETIME,
            PRIMARY KEY (id),
            UNIQUE (po_line_id),
            FOREIGN KEY(po_line_id) REFERENCES po_line (id)
        )
        """
    )

    for record in expected_inventory_records:
        placeholders = ", ".join("?" for _ in inventory_columns)
        quoted_columns = ", ".join(
            f'"{column}"' for column in inventory_columns
        )
        connection.execute(
            f"INSERT INTO inventory_plan__module5_new "
            f"({quoted_columns}) VALUES ({placeholders})",
            record,
        )

    connection.execute("DROP TABLE acknowledgement")
    connection.execute(
        "ALTER TABLE acknowledgement__module5_new RENAME TO acknowledgement"
    )
    connection.execute("DROP TABLE inventory_plan")
    connection.execute(
        "ALTER TABLE inventory_plan__module5_new RENAME TO inventory_plan"
    )

    for object_type, sql in acknowledgement_objects + inventory_objects:
        connection.execute(sql)

    after_counts = row_counts(connection)
    if before_counts != after_counts:
        raise RuntimeError(
            f"Table row counts changed: before={before_counts!r}, "
            f"after={after_counts!r}"
        )

    migrated_acknowledgements = get_table_records(
        connection, "acknowledgement", acknowledgement_columns
    )
    if acknowledgement_records != migrated_acknowledgements:
        raise RuntimeError(
            "Acknowledgement rows changed unexpectedly during migration."
        )

    migrated_inventory = get_table_records(
        connection, "inventory_plan", inventory_columns
    )
    if expected_inventory_records != migrated_inventory:
        raise RuntimeError(
            "Inventory plan rows differ from the documented status/date "
            "normalization."
        )

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        raise RuntimeError(
            f"Foreign-key check failed after migration: {foreign_key_errors!r}"
        )

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(
            f"Integrity check failed after migration: {integrity}"
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or explicitly migrate Module 5 SQLite columns. "
            "Without --apply, this script makes no changes."
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="SQLite database to inspect or migrate.",
    )
    parser.add_argument(
        "--verified-backup",
        required=True,
        type=Path,
        help="Previously verified backup to check before migration.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. Without this flag the script is read-only.",
    )
    arguments = parser.parse_args()

    database_path = arguments.database.resolve()
    verified_backup = arguments.verified_backup.resolve()
    if not database_path.is_file():
        raise RuntimeError(f"Database does not exist: {database_path}")
    if not verified_backup.is_file():
        raise RuntimeError(f"Verified backup does not exist: {verified_backup}")
    if database_path == verified_backup:
        raise RuntimeError("The database and verified backup must be different files.")

    backup_connection = open_read_only(verified_backup)
    try:
        backup_integrity = backup_connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        if backup_integrity != "ok":
            raise RuntimeError(
                f"Verified backup failed integrity check: {backup_integrity}"
            )
    finally:
        backup_connection.close()

    preflight = open_read_only(database_path)
    try:
        validate_database(preflight)
        original_counts = row_counts(preflight)
    finally:
        preflight.close()

    if not arguments.apply:
        print("Preflight passed. No database changes were made.")
        print(f"Database: {database_path}")
        print(f"Verified recovery reference: {verified_backup}")
        print(f"Tables and row counts: {original_counts}")
        print("To migrate a reviewed copy, rerun with --apply and that copy's path.")
        return

    backup_directory = database_path.parent.parent / "database_backups"
    fresh_backup = create_pre_migration_backup(
        database_path, backup_directory
    )
    print(f"Fresh pre-migration backup created: {fresh_backup}")

    connection = sqlite3.connect(str(database_path), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        validate_database(connection)
        rebuild_tables(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    verification = open_read_only(database_path)
    try:
        validate_migrated_database(verification)
        final_counts = row_counts(verification)
    finally:
        verification.close()

    if original_counts != final_counts:
        raise RuntimeError(
            "Post-migration row counts differ from the pre-migration counts."
        )

    print("Migration and post-migration checks passed.")
    print(f"Database: {database_path}")
    print(f"Table row counts: {final_counts}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"Migration stopped safely: {error}", file=sys.stderr)
        raise SystemExit(1)
