#!/usr/bin/env bash
set -euo pipefail
cd /opt/render/project/src
D="/var/data/diamond_end_review_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$D"
cp diamond_prospective_decision_rules.json "$D/"
cp diamond_prospective_final_analyzer.py "$D/"
python3 diamond_prospective_final_analyzer.py > "$D/final_analysis.txt"
python3 diamond_decision_gate_v1_4.py > "$D/decision_gate.txt"
sha256sum "$D"/* > "$D/SHA256SUMS.txt"
echo "ENDREVIEW: $D"
