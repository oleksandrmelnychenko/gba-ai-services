#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
files=(app/risk/artifacts/cv_report.json app/risk/artifacts/forward_model_status.json app/risk/artifacts/monitor_baseline.json app/risk/artifacts/scorecard_coefficients.json)
if git diff --quiet -- "${files[@]}"; then
  exit 0
fi
git add -- "${files[@]}"
git -c user.name="GBA solvency retrain" -c user.email="solvency-retrain@gba.local" commit -q -m "chore: publish weekly solvency artifacts"
