
"""
Seed PostgreSQL with all TransitFlow mock data from train-mock-data/.

Usage:
    python skeleton/seed_postgres.py

Run AFTER docker-compose up -d.
You must first design and create your tables in databases/relational/schema.sql.
Safe to re-run: implement your inserts with ON CONFLICT DO NOTHING.
"""

import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

import psycopg2
from psycopg2.extras import execute_values

from argon2 import PasswordHasher

# ── resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR    = os.path.join(PROJECT_DIR, "train-mock-data")

sys.path.insert(0, PROJECT_DIR)
from skeleton import config as cfg


def load(filename):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def connect():
    return psycopg2.connect(
        host=cfg.PG_HOST,
        port=cfg.PG_PORT,
        dbname=cfg.PG_DB,
        user=cfg.PG_USER,
        password=cfg.PG_PASSWORD,
    )


def insert_many(cur, table, columns, rows):
    """Bulk insert with ON CONFLICT DO NOTHING. Returns row count inserted."""
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT DO NOTHING"
    )
    execute_values(cur, sql, rows)
    return cur.rowcount


# ── seeders ──────────────────────────────────────────────────────────────────

def seed_metro_stations(cur):
    data = load("metro_stations.json")
    columns = [
        "station_id", "name", "lines", "is_interchange_metro", 
        "interchange_metro_lines", "is_interchange_national_rail", 
        "interchange_national_rail_station_id"
    ]
    rows = []
    links = []
    for item in data:
        rows.append((
            item["station_id"],
            item["name"],
            item["lines"],
            item["is_interchange_metro"],
            item["interchange_metro_lines"],
            item["is_interchange_national_rail"],
            item["interchange_national_rail_station_id"]
        ))
        # Additional processing for adjacent stations to populate your custom metro_links table
        for adj in item.get("adjacent_stations", []):
            links.append((
                item["station_id"],
                adj["station_id"],
                adj["line"],
                adj["travel_time_min"]
            ))
            
    count = insert_many(cur, "metro_stations", columns, rows)
    link_count = insert_many(cur, "metro_links", ["from_station_id", "to_station_id", "line", "travel_time_min"], links)
    print(f"  - Seeded {count} metro stations and {link_count} links")


def seed_national_rail_stations(cur):
    data = load("national_rail_stations.json")
    columns = [
        "station_id", "name", "lines", "is_interchange_national_rail", 
        "interchange_national_rail_lines", "is_interchange_metro", 
        "interchange_metro_station_id"
    ]
    rows = []
    links = []
    for item in data:
        rows.append((
            item["station_id"],
            item["name"],
            item["lines"],
            item["is_interchange_national_rail"],
            item["interchange_national_rail_lines"],
            item["is_interchange_metro"],
            item["interchange_metro_station_id"]
        ))
        # Additional processing for adjacent stations to populate your custom national_rail_links table
        for adj in item.get("adjacent_stations", []):
            links.append((
                item["station_id"],
                adj["station_id"],
                adj["line"],
                adj["travel_time_min"]
            ))
            
    count = insert_many(cur, "national_rail_stations", columns, rows)
    link_count = insert_many(cur, "national_rail_links", ["from_station_id", "to_station_id", "line", "travel_time_min"], links)
    print(f"  - Seeded {count} national rail stations and {link_count} links")


def seed_metro_schedules(cur):
    data = load("metro_schedules.json")
    columns = [
        "schedule_id", "line", "direction", "origin_station_id", 
        "destination_station_id", "stops_in_order", "first_train_time", 
        "last_train_time", "travel_time_from_origin_min", "base_fare_usd", 
        "per_stop_rate_usd", "frequency_min", "operates_on"
    ]
    rows = []
    for item in data:
        rows.append((
            item["schedule_id"],
            item["line"],
            item["direction"],
            item["origin_station_id"],
            item["destination_station_id"],
            item["stops_in_order"],
            item["first_train_time"],
            item["last_train_time"],
            json.dumps(item["travel_time_from_origin_min"]),  # Convert to JSON string to match JSONB type
            item["base_fare_usd"],
            item["per_stop_rate_usd"],
            item["frequency_min"],
            item["operates_on"]
        ))
    count = insert_many(cur, "metro_schedules", columns, rows)
    print(f"  - Seeded {count} metro schedules")


def seed_national_rail_schedules(cur):
    data = load("national_rail_schedules.json")
    columns = [
        "schedule_id", "line", "service_type", "direction", 
        "origin_station_id", "destination_station_id", "stops_in_order", 
        "passed_through_stations", "first_train_time", "last_train_time", 
        "travel_time_from_origin_min", "fare_classes", "frequency_min", "operates_on"
    ]
    rows = []
    for item in data:
        rows.append((
            item["schedule_id"],
            item["line"],
            item["service_type"],
            item["direction"],
            item["origin_station_id"],
            item["destination_station_id"],
            item["stops_in_order"],
            item.get("passed_through_stations", []),
            item["first_train_time"],
            item["last_train_time"],
            json.dumps(item["travel_time_from_origin_min"]),
            json.dumps(item["fare_classes"]),
            item["frequency_min"],
            item["operates_on"]
        ))
    count = insert_many(cur, "national_rail_schedules", columns, rows)
    print(f"  - Seeded {count} national rail schedules")


