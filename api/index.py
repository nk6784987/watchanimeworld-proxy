"""
api/index.py — Vercel Python Serverless Function entrypoint.

Vercel's Python runtime auto-detects a WSGI-compatible `app` object in this
file and wires it up to handle every request routed here by vercel.json.
We simply import the Flask app instance defined in app.py at the project
root and re-export it.
"""

import sys
import os

# Ensure the project root (one level up from /api) is importable so that
# `from app import app` resolves correctly in the Vercel build environment.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (Flask application instance)

# Vercel looks for a module-level `app` (WSGI callable) — nothing further
# needed here.
