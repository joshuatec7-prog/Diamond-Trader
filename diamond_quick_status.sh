#!/usr/bin/env bash

python3 diamond_master_status.py | grep -E \
'Status  :|Samples :|Current :|oom_kill|SELECTIVE  accepted|STRONG     accepted|CURRENT    accepted|WAIT_15M|WAIT15_050|OFF_HOURS|DAYTIME|SECOND_CHANCE  :|BREAKOUT_ONLY|STRONG_QUALITY|Volgende stap|Longtest|Paper-shorttest'
