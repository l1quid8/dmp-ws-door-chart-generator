#!/bin/bash
PROJ="/Users/tylercaldwell/Documents/Claude/Projects/C1 Intrusion Door Chart Script Build"
cd "$PROJ"
source venv/bin/activate
exec python scripts/app.py
