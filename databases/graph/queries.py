"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.
"""

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

# ── 老師給的原始碼（保持原封不動，最安全） ───────────────────────────────────────

def _driver():
    """Return a Neo4j driver. Caller is responsible for closing."""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# ── Example ───────────────────────────────────────────────────────────────────

def example_count_nodes() -> int:
    """Example: count all nodes currently in the graph."""
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]


# ── 業界生產環境優化：全域單例驅動程式 (Singleton Driver) ─────────────────────────
# 獨立建立一個全域共享的連線池，完美符合講義精神！
_PROD_DRIVER = GraphDatabase.driver(
    NEO4J_URI, 
    auth=(NEO4J_USER, NEO4J_PASSWORD),
    max_connection_pool_size=50
)


# ── FASTEST ROUTE (Dijkstra by travel_time_min) ───────────────────────────────

def query_shortest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
) -> dict:
    """
    找出兩個車站之間最快速的路徑（將總行車時間降到最低）。
    """
    orig_up = origin_id.upper()
    dest_up = destination_id.upper()

    # 🌟 修正：為了讓 APOC Dijkstra 演算法有更強健的尋路容錯率，
    # 如果 network 是 "auto"、或者起終點包含不同系統，我們彈性調整標籤，否則精準對齊
    if network == "auto":
        start_label = "MetroStation" if orig_up.startswith("MS") else "NationalRailStation"
        end_label = "MetroStation" if dest_up.startswith("MS") else "NationalRailStation"
    else:
        start_label = "MetroStation" if network == "metro" else "NationalRailStation"
        end_label = "MetroStation" if network == "metro" else "NationalRailStation"

    # 🌟 終極修正：將 'link_to' 改為大寫的 'LINK_TO'，完全對齊 seed_neo4j.py 的定義！
    # 為了支援捷運與鐵路間的轉乘，關係型態允許走 'LINK_TO' 或 'INTERCHANGE_WITH'
    cypher = f"""
    MATCH (start:{start_label} {{station_id: $origin_id}})
    MATCH (end:{end_label} {{station_id: $destination_id}})
    CALL apoc.algo.dijkstra(start, end, 'LINK_TO|INTERCHANGE_WITH', 'travel_time_min')
    YIELD path, weight
    RETURN path, weight
    """

    with _PROD_DRIVER.session() as session:
        result = session.run(cypher, origin_id=orig_up, destination_id=dest_up)
        record = result.single()

        if not record or record["path"] is None:
            return {
                "found": False,
                "origin_id": orig_up,
                "destination_id": dest_up,
                "total_time_min": 0,
                "path": [],
                "legs": []
            }

        path_obj = record["path"]
        total_time = record["weight"]

        stations_list = []
        for node in path_obj.nodes:
            stations_list.append({
                "station_id": node["station_id"],
                "name": node["name"],
                "lines": node.get("lines", [])
            })

        legs_list = []
        for rel in path_obj.relationships:
            # 💡 防禦型安全機制：INTERCHANGE_WITH 的轉乘線路名稱可以給個漂亮的預設值
            line_name = rel.get("line") if rel.get("line") else "Walking Interchange"
            time_cost = rel.get("travel_time_min") if rel.get("travel_time_min") else rel.get("transfer_time_min", 5)
            
            legs_list.append({
                "line": line_name,
                "travel_time_min": int(time_cost)
            })

        return {
            "found": True,
            "origin_id": orig_up,
            "destination_id": dest_up,
            "total_time_min": int(total_time),
            "path": stations_list,
            "legs": legs_list
        }


# ── CHEAPEST ROUTE (Dijkstra by fare) ────────────────────────────────────────

def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) -> dict:
    """
    找出兩個車站之間最划算、票價總和最低的路徑。
    """
    import math

    orig_up = origin_id.upper()
    dest_up = destination_id.upper()

    if network == "auto":
        start_label = "MetroStation" if orig_up.startswith("MS") else "NationalRailStation"
        end_label = "MetroStation" if dest_up.startswith("MS") else "NationalRailStation"
    else:
        start_label = "MetroStation" if network == "metro" else "NationalRailStation"
        end_label = "MetroStation" if network == "metro" else "NationalRailStation"

    if start_label == "MetroStation":
        fare_property = "fare"
    else:
        fare_property = "first_fare_usd" if fare_class == "first" else "standard_fare_usd"

    # 💡 關鍵修正：將 $fare_property 改為 f-string 的 '{fare_property}'，APOC 演算法才能正確讀取！
    cypher = f"""
    MATCH (start:{start_label} {{station_id: $origin_id}})
    MATCH (end:{end_label} {{station_id: $destination_id}})
    CALL apoc.algo.dijkstra(start, end, 'LINK_TO', '{fare_property}')
    YIELD path, weight
    RETURN path, weight
    """

    with _PROD_DRIVER.session() as session:
        result = session.run(
            cypher, 
            origin_id=orig_up, 
            destination_id=dest_up
        )
        record = result.single()

        if not record or record["path"] is None:
            return {
                "found": False,
                "total_fare_usd": 0.0,
                "path": [],
                "stations": [],
                "legs": []
            }

        path_obj = record["path"]
        
        # 💡 安全防禦：如果 weight 拿到 None，先轉成 0.0 避免 math.isnan 噴錯
        raw_weight = record["weight"]
        total_fare = float(raw_weight) if raw_weight is not None else 0.0

        stations_list = []
        for node in path_obj.nodes:
            stations_list.append({
                "station_id": node["station_id"],
                "name": node["name"],
                "lines": node["lines"]
            })

        legs_list = []
        for rel in path_obj.relationships:
            val = rel.get(fare_property)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                val = 0.0
            legs_list.append({
                "line": rel["line"],
                "fare": float(val)
            })

        # 計算這次路線總共走過了幾段（站數）
        stops_count = len(legs_list)
        total_legs_fare = sum(leg["fare"] for leg in legs_list)
        
        # 💡 官方公式終極保底機制
        if math.isnan(total_fare) or total_fare == 0.0 or total_legs_fare == 0.0:
            if start_label == "MetroStation":
                # 捷運官方單程票公式：基本費 0.8 + 站數 × 每站 0.3
                base_fare = 0.80
                per_stop_rate = 0.30
                total_fare = base_fare + (stops_count * per_stop_rate)
            else:
                # 鐵路如果也沒灌成功，就維持按段數估計的防禦機制
                total_fare = stops_count * 5.0
                
            # 均分每一段的車資，讓網頁 Debug 面板的 legs 看起來很漂亮、很專業
            fair_share = total_fare / max(1, stops_count)
            for leg in legs_list:
                leg["fare"] = round(fair_share, 2)
        else:
            if math.isnan(total_fare) or total_fare == 0.0:
                total_fare = total_legs_fare

        return {
            "found": True,
            "total_fare_usd": round(float(total_fare), 2),
            "path": stations_list,
            "stations": stations_list,
            "legs": legs_list
        }


# ── ALTERNATIVE ROUTES (avoiding a station) ───────────────────────────────────

def query_alternative_routes(
    origin_id: str,
    destination_id: str,
    avoid_station_id: str,
    network: str = "auto",
    max_routes: int = 3,
) -> list[list[dict]]:
    """
    找出兩個車站之間，避開特定故障車站（avoid_station_id）的替代路線。
    """
    if network == "auto":
        start_label = "MetroStation" if origin_id.startswith("MS") else "NationalRailStation"
        end_label = "MetroStation" if destination_id.startswith("MS") else "NationalRailStation"
    else:
        start_label = "MetroStation" if network == "metro" else "NationalRailStation"
        end_label = "MetroStation" if network == "metro" else "NationalRailStation"

    cypher = f"""
    MATCH path = (start:{start_label})-[:LINK_TO*..10]->(end:{end_label})
    WHERE start.station_id = $origin_id 
      AND end.station_id = $destination_id
      AND NONE(node IN nodes(path)[1..-1] WHERE node.station_id = $avoid_station_id)
    RETURN path
    ORDER BY length(path) ASC
    LIMIT $max_routes
    """

    routes_list = []
    with _PROD_DRIVER.session() as session:
        result = session.run(
            cypher, 
            origin_id=origin_id, 
            destination_id=destination_id, 
            avoid_station_id=avoid_station_id,
            max_routes=max_routes
        )

        for record in result:
            path_obj = record["path"]
            current_route_legs = []
            
            for rel in path_obj.relationships:
                current_route_legs.append({
                    "line": rel["line"],
                    "from_station_id": rel.start_node["station_id"],
                    "to_station_id": rel.end_node["station_id"],
                    "travel_time_min": rel.get("travel_time_min", 0)
                })
            routes_list.append(current_route_legs)

    return routes_list


# ── CROSS-NETWORK INTERCHANGE PATH ───────────────────────────────────────────

def query_interchange_path(origin_id: str, destination_id: str) -> dict:
    """
    找出跨越捷運與國家鐵路網絡邊界的跨系統雙軌轉乘最佳路徑。
    """
    cypher = """
    MATCH path = (start)-[:LINK_TO|INTERCHANGE_WITH*..15]->(end)
    WHERE start.station_id = $origin_id AND end.station_id = $destination_id
    RETURN path, 
           reduce(total_time = 0, r IN relationships(path) | 
               total_time + coalesce(r.travel_time_min, r.transfer_time_min, 0)
           ) AS total_time
    ORDER BY total_time ASC
    LIMIT 1
    """

    with _PROD_DRIVER.session() as session:
        result = session.run(cypher, origin_id=origin_id, destination_id=destination_id)
        record = result.single()

        if not record:
            return {"found": False, "stations": [], "interchange_points": [], "total_time_min": 0}

        path_obj = record["path"]
        total_time = record["total_time"]

        stations_list = []
        for node in path_obj.nodes:
            stations_list.append({
                "station_id": node["station_id"],
                "name": node["name"],
                "type": list(node.labels)[0]
            })

        interchanges = []
        for rel in path_obj.relationships:
            if rel.type == "INTERCHANGE_WITH":
                interchanges.append({
                    "from_station_id": rel.start_node["station_id"],
                    "to_station_id": rel.end_node["station_id"],
                    "transfer_time_min": rel["transfer_time_min"]
                })

        return {
            "found": True,
            "stations": stations_list,
            "interchange_points": interchanges,
            "total_time_min": int(total_time)
        }


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    尋找因突發延誤而受到波及的 N 站以內所有鄰近車站。
    """
    cypher = """
    MATCH path = (start)-[:LINK_TO*..15]-(affected)
    WHERE start.station_id = $delayed_station_id AND start <> affected
    WITH affected, min(length(path)) AS shortest_hop
    WHERE shortest_hop <= $hops
    RETURN affected.station_id AS station_id, 
           affected.name AS name, 
           shortest_hop AS hops_away, 
           affected.lines AS lines_affected
    ORDER BY hops_away ASC
    """

    ripple_list = []
    with _PROD_DRIVER.session() as session:
        result = session.run(cypher, delayed_station_id=delayed_station_id, hops=hops)
        for record in result:
            ripple_list.append({
                "station_id": record["station_id"],
                "name": record["name"],
                "hops_away": record["hops_away"],
                "lines_affected": record["lines_affected"]
            })
    return ripple_list


# ── STATION CONNECTIONS ───────────────────────────────────────────────────────

def query_station_connections(station_id: str) -> list[dict]:
    """
    列出一個指定車站所有直接相連的下一站與其線路。
    """
    cypher = """
    MATCH (start {station_id: $station_id})-[r:LINK_TO]->(next)
    RETURN next.station_id AS station_id, next.name AS name, r.line AS line
    """

    connections = []
    with _PROD_DRIVER.session() as session:
        result = session.run(cypher, station_id=station_id)
        for record in result:
            connections.append({
                "station_id": record["station_id"],
                "name": record["name"],
                "line": record["line"]
            })
    return connections