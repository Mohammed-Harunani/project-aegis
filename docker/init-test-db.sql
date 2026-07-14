-- Runs automatically on first Postgres container startup (mounted into
-- /docker-entrypoint-initdb.d/ -- only fires when the data volume is
-- empty, not on every restart). Creates a second, separate database
-- for integration tests so they never touch the real `aegis` database.
CREATE DATABASE aegis_test;
