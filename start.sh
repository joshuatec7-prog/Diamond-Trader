#!/bin/bash
set -e

python3 agent.py &
exec python3 diamond_bot.py
