#!/bin/bash
set -e

python3 agent.py &
python3 diagnose.py &
python3 supervisor_agent.py &
exec python3 diamond_bot.py
