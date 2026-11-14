#!/bin/sh
cd "$(dirname "$0")" || { echo 'Could not open project directory'; exit 1; }
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.10+ is required to start Forma Home.'
  echo 'Install Python 3 and run this script again.'
  exit 1
fi
exec FORMA_PORT=8767 python3 server.py
