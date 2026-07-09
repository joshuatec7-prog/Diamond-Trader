#!/bin/bash
# Start grid bot, agent en short-test tegelijk
python3 agent.py &
python3 short_test.py &
python3 grid_bot.py
