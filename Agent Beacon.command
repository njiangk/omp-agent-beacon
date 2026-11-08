#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 beacon.py "$@"
