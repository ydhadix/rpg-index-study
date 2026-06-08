"""Shared database helpers."""
from __future__ import annotations
import os
import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://rpg:rpg@localhost:5544/rpg")


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=autocommit)
