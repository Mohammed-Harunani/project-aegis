-- Runs automatically on first Postgres container startup (mounted into
-- /docker-entrypoint-initdb.d/ -- only fires when the data volume is
-- empty, not on every restart). Creates:
--  - aegis_test: for governance-layer integration tests
--  - aegis_live_test: a separate live-TARGET test database for Phase
--    2.5's live-execution tests. Must be distinct from aegis_test --
--    the live writer explicitly refuses aegis and aegis_test as
--    targets, so testing live writes needs a third database.
CREATE DATABASE aegis_test;
CREATE DATABASE aegis_live_test;
