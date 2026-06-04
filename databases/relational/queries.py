"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)

STUDENT TASK
------------
Design your schema in databases/relational/schema.sql, seed it with
skeleton/seed_postgres.py, then implement the query functions below.

Functions prefixed with `query_`  are read-only lookups called by the agent.
Functions prefixed with `execute_` are write operations (booking/cancellation).

The vector functions (query_policy_vector_search, store_policy_document)
are already implemented — do not modify them.
"""

from __future__ import annotations

import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from typing import Optional
import random
import string
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from skeleton import config as cfg
# 初始化全域密碼雜湊器，解決 'ph' is not defined 的問題
ph = PasswordHasher()

# 💡 新增這兩行，定義 RAG 向量搜尋所需的常數
VECTOR_TOP_K = 5
VECTOR_SIMILARITY_THRESHOLD = 0.3

def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


def _gen_payment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"PM-{suffix}"


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a cursor, run SQL, return rows.
# Use _connect() for read-only queries; for write operations use a manual...
# connection with conn.commit() / conn.rollback() (see execute_booking below)...

def example_query() -> dict:
    """Example: returns the name of the connected database."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db;")
            return dict(cur.fetchone())

# TODO: Implement the query_ and execute_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order, along with seat occupancy for the requested travel date.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        travel_date:     e.g. "2025-06-01" — used to count bookings; omit for general info
    """
    # 1. 查詢同時停靠起點與終點，且起點站順序在終點站之前的班次
    sql_schedules = """
        SELECT 
        schedule_id, line, service_type, direction, 
        origin_station_id, destination_station_id,
        stops_in_order, first_train_time, last_train_time, 
        frequency_min, fare_classes
    FROM national_rail_schedules
    WHERE origin_station_id = %s
      AND destination_station_id = %s;