def seed_seat_layouts(cur):
    data = load("national_rail_seat_layouts.json")
    columns = ["layout_id", "schedule_id", "coaches"]
    rows = []
    for item in data:
        rows.append((
            item["layout_id"],
            item["schedule_id"],
            json.dumps(item["coaches"])
        ))
    count = insert_many(cur, "national_rail_seat_layouts", columns, rows)
    print(f"  - Seeded {count} seat layouts")


def seed_users(cur):
    data = load("registered_users.json")
    ph = PasswordHasher()

    # Fields for registered_users, removing security questions (since they don't exist in this table schema)
    user_columns = [
        "user_id", "full_name", "email", "phone",
        "date_of_birth", "registered_at", "is_active"
    ]

    # Fields for user_credentials, adding secret_question and secret_answer_hash
    credential_columns = [
        "user_id", "password_hash", "secret_question", "secret_answer_hash"
    ]

    user_rows = []
    credential_rows = []

    for item in data:
        # 1. Prepare profile data to be written into registered_users
        user_rows.append((
            item["user_id"],
            item["full_name"],
            item["email"],
            item.get("phone"),
            item.get("date_of_birth"),
            item["registered_at"],
            item["is_active"]
        ))

        # Security Guard: Consistent with queries.py, convert password and security answers to Argon2 hashes in memory
        raw_password = item["password"]
        raw_answer = item.get("secret_answer", "").strip().lower()  # Convert to lowercase before hashing to prevent verification failures due to casing

        # 2. Prepare credential data to be written into user_credentials
        credential_rows.append((
            item["user_id"],
            ph.hash(raw_password),                       # Password HASH
            item.get("secret_question"),                  # Security question in plaintext
            ph.hash(raw_answer) if raw_answer else ""   # Security answer HASH
        ))

    # Execute batch insertion for the profile data table
    user_count = insert_many(
        cur,
        "registered_users",
        user_columns,
        user_rows
    )

    # Execute batch insertion for the credentials security table
    credential_count = insert_many(
        cur,
        "user_credentials",
        credential_columns,
        credential_rows
    )

    print(f"  - Seeded {user_count} registered users")
    print(f"  - Seeded {credential_count} user credentials")


def seed_national_rail_bookings(cur):
    data = load("bookings.json")
    columns = [
        "booking_id", "user_id", "schedule_id", "origin_station_id", 
        "destination_station_id", "travel_date", "departure_time", 
        "ticket_type", "fare_class", "coach", "seat_id", 
        "stops_travelled", "amount_usd", "status", "booked_at", "travelled_at"
    ]
    rows = []
    for item in data:
        rows.append((
            item["booking_id"],
            item["user_id"],
            item["schedule_id"],
            item["origin_station_id"],
            item["destination_station_id"],
            item["travel_date"],
            item["departure_time"],
            item["ticket_type"],
            item["fare_class"],
            item.get("coach"),
            item.get("seat_id"),
            item["stops_travelled"],
            item["amount_usd"],
            item["status"],
            item["booked_at"],
            item.get("travelled_at")
        ))
    count = insert_many(cur, "national_rail_bookings", columns, rows)
    print(f"  - Seeded {count} national rail bookings")


def seed_metro_travels(cur):
    data = load("metro_travel_history.json")
    columns = [
        "trip_id", "user_id", "schedule_id", "origin_station_id", 
        "destination_station_id", "travel_date", "ticket_type", 
        "day_pass_ref", "stops_travelled", "amount_usd", "status", 
        "purchased_at", "travelled_at"
    ]
    rows = []
    for item in data:
        rows.append((
            item["trip_id"],
            item["user_id"],
            item["schedule_id"],
            item["origin_station_id"],
            item["destination_station_id"],
            item["travel_date"],
            item["ticket_type"],
            item.get("day_pass_ref"),
            item.get("stops_travelled"),
            item["amount_usd"],
            item["status"],
            item.get("purchased_at"),
            item.get("travelled_at")
        ))
    count = insert_many(cur, "metro_travel_history", columns, rows)
    print(f"  - Seeded {count} metro travel histories")


def seed_payments(cur):
    data = load("payments.json")
    columns = ["payment_id", "booking_id", "amount_usd", "method", "status", "paid_at"]
    rows = []
    for item in data:
        rows.append((
            item["payment_id"],
            item["booking_id"],
            item["amount_usd"],
            item["method"],
            item["status"],
            item["paid_at"]
        ))
    count = insert_many(cur, "payments", columns, rows)
    print(f"  - Seeded {count} payments")


def seed_feedback(cur):
    data = load("feedback.json")
    columns = ["feedback_id", "booking_id", "user_id", "rating", "comment", "submitted_at"]
    rows = []
    for item in data:
        rows.append((
            item["feedback_id"],
            item["booking_id"],
            item["user_id"],
            item["rating"],
            item.get("comment"),
            item["submitted_at"]
        ))
    count = insert_many(cur, "feedback", columns, rows)
    print(f"  - Seeded {count} feedback entries")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to PostgreSQL...")
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Seeding tables (dependency order):")

        seed_national_rail_stations(cur)
        seed_metro_stations(cur)

        seed_users(cur)

        seed_national_rail_schedules(cur)
        seed_metro_schedules(cur)

        seed_seat_layouts(cur)

        seed_national_rail_bookings(cur)
        seed_metro_travels(cur)

        seed_payments(cur)
        seed_feedback(cur)

        conn.commit()
        print("\nAll done. Database seeded successfully.")

    except Exception as e:
        conn.rollback()
        print(f"\nError: {e}")
        raise

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()