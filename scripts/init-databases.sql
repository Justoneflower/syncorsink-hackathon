-- Each service gets its own logical database on the same Postgres instance.
-- This mirrors reality: each Karnataka department runs its own DB.
CREATE DATABASE sws;
CREATE DATABASE factories;
CREATE DATABASE shops;
CREATE DATABASE middleware;