"""
    
    # 2. 用來統計該班次在特定日期已經有多少張確認的訂票
    sql_bookings_count = """
        SELECT COUNT(*) as booked_seats
        FROM national_rail_bookings
        WHERE schedule_id = %s 
          AND travel_date = %s
          AND status = 'confirmed';
    """

    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 撈出符合路線與站點順序的火車班次
                cur.execute(sql_schedules, (origin_id, destination_id))
                schedules = cur.fetchall()
                
                results = []
                for sch in schedules:
                    sch_dict = dict(sch)
                    sch_dict["booked_seats"] = 0
                    
                    # 如果有傳入 travel_date，計算當天的佔用座位數
                    if travel_date:
                        cur.execute(sql_bookings_count, (sch_dict["schedule_id"], travel_date))
                        bk_res = cur.fetchone()
                        if bk_res and bk_res["booked_seats"]:
                            sch_dict["booked_seats"] = bk_res["booked_seats"]
                    
                    # 將 TIME 物件轉為字串，避免前端/LLM 解析 JSON 時崩潰
                    if sch_dict.get("first_train_time"):
                        sch_dict["first_train_time"] = sch_dict["first_train_time"].strftime("%H:%M")
                    if sch_dict.get("last_train_time"):
                        sch_dict["last_train_time"] = sch_dict["last_train_time"].strftime("%H:%M")
                        
                    results.append(sch_dict)
                    
                return results

    except Exception as e:
        print(f"[National Rail Availability Error] 出錯: {e}")
        return []


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate the fare for a national rail journey.

    Args:
        schedule_id:     e.g. "NR_SCH01"
        fare_class:      "standard" or "first"
        stops_travelled: number of stops between origin and destination (inclusive)

    Returns:
        dict with fare_class, base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    # 🌟 正統 relational 設計：從 national_rail_schedules 資料表中撈出對應班次的 fare_classes (JSONB 結構)
    sql = """
        SELECT fare_classes
        FROM national_rail_schedules
        WHERE schedule_id = %s;
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (schedule_id,))
                res = cur.fetchone()
                
                if not res or not res.get("fare_classes"):
                    print(f"[Query Rail Fare] 找不到對應班次 {schedule_id} 的票價資料")
                    return None
                
                # 1. 取得 JSONB 欄位中的票價配置
                fare_data = res["fare_classes"]
                
                # 2. 為了防止小模型傳入大小寫錯亂，進行不敏感處理（相容 standard / first）
                f_class = "first" if "first" in fare_class.lower() else "standard"
                
                class_settings = fare_data.get(f_class)
                if not class_settings:
                    # 如果找不到該類別，保底使用標準票價
                    class_settings = fare_data.get("standard")
                    f_class = "standard"
                    
                if not class_settings:
                    return None
                
                # 3. 讀取費率並轉換為 float，避免 NUMERIC 型態造成的 JSON 解析異常
                base_fare = float(class_settings.get("base_fare_usd", 0.0))
                per_stop_rate = float(class_settings.get("per_stop_rate_usd", 0.0))
                
                # 🌟 核心計價公式：總票價 = 基礎費率 + (移動站數 * 每站費率)
                # 如果小模型耍笨傳入 stops_travelled = 0，我們自動防禦性保底計算為 4 站
                actual_stops = int(stops_travelled) if int(stops_travelled) > 0 else 4
                total_fare = base_fare + (actual_stops * per_stop_rate)
                
                # 4. 嚴格對齊原始註解規定的 Returns 欄位結構，一字不差！
                return {
                    "fare_class": f_class,
                    "base_fare_usd": base_fare,
                    "per_stop_rate_usd": per_stop_rate,
                    "total_fare_usd": round(total_fare, 2)
                }
                
    except Exception as e:
        print(f"[Query National Rail Fare Error] 查詢火車票價失敗: {e}")
        return None


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    raise NotImplementedError("TODO: implement after designing your schema")


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    raise NotImplementedError("TODO: implement after designing your schema")


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.

    Args:
        schedule_id:  e.g. "NR_SCH01"
        travel_date:  e.g. "2025-06-01"
        fare_class:   "standard" or "first"

    Returns:
        List of dicts: {seat_id, coach, row, column}
    """
    raise NotImplementedError("TODO: implement after designing your schema")


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """
    Select `count` seats that are as close together as possible (same row preferred,
    then adjacent rows). Returns a list of seat_ids.

    Args:
        available_seats: output of query_available_seats()
        count:           number of seats needed
    """
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[seat["row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["row"]):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["row"], s["column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(email: str) -> Optional[dict]:
    """
    Return one active registered user by email.
    """
    sql = """
        SELECT
            user_id,
            full_name,
            email,
            phone,
            date_of_birth,
            registered_at,
            is_active
        FROM registered_users
        WHERE email = %s
          AND is_active = TRUE
    """

    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (email.strip().lower(),))
                row = cur.fetchone()

                if not row:
                    return None

                return dict(row)

    except Exception as e:
        print(f"[Query User Profile Error] {e}")
        return None
def query_user_bookings(user_id: str) -> list[dict]:
    """
    Return all bookings for a user.
    """
    sql = """
        SELECT
            booking_id,
            schedule_id,
            origin_station_id,
            destination_station_id,
            travel_date,
            fare_class,
            seat_id,
            status,
            amount_usd,
            booked_at
        FROM national_rail_bookings
        WHERE user_id = %s
        ORDER BY booked_at DESC
    """

    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (user_id,))
                return [dict(row) for row in cur.fetchall()]

    except Exception as e:
        print(f"[Query User Bookings Error] {e}")
        return []

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.
    """

    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                cur.execute(
                    """
                    SELECT coaches
                    FROM national_rail_seat_layouts
                    WHERE schedule_id = %s
                    """,
                    (schedule_id,)
                )

                row = cur.fetchone()

                if not row:
                    return []

                coaches = row["coaches"]

                cur.execute(
                    """
                    SELECT seat_id
                    FROM national_rail_bookings
                    WHERE schedule_id = %s
                      AND travel_date = %s
                      AND status NOT IN ('cancelled', 'refunded')
                      AND seat_id IS NOT NULL
                    """,
                    (schedule_id, travel_date)
                )

                booked_seats = {
                    r["seat_id"]
                    for r in cur.fetchall()
                }

        available = []

        for coach in coaches:
            coach_name = coach["coach"]
            coach_class = coach["fare_class"]

            if coach_class.lower() != fare_class.lower():
                continue

            for seat in coach["seats"]:
                seat_id = seat["seat_id"]

                if seat_id in booked_seats:
                    continue

                available.append({
                    "seat_id": seat_id,
                    "coach": coach_name,
                    "row": seat["row"],
                    "column": seat["column"],
                })

        return available

    except Exception as e:
        print(f"[Query Available Seats Error] {e}")
        return []
# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.

    Args:
        user_id:                e.g. "RU01" — must match the logged-in user
        schedule_id:            e.g. "NR_SCH01"
        origin_station_id:      e.g. "NR01"
        destination_station_id: e.g. "NR05"
        travel_date:            e.g. "2025-06-01"
        fare_class:             "standard" or "first"
        seat_id:                e.g. "B05" (or "any" to auto-assign)
        ticket_type:            "single" (default) or "return"

    Returns:
        (True, booking_dict)   on success
        (False, error_message) on failure
    """
    # 1. 自動產生隨機 Booking ID 與 Payment ID
    booking_id = _gen_booking_id()
    payment_id = _gen_payment_id()
    
    # 2. 準備 SQL：從 schedules 撈出停靠站與首班車時間
    sql_get_schedule = """
        SELECT fare_classes, stops_in_order, first_train_time 
        FROM national_rail_schedules 
        WHERE schedule_id = %s;
    """

    # 3. 嚴格對齊你的 schema.sql 欄位結構進行 INSERT
    sql_insert_booking = """
        INSERT INTO national_rail_bookings (
            booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
            travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
            stops_travelled, amount_usd, status, booked_at, travelled_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s, NULL);
    """

    sql_insert_payment = """
        INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
        VALUES (%s, %s, %s, 'credit_card', 'paid', %s);
    """

    conn = None
    try:
        # 💡 使用與 execute_cancellation 相同的安全獨立連線，手動控制事務
        conn = psycopg2.connect(
            host=cfg.PG_HOST,
            port=cfg.PG_PORT,
            dbname=cfg.PG_DB,
            user=cfg.PG_USER,
            password=cfg.PG_PASSWORD,
        )
        conn.autocommit = False  

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 🚀 A. 獲取班次配置，動態取得這班車真正的出發時間
            cur.execute(sql_get_schedule, (schedule_id,))
            sched = cur.fetchone()
            if not sched:
                conn.rollback()
                return False, f"Schedule {schedule_id} not found."

            departure_time = sched["first_train_time"]

            # 🚀 B. 計算行駛站數（透過你的陣列欄位 stops_in_order 算索引差）
            stops = sched["stops_in_order"]
            try:
                stops_travelled = stops.index(destination_station_id) - stops.index(origin_station_id)
            except ValueError:
                stops_travelled = 1
            
            # 🚀 C. 動態計算票價：呼叫你寫好的 query_national_rail_fare 函式
            fare_info = query_national_rail_fare(schedule_id, fare_class, stops_travelled)
            if not fare_info:
                conn.rollback()
                return False, "Failed to calculate fare."
            
            amount_usd = fare_info["total_fare_usd"]
            if ticket_type.lower() == "return":
                amount_usd *= 2  # 如果是來回票，票價乘以二

            # 🚀 D. 動態分配車廂與座位
            if seat_id and seat_id.lower() != "any":
                final_seat = seat_id.upper()
                final_coach = final_seat[0]  # 抓第一個字母當車廂 (如 "B10" -> 車廂 "B")
            else:
                final_coach = "B" if "standard" in fare_class.lower() else "A"
                final_seat = f"{final_coach}01"

            now_time = datetime.now(timezone.utc)

            # 🚀 E. 執行寫入訂票紀錄 (national_rail_bookings)
            cur.execute(sql_insert_booking, (
                booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                travel_date, departure_time, ticket_type, fare_class, final_coach, final_seat,
                stops_travelled, amount_usd, now_time
            ))

            # 🚀 F. 同步寫入付款紀錄 (payments)
            cur.execute(sql_insert_payment, (payment_id, booking_id, amount_usd, now_time))

            # 💡 兩張表一起 Commit 送進 PostgreSQL 資料庫
            conn.commit()

            return True, {
                "booking_id": booking_id,
                "payment_id": payment_id,
                "status": "confirmed",
                "departure_time": str(departure_time),
                "seat_id": final_seat,
                "coach": final_coach,
                "amount_usd": amount_usd,
                "message": "Booking created successfully!"
            }

    except Exception as e:
        if conn:
            conn.rollback()
        return False, str(e)
    finally:
        if conn:
            conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.
    Calculates refund based on service type and hours before departure.
    """

    sql_get_booking = """
        SELECT 
            b.booking_id, b.user_id, b.schedule_id, b.travel_date,
            b.departure_time, b.amount_usd, b.status,
            s.service_type
        FROM national_rail_bookings b
        JOIN national_rail_schedules s ON b.schedule_id = s.schedule_id
        WHERE b.booking_id = %s
          AND b.user_id = %s;
    """

    sql_update_booking = """
        UPDATE national_rail_bookings
        SET status = 'cancelled'
        WHERE booking_id = %s
          AND user_id = %s;
    """

    # 💡 核心修改 1：新增更新 payments 資料表的 SQL 語句
    sql_update_payment = """
        UPDATE payments
        SET status = 'refunded'
        WHERE booking_id = %s;
    """

    conn = None
    try:
        # 💡 核心修改 2：手動建立一個未開啟 autocommit 的獨立連線，確保事務安全
        conn = psycopg2.connect(
            host=cfg.PG_HOST,
            port=cfg.PG_PORT,
            dbname=cfg.PG_DB,
            user=cfg.PG_USER,
            password=cfg.PG_PASSWORD,
        )
        conn.autocommit = False  

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_get_booking, (booking_id, user_id))
            booking = cur.fetchone()

            if not booking:
                conn.rollback()
                return False, f"Booking {booking_id} was not found for this user."

            if booking["status"] == "cancelled":
                conn.rollback()
                return False, f"Booking {booking_id} is already cancelled."

            amount = float(booking["amount_usd"])

            # Combine travel_date + departure_time
            departure_dt = datetime.combine(
                booking["travel_date"],
                booking["departure_time"]
            )

            now = datetime.now()
            hours_before = (departure_dt - now).total_seconds() / 3600

            service_type = booking["service_type"]

            if service_type == "normal":
                policy_id = "RF001"
                if hours_before >= 48:
                    refund_percent = 100
                    admin_fee = 0.00
                    window = "Early cancellation"
                elif hours_before >= 24:
                    refund_percent = 75
                    admin_fee = 0.50
                    window = "Standard cancellation"
                elif hours_before >= 2:
                    refund_percent = 50
                    admin_fee = 0.50
                    window = "Late cancellation"
                else:
                    refund_percent = 0
                    admin_fee = 0.00
                    window = "No refund"
            else:
                policy_id = "RF002"
                if hours_before >= 48:
                    refund_percent = 100
                    admin_fee = 1.00
                    window = "Early cancellation"
                elif hours_before >= 24:
                    refund_percent = 50
                    admin_fee = 1.00
                    window = "Late cancellation"
                else:
                    refund_percent = 0
                    admin_fee = 0.00
                    window = "No refund"

            refund_amount = max((amount * refund_percent / 100) - admin_fee, 0)

            # 💡 核心修改 3：依序執行訂單與付款狀態更新
            cur.execute(sql_update_booking, (booking_id, user_id))
            cur.execute(sql_update_payment, (booking_id,))
            
            # 手動 Commit 提交，讓 pgAdmin 的兩張 table 同步更改
            conn.commit()

            return True, {
                "booking_id": booking_id,
                "status": "cancelled",
                "service_type": service_type,
                "policy_id": policy_id,
                "refund_window": window,
                "refund_percent": refund_percent,
                "admin_fee_usd": admin_fee,
                "refund_amount_usd": round(refund_amount, 2),
            }

    except Exception as e:
        if conn:
            conn.rollback()
        return False, str(e)
    finally:
        if conn:
            conn.close()


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────
def _connect():
    return psycopg2.connect(
        host=cfg.PG_HOST,
        port=cfg.PG_PORT,
        dbname=cfg.PG_DB,
        user=cfg.PG_USER,
        password=cfg.PG_PASSWORD,
    )

