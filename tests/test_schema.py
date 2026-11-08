import sqlite3

import pytest

from src.database.init_db import initialize_database
from src.database.schema import CORE_TABLES


def test_schema_creates_all_core_tables(tmp_path):
    db_path = tmp_path / "schema.sqlite"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert set(CORE_TABLES).issubset(tables)
    assert len(CORE_TABLES) == 11


def test_foreign_key_enforcement_rejects_orphan_shipment(tmp_path):
    db_path = tmp_path / "schema.sqlite"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO Shipment (
                    shipment_id, carrier_id, route_id, service_level_id,
                    origin_facility_id, destination_facility_id, created_at,
                    planned_pickup_time, planned_delivery_time, lifecycle_state
                ) VALUES ('SHP-2026-000001', 'missing-carrier', 'missing-route',
                    'missing-service', 'missing-origin', 'missing-destination',
                    '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z',
                    '2026-01-02T01:00:00Z', 'created')"""
            )
