\set ON_ERROR_STOP on

-- Run this once as the PostgreSQL cluster administrator, not as an
-- application role.  The four databases remain isolated even though they
-- share one PostgreSQL server.
--
-- Example (passwords are supplied as psql variables, never committed):
-- sudo -u postgres psql -d postgres \
--   -v litellm_password='...' -v openwebui_password='...' \
--   -f infra/postgres/bootstrap-ai-databases.sql

CREATE ROLE litellm
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
  PASSWORD :'litellm_password';

CREATE ROLE openwebui
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
  PASSWORD :'openwebui_password';

CREATE DATABASE litellm OWNER litellm;
CREATE DATABASE openwebui OWNER openwebui;

REVOKE ALL ON DATABASE litellm FROM PUBLIC;
REVOKE ALL ON DATABASE openwebui FROM PUBLIC;

-- Do not grant either role access to `construction_hazard_detection` or
-- `keycloak`.  Database ownership is the only application permission needed.
