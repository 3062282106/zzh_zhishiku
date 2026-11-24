#!/usr/bin/env bash
# Build the deployment archive for scripts/deploy-admin-ssh.py.
# Run this from anywhere; it always works on the repository root.
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ ! -s web/dist/index.html ]]; then
  echo 'web/dist is missing. Run: npm --prefix web ci && npm --prefix web run build' >&2
  exit 1
fi

# Keep this list in sync with `files` in scripts/deploy-admin.sh and REQUIRED in
# scripts/deploy-admin-ssh.py: the server copies exactly these.
files=(
  access.py admin.py admin_api.py admin_schema.sql agent_import.py api.py docling_parser.py feishu_source.py feishu_sync.py file_ingest.py
  import_api.py mcp_server.py notion_source.py search.py schema.sql
  scripts/deploy-admin.sh scripts/verify-admin-cloud.py scripts/configure-notion.sh
  scripts/configure-mcp-entry.sh scripts/configure-admin-entry.sh scripts/configure-feishu-sync.sh
)

for file in "${files[@]}"; do
  if [[ ! -s "$file" ]]; then
    echo "missing or empty: $file" >&2
    exit 1
  fi
done

out=scripts/knowledge-admin.tar.gz
rm -f -- "$out"
tar -czf "$out" "${files[@]}" web/dist
echo "wrote $out ($(du -h "$out" | cut -f1))"
echo "next: python scripts/deploy-admin-ssh.py <ECS_IP> --tunnel"
