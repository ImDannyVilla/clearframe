#!/bin/sh
# Install ClearFrame's git hooks: checks before every commit, `make verify` before every push.
# Run once per clone, on laptops and on the GB10. Plain git hooks, so nothing to pip install.
set -eu

HOOKS="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS"

cat > "$HOOKS/pre-commit" <<'EOF'
#!/bin/sh
set -e
git diff --cached --check
python3 scripts/scan_secrets.py --staged
make -s check
EOF

cat > "$HOOKS/pre-push" <<'EOF'
#!/bin/sh
set -e
make -s verify
EOF

chmod +x "$HOOKS/pre-commit" "$HOOKS/pre-push"
echo "Installed pre-commit (secret scan and make check) and pre-push (make verify) in $HOOKS"
