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
        WHERE %s = ANY(stops_in_order)
          AND %s = ANY(stops_in_order)
          AND array_position(stops_in_order, %s) < array_position(stops_in_order, %s);
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
                cur.execute(sql_schedules, (origin_id, destination_id, origin_id, destination_id))
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

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    # 建立 SQL 查詢語法，從資料庫撈出該使用者的基本資料
    sql = """
        SELECT user_id, email, full_name, phone, date_of_birth, is_active
        FROM registered_users
        WHERE email = %s;
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (user_email.strip().lower(),))
                user = cur.fetchone()
                
                if user:
                    return dict(user)
                return None
    except Exception as e:
        print(f"[Query User Profile Error] 出錯: {e}")
        return None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).
    """
    # 1. 查詢該使用者的 National Rail 火車訂票紀錄
    sql_rail = """
        SELECT 
            b.booking_id, b.schedule_id, b.origin_station_id, b.destination_station_id,
            b.travel_date, b.departure_time, b.ticket_type, b.fare_class, 
            b.coach, b.seat_id, b.amount_usd, b.status
        FROM national_rail_bookings b
        JOIN registered_users u ON b.user_id = u.user_id
        WHERE u.email = %s
        ORDER BY b.travel_date DESC, b.departure_time DESC;
    """

    # 2. 查詢該使用者的 Metro 捷運搭乘/購票紀錄
    sql_metro = """
        SELECT 
            m.trip_id, m.schedule_id, m.origin_station_id, m.destination_station_id,
            m.travel_date, m.ticket_type, m.amount_usd, m.status
        FROM metro_travel_history m
        JOIN registered_users u ON m.user_id = u.user_id
        WHERE u.email = %s
        ORDER BY m.travel_date DESC;
    """

    results = {
        "national_rail": [],
        "metro": []
    }

    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                email_clean = user_email.strip().lower()
                
                # 撈取火車訂票
                cur.execute(sql_rail, (email_clean,))
                rail_rows = cur.fetchall()
                for row in rail_rows:
                    r_dict = dict(row)
                    if r_dict.get("travel_date"):
                        r_dict["travel_date"] = r_dict["travel_date"].strftime("%Y-%m-%d")
                    if r_dict.get("departure_time"):
                        r_dict["departure_time"] = r_dict["departure_time"].strftime("%H:%M")
                    # 將 NUMERIC 型態轉為 float，避免 JSON 解析崩潰
                    r_dict["amount_usd"] = float(r_dict["amount_usd"]) if r_dict.get("amount_usd") else 0.0
                    results["national_rail"].append(r_dict)

                # 撈取捷運歷史
                cur.execute(sql_metro, (email_clean,))
                metro_rows = cur.fetchall()
                for row in metro_rows:
                    m_dict = dict(row)
                    if m_dict.get("travel_date"):
                        m_dict["travel_date"] = m_dict["travel_date"].strftime("%Y-%m-%d")
                    m_dict["amount_usd"] = float(m_dict["amount_usd"]) if m_dict.get("amount_usd") else 0.0
                    results["metro"].append(m_dict)

                return results
    except Exception as e:
        print(f"[Query User Bookings Error] 查詢用戶訂票史失敗: {e}")
        return {"national_rail": [], "metro": []}


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    raise NotImplementedError("TODO: implement after designing your schema")


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
    raise NotImplementedError("TODO: implement after designing your schema")


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.

    Calculates the refund amount according to the booking's service type:
      - Normal service: RF001 windows (100% / 75% / 50% / 0%)
      - Express service: RF002 windows (100% / 50% / 0%)

    Args:
        booking_id: e.g. "BK001"
        user_id:    must match the booking's user_id

    Returns:
        (True, result_dict)  with refund_amount_usd and policy note
        (False, error_msg)
    """
    raise NotImplementedError("TODO: implement after designing your schema")


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
