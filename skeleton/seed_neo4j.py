"""
TransitFlow — Neo4j Seeder
Run once after starting Docker:
    python skeleton/seed_neo4j.py

Loads station and network data from train-mock-data/:
  - metro_stations.json         — city metro stations and adjacencies
  - national_rail_stations.json — national rail stations and adjacencies

Design your graph schema (node labels, relationship types, properties)
based on the data in these files, then implement the seed() function below.
"""

import json
import os
import sys

sys.path.insert(0, ".")

from neo4j import GraphDatabase
from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def seed():
    metro_stations = _load("metro_stations.json")
    rail_stations  = _load("national_rail_stations.json")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:

        session.run("MATCH (n) DETACH DELETE n")
        print("  Cleared existing graph data")

        # ==========================================================
        # 1. Create MetroStation Nodes (MetroStation)
        # ==========================================================
        print("  Creating MetroStation nodes...")
        for station in metro_stations:
            session.run(
                """
                MERGE (s:MetroStation {station_id: $station_id})
                SET s.name = $name,
                    s.lines = $lines,
                    s.is_interchange_metro = $is_interchange_metro,
                    s.interchange_metro_lines = $interchange_metro_lines,
                    s.is_interchange_national_rail = $is_interchange_national_rail,
                    s.interchange_national_rail_station_id = $interchange_national_rail_station_id
                """,
                station_id=station["station_id"],
                name=station["name"],
                lines=station["lines"],
                is_interchange_metro=station["is_interchange_metro"],
                interchange_metro_lines=station["interchange_metro_lines"],
                is_interchange_national_rail=station["is_interchange_national_rail"],
                interchange_national_rail_station_id=station["interchange_national_rail_station_id"]
            )

        # ==========================================================
        # 2. Create NationalRailStation Nodes (NationalRailStation)
        # ==========================================================
        print("  Creating NationalRailStation nodes...")
        for station in rail_stations:
            session.run(
                """
                MERGE (s:NationalRailStation {station_id: $station_id})
                SET s.name = $name,
                    s.lines = $lines
                """,
                station_id=station["station_id"],
                name=station["name"],
                lines=station.get("lines", [])
            )

        # ==========================================================
        # 3. Create Metro Network Relationships (LINK_TO)
        # ==========================================================
        print("  Creating metro links...")
        for station in metro_stations:
            from_id = station["station_id"]
            for adjacent in station.get("adjacent_stations", []):
                to_id = adjacent["station_id"]
                line_name = adjacent["line"]
                travel_time = adjacent["travel_time_min"]
                
                session.run(
                    """
                    MATCH (from:MetroStation {station_id: $from_id})
                    MATCH (to:MetroStation {station_id: $to_id})
                    MERGE (from)-[r:LINK_TO {line: $line_name}]->(to)
                    SET r.travel_time_min = $travel_time
                    """,
                    from_id=from_id,
                    to_id=to_id,
                    line_name=line_name,
                    travel_time=travel_time
                )

        # ==========================================================
        # 4. Create National Rail Network Relationships (LINK_TO)
        # ==========================================================
        print("  Creating national rail links...")
        for station in rail_stations:
            from_id = station["station_id"]
            for adjacent in station.get("adjacent_stations", []):
                to_id = adjacent["station_id"]
                line_name = adjacent["line"]
                travel_time = adjacent["travel_time_min"]
                
                session.run(
                    """
                    MATCH (from:NationalRailStation {station_id: $from_id})
                    MATCH (to:NationalRailStation {station_id: $to_id})
                    MERGE (from)-[r:LINK_TO {line: $line_name}]->(to)
                    SET r.travel_time_min = $travel_time
                    """,
                    from_id=from_id,
                    to_id=to_id,
                    line_name=line_name,
                    travel_time=travel_time
                )

        # ==========================================================
        # 5. Create Cross-Network Interchange Relationships (INTERCHANGE_WITH)
        # ==========================================================
        print("  Creating interchange relationships between metro and national rail...")
        for station in metro_stations:
            if station.get("is_interchange_national_rail") and station.get("interchange_national_rail_station_id"):
                metro_id = station["station_id"]
                rail_id = station["interchange_national_rail_station_id"]
                
                # Establish a bidirectional interchange relationship.
                # Transfer time defaults to 5 minutes but can be adjusted if needed.
                session.run(
                    """
                    MATCH (m:MetroStation {station_id: $metro_id})
                    MATCH (r:NationalRailStation {station_id: $rail_id})
                    MERGE (m)-[i1:INTERCHANGE_WITH]->(r)
                    SET i1.transfer_time_min = 5
                    MERGE (r)-[i2:INTERCHANGE_WITH]->(m)
                    SET i2.transfer_time_min = 5
                    """,
                    metro_id=metro_id,
                    rail_id=rail_id
                )

    driver.close()
    print("\nNeo4j graph seeded successfully.")
    print("   Open http://localhost:7475 to explore the graph.")


if __name__ == "__main__":
    print("Connecting to Neo4j...")
    seed()