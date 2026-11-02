#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "Starting Chrome History SQLite Studio..."
python3 app.py --auto-connect --open-browser
