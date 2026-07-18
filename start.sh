#!/bin/bash
set -e

python3 agent.py &
python3 diagnose.py &
exec python3 diamond_bot.py