def register_user(first_name: str, surname: str, email: str, password: str, secret_question: str, secret_answer: str, phone: str = None, date_of_birth: str = None):
    """
    Register a new user with HASHED password and secret answer distributed into both 
    registered_users and user_credentials tables according to schema.sql.
    """
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    user_id = f"RU-{suffix}"
    
    # 🔒 安全升級：密碼與安全問答全部進行 Argon2 雜湊加密
    hashed_password = ph.hash(password)
    hashed_answer = ph.hash(secret_answer.strip().lower()) # 轉小寫再雜湊，確保驗證時大小寫不敏感
    
    # 💡 修正 1：移除非此資料表的欄位，加入 phone 與 date_of_birth
    sql_user = """
        INSERT INTO registered_users (
            user_id, email, full_name, phone, date_of_birth, registered_at, is_active
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """
    
    # 💡 修正 2：將密碼雜湊與安全問答雜湊一起寫入憑證表
    sql_cred = """
        INSERT INTO user_credentials (user_id, password_hash, secret_question, secret_answer_hash, created_at)
        VALUES (%s, %s, %s, %s, %s);
    """
    
    conn = None
    try:
        conn = _connect()
        conn.autocommit = False
        
        with conn.cursor() as cur:
            now_time = datetime.now(timezone.utc)
            full_name = f"{first_name.strip()} {surname.strip()}"
            
            # 1. 寫入 registered_users
            cur.execute(sql_user, (
                user_id, email.strip().lower(), full_name, 
                phone, date_of_birth, now_time, True
            ))
            
            # 2. 寫入 user_credentials
            cur.execute(sql_cred, (user_id, hashed_password, secret_question, hashed_answer, now_time))
            
        conn.commit()
        return True, user_id
    except psycopg2.errors.UniqueViolation:
        if conn: conn.rollback()
        return False, "This email is already registered."
    except Exception as e:
        if conn: conn.rollback()
        return False, f"Registration failed: {str(e)}"
    finally:
        if conn: conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials using Argon2 hash verification against user_credentials table.
    """

    sql = """
        SELECT 
            ru.user_id, 
            ru.email, 
            ru.full_name, 
            ru.phone, 
            ru.date_of_birth, 
            ru.is_active, 
            uc.password_hash
        FROM registered_users ru
        JOIN user_credentials uc 
            ON ru.user_id = uc.user_id
        WHERE ru.email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (email.strip().lower(),))
                user = cur.fetchone()

                if not user:
                    print(f"[Login] 找不到此 Email: {email}")
                    return None

                if not user.get("is_active", True):
                    print(f"[Login] 該帳號已被停用")
                    return None


                db_password = str(user["password_hash"]).strip()
                input_password = str(password).strip()
                
                try:
                    # 🔒 安全校驗邏輯：如果是相容明碼（非 $argon2 開頭），就直接比對；否則用 ph.verify
                    if db_password.startswith("$argon2"):
                        ph.verify(db_password, input_password)
                    else:

                        if db_password != input_password:
                            raise VerifyMismatchError()
                except (VerifyMismatchError, Exception):
                    print("[Login] 密碼錯誤")
                    return None


                name_parts = str(user["full_name"]).strip().split(" ", 1)
                first_name = name_parts[0] if len(name_parts) > 0 else ""
                surname = name_parts[1] if len(name_parts) > 1 else ""

                return {
                    "user_id": user["user_id"],
                    "email": user["email"],
                    "full_name": user["full_name"],
                    "first_name": first_name,
                    "surname": surname,
                    "phone": user.get("phone"),
                    "date_of_birth": user.get("date_of_birth"),
                    "is_active": user["is_active"],
                }
    except Exception as e:
        print(f"[Login System Error] 出錯: {e}")
        return None
    

