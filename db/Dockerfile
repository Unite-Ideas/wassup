# Postgres 16 with pgvector and PostGIS.
# pgvector/pgvector is the official Postgres image (current Debian) with pgvector built in;
# PostGIS comes from the same Postgres apt repository it already has configured.
FROM pgvector/pgvector:pg16
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
COPY schema.sql /docker-entrypoint-initdb.d/10-wassup-schema.sql
