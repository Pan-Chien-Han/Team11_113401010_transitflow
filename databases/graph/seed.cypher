// Deprecated: seeding is now done via skeleton/seed_neo4j.py
// which loads data directly from train-mock-data/ JSON files.
//
// If you prefer Cypher-file seeding, implement your graph schema here.
// Run with: python skeleton/seed_neo4j.py (or via the Neo4j Browser)


// 1. Ensure MetroStation IDs are unique, and create a constraint/index to optimize MATCH query performance.
CREATE CONSTRAINT FOR (s:MetroStation) REQUIRE s.station_id IS UNIQUE;

// 2. Ensure NationalRailStation IDs are unique (preparing the database layer for upcoming national_rail data imports).
CREATE CONSTRAINT FOR (s:NationalRailStation) REQUIRE s.station_id IS UNIQUE;