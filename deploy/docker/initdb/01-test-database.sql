-- Separate database for the PostgreSQL integration tests (`make test`); they drop and recreate its schema.
CREATE DATABASE branchops_test OWNER branchops;
