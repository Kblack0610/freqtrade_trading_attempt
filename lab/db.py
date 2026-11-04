"""Postgres connection for the lab: one env var, DATABASE_URL."""

import os
import sys

import psycopg


def connect(autocommit: bool = False) -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    return psycopg.connect(url, autocommit=autocommit)
