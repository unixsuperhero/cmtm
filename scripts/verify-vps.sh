#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LIMIT=${CMTM_VERIFY_TIMEOUT:-120}
[[ "$LIMIT" =~ ^[1-9][0-9]*$ ]] && (( LIMIT <= 900 )) || { echo 'CMTM_VERIFY_TIMEOUT must be 1..900 seconds' >&2; exit 2; }
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=2 vps2)
RUN=$("${SSH[@]}" 'mktemp -d /tmp/cmtm-verify.XXXXXXXX')
[[ "$RUN" == /tmp/cmtm-verify.* && "$RUN" != *[!a-zA-Z0-9/._-]* ]] || exit 1
printf 'Verification snapshot: vps2:%s\n' "$RUN"
rsync -rtp --safe-links --timeout=30 \
  -e 'ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=2' \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.db*' --exclude='*.sqlite*' --exclude='.env*' \
  "$ROOT/" "vps2:$RUN/"
printf -v REMOTE 'bash -s -- %q %q' "$RUN" "$LIMIT"
"${SSH[@]}" "$REMOTE" <<'REMOTE_SCRIPT'
set -euo pipefail
RUN=$1
LIMIT=$2
cd "$RUN"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export XDG_CONFIG_HOME="$RUN/config"
mkdir -p "$XDG_CONFIG_HOME"
printf -v CMTM_TEST_SERVER 'cmtm-t-%06x' "$(((RANDOM << 9 | RANDOM) & 16777215))"
export CMTM_TEST_SERVER
trap 'timeout --kill-after=1s 5s tuios --server "$CMTM_TEST_SERVER" kill-server >/dev/null 2>&1 || true' EXIT
{
  printf 'Running isolated acceptance in %s (limit %ss)\n' "$RUN" "$LIMIT"
  timeout --kill-after=5s "$LIMIT" python3 scripts/smoke.py
} 2>&1 | tee verification.log
REMOTE_SCRIPT