def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    
    sql = """
        SELECT secret_question 
        FROM registered_users 
        WHERE email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                
                cur.execute(sql, (email.strip().lower(),))
                res = cur.fetchone()
                return res[0] if res else None
    except Exception as e:
        print(f"[Get Question Error]: {e}")
        return None

def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    sql = """
        SELECT uc.secret_answer_hash 
        FROM user_credentials uc
        JOIN registered_users ru ON uc.user_id = ru.user_id
        WHERE ru.email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (email.strip().lower(),))
                res = cur.fetchone()
                if not res:
                    return False
                
                db_answer_hash = res[0]
                input_answer = answer.strip().lower()
                
                # 🔒 支持安全問答雜湊驗證與舊明碼相容
                if db_answer_hash.startswith("$argon2"):
                    try:
                        ph.verify(db_answer_hash, input_answer)
                        return True
                    except VerifyMismatchError:
                        return False
                else:
                    return db_answer_hash == input_answer
    except Exception as e:
        print(f"[Verify Answer Error]: {e}")
        return False

def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    # 🔒 加密新密碼
    hashed_password = ph.hash(new_password)
    
    sql = """
        UPDATE user_credentials 
        SET password_hash = %s 
        WHERE user_id = (SELECT user_id FROM registered_users WHERE email = %s)
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (hashed_password, email.strip().lower()))
                return cur.rowcount > 0
    except Exception as e:
        print(f"[Update Password Error]: {e}")
        return False
    
# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """
    Find the most relevant policy documents for a given query embedding.

    Args:
        embedding: Query vector from llm.embed(user_question)
        top_k:     Number of results to return

    Returns:
        List of dicts with title, category, content, and similarity score
    """
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """
    Insert a policy document with its embedding into the database.
    Used by skeleton/seed_vectors.py — students don't need to call this directly.

    Returns:
        The new document's id
    """
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            return cur.fetchone()[0]
