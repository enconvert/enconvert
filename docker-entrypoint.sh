#!/usr/bin/env bash
set -e

# Start unoserver in the background for office-document -> PDF conversions.
# If it fails to start, office conversion is simply unavailable; everything
# else still works.
if command -v unoserver >/dev/null 2>&1; then
  unoserver >/tmp/unoserver.log 2>&1 &
fi

exec python main.py
