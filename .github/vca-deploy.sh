#!/bin/bash
# VCA deploy entry point. This is the ONLY command the GitHub Actions deploy key may run (forced command
# in ~ubuntu/.ssh/authorized_keys). It reads a tar.gz of the app from stdin and deploys it to /opt/vca only;
# nothing else on this server is touched. On a failed health check it rolls back to the previous version.
set -euo pipefail
APP=/opt/vca
PREV=/opt/vca-prev
FILES="app.py index.html requirements.txt"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cd "$APP"  # never run tools from ~ubuntu (uv would try to read its config files as the vca user)

echo "==> receiving release"
head -c 20000000 | tar -xzf - -C "$STAGE" --no-same-owner $FILES
for f in $FILES; do [ -s "$STAGE/$f" ] || { echo "missing $f"; exit 1; }; done
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$STAGE/app.py"

if cmp -s "$STAGE/app.py" "$APP/app.py" && cmp -s "$STAGE/index.html" "$APP/index.html" \
   && cmp -s "$STAGE/requirements.txt" "$APP/requirements.txt"; then
  echo "==> no changes, nothing to deploy"; exit 0
fi

echo "==> backing up current version"
sudo rm -rf "$PREV" && sudo mkdir -p "$PREV"
for f in $FILES; do sudo cp "$APP/$f" "$PREV/$f"; done

REQS_CHANGED=0
cmp -s "$STAGE/requirements.txt" "$APP/requirements.txt" || REQS_CHANGED=1

install_files() {  # $1 = source dir
  for f in $FILES; do sudo install -o vca -g vca -m 644 "$1/$f" "$APP/$f"; done
  if [ "$REQS_CHANGED" = 1 ]; then
    echo "==> installing python packages"
    sudo -u vca env HOME=/var/lib/vca /usr/local/bin/uv pip install -q --no-config --python "$APP/.venv/bin/python" -r "$APP/requirements.txt"
  fi
}
healthy() {
  for i in $(seq 1 45); do curl -fsS -o /dev/null http://127.0.0.1:8010/api/personas && return 0; sleep 1; done
  return 1
}

echo "==> installing new version"
if ! install_files "$STAGE"; then
  echo "!! install failed, restoring previous version"
  REQS_CHANGED=0; install_files "$PREV"
  exit 1
fi
sudo systemctl restart vca
if healthy; then
  echo "==> deployed OK"
else
  echo "!! health check failed, rolling back"
  sudo journalctl -u vca -n 30 --no-pager || true
  install_files "$PREV"
  sudo systemctl restart vca
  healthy && echo "==> rolled back to previous version" || echo "!! rollback also unhealthy"
  exit 1
fi
