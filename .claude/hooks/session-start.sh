#!/bin/bash
# Install Python dependencies so tests and Django checks run in Claude Code cloud sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
python -m pip install --quiet --disable-pip-version-check --root-user-action=ignore -r requirements.txt
