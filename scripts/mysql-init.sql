-- Run by the MySQL image on a new data directory (compose.yaml mounts it into
-- /docker-entrypoint-initdb.d) and by CI against its service container.
--
-- sqla_database = run and worker create databases named after the configured
-- one: sqla_<run> and sqla_<run>_gw0. The image grants MYSQL_USER its own
-- database only, so CREATE DATABASE for any other name fails with 1044.
-- MySQL 8.4 still applies the wildcard in a database-level grant, and
-- deprecates it.
GRANT ALL ON `sqla\_%`.* TO 'sqla'@'%';

-- A user with what the image gives its user, all privileges on sqla only, for
-- the message about the CREATE privilege it lacks (tests/integration).
CREATE USER IF NOT EXISTS 'sqla_nocreate'@'%' IDENTIFIED BY 'sqla';
GRANT ALL ON `sqla`.* TO 'sqla_nocreate'@'%';
