"""Postgres connection for the lab: one env var, DATABASE_URL."""

import os
import sys

import psycopg


def url() -> str:
    found = os.environ.get("DATABASE_URL")
    if not found:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    return found


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url(), autocommit=autocommit)
