#!/usr/bin/env bash
set -e
cd /opt/render/project/src
python3 -m py_compile diamond_prospective_final_analyzer.py
bash -n diamond_capture_end_review.sh
python3 diamond_truth_audit.py | grep -q 'AUDIT: OK'
echo "PREFLIGHT_INFRA_READY"
echo "LIVE: NEE"
