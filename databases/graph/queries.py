"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.
"""

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


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


# ── PRODUCTION ENVIRONMENT OPTIMIZATION: GLOBAL SINGLETON DRIVER ──────────────
# Initialize a globally shared connection pool
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
    Find the fastest route between two stations (minimizing total travel time).
    """
    orig_up = origin_id.upper()
    dest_up = destination_id.upper()

    # To make the APOC Dijkstra algorithm more robust and fault-tolerant,
    # if network is set to "auto" or the origin/destination belong to different systems, 
    # we dynamically adjust the node labels; otherwise, we enforce precise alignment.
    if network == "auto":
        start_label = "MetroStation" if orig_up.startswith("MS") else "NationalRailStation"
        end_label = "MetroStation" if dest_up.startswith("MS") else "NationalRailStation"
    else:
        start_label = "MetroStation" if network == "metro" else "NationalRailStation"
        end_label = "MetroStation" if network == "metro" else "NationalRailStation"

    # Capitalize 'link_to' to 'LINK_TO' to fully align with the definitions in seed_neo4j.py!
    # To support inter-network transfers between Metro and National Rail, the relationship types allow either 'LINK_TO' or 'INTERCHANGE_WITH'.
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
            # Defensive Safeguard: Assign a clean default name for INTERCHANGE_WITH walking transfers
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
    Find the most cost-effective route between two stations using Neo4j relationship cost weights.
    This function relies on fare/cost properties stored on graph relationships.
    It does NOT fall back to hard-coded fare formulas.
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

    # Use graph relationship cost properties.
    # Metro and standard rail use standard_fare_usd.
    # First class rail uses first_fare_usd.
    if fare_class.lower() == "first":
        fare_property = "first_fare_usd"
    else:
        fare_property = "standard_fare_usd"

    # Include INTERCHANGE_WITH for cross-network paths.
    # This requires INTERCHANGE_WITH relationships to also have the selected fare_property.
    relationship_filter = "LINK_TO>|INTERCHANGE_WITH>"

    cypher = f"""
    MATCH (start:{start_label} {{station_id: $origin_id}})
    MATCH (end:{end_label} {{station_id: $destination_id}})
    CALL apoc.algo.dijkstra(start, end, $relationship_filter, $fare_property)
    YIELD path, weight
    RETURN path, weight
    """

    with _PROD_DRIVER.session() as session:
        result = session.run(
            cypher,
            origin_id=orig_up,
            destination_id=dest_up,
            relationship_filter=relationship_filter,
            fare_property=fare_property,
        )
        record = result.single()

        if not record or record["path"] is None:
            return {
                "found": False,
                "error": "No cheapest route found in Neo4j graph.",
                "total_fare_usd": None,
                "path": [],
                "stations": [],
                "legs": []
            }

        path_obj = record["path"]
        raw_weight = record["weight"]

        if raw_weight is None:
            return {
                "found": False,
                "error": f"No cost data found on graph relationships for property '{fare_property}'.",
                "total_fare_usd": None,
                "path": [],
                "stations": [],
                "legs": []
            }

        total_fare = float(raw_weight)

        if math.isnan(total_fare):
            return {
                "found": False,
                "error": f"Invalid cost data found on graph relationships for property '{fare_property}'.",
                "total_fare_usd": None,
                "path": [],
                "stations": [],
                "legs": []
            }

        stations_list = []
        for node in path_obj.nodes:
            stations_list.append({
                "station_id": node.get("station_id"),
                "name": node.get("name"),
                "lines": node.get("lines", []),
            })

        legs_list = []
        missing_cost_edges = []

        for rel in path_obj.relationships:
            rel_type = rel.type
            line = rel.get("line", "interchange")

            fare_value = rel.get(fare_property)

            if fare_value is None:
                missing_cost_edges.append({
                    "relationship_type": rel_type,
                    "line": line,
                    "missing_property": fare_property,
                })
                fare_value = 0.0
            else:
                fare_value = float(fare_value)

            legs_list.append({
                "relationship_type": rel_type,
                "line": line,
                "fare_property": fare_property,
                "fare": fare_value,
            })

        if missing_cost_edges:
            return {
                "found": False,
                "error": f"Some graph relationships are missing cost property '{fare_property}'. Re-run seed_neo4j.py after adding fare properties.",
                "missing_cost_edges": missing_cost_edges,
                "total_fare_usd": None,
                "path": stations_list,
                "stations": stations_list,
                "legs": legs_list,
            }

        return {
            "found": True,
            "total_fare_usd": round(total_fare, 2),
            "fare_class": fare_class.lower(),
            "fare_property_used": fare_property,
            "path": stations_list,
            "stations": stations_list,
            "legs": legs_list,
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
    Find alternative routes between two stations, explicitly bypassing a malfunctioning or blocked station (avoid_station_id).
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
    Find the optimal dual-system path that crosses the boundaries between Metro and National Rail networks.
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

        # ==========================================================
        # CORE OPTIMIZATION FOR Llama 3.2:1b: FLAT PROMPT STRINGS
        # ==========================================================
        # 1. Concatenate a 100% complete station itinerary to prevent small LLMs from skipping stops.
        route_segments = []
        for i, st in enumerate(stations_list):
            route_segments.append(f"{i+1}. {st['name']} ({st['station_id']})")
        full_route_string = " -> ".join(route_segments)

        # 2. Build unambiguous, explicit transfer string hints
        interchange_hints = []
        for ic in interchanges:
            interchange_hints.append(
                f"Transfer at {ic['from_station_id']} to {ic['to_station_id']} (takes {ic['transfer_time_min']} minutes)."
            )
        interchange_string = " | ".join(interchange_hints) if interchanges else "No transfer needed."

        # Pack into a "foolproof" schema to return to agent.py
        return {
            "found": True,
            "total_time_min": int(total_time),
            "complete_itinerary_path_do_not_skip": full_route_string, # Forcefully inject complete route string
            "transfer_instructions": interchange_string,              # Forcefully inject explicit transfer rules
            "stations": stations_list,             # Retain original nested structure for system compatibility
            "interchange_points": interchanges     # Retain original nested structure for system compatibility
        }


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    Identify all neighboring stations within N hops that are affected by a sudden delay ripple effect.
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
    List all directly connected downstream stations and their corresponding lines for a given station.
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