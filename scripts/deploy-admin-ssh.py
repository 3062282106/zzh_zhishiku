"""Upload, deploy and verify the admin console through one interactive SSH login."""
import argparse
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
import shlex
from shutil import which
import subprocess
import sys
import tarfile


REQUIRED = {
    "access.py", "admin.py", "admin_api.py", "admin_schema.sql", "agent_import.py", "api.py", "docling_parser.py", "feishu_source.py",
    "feishu_sync.py", "file_ingest.py", "import_api.py", "mcp_server.py", "notion_source.py", "search.py", "schema.sql",
    "scripts/deploy-admin.sh", "scripts/verify-admin-cloud.py", "scripts/configure-notion.sh",
    "scripts/configure-mcp-entry.sh", "scripts/configure-admin-entry.sh", "scripts/configure-feishu-sync.sh",
}
# The console is a Vite build; its hashed asset names change on every build, so the
# archive is validated by prefix instead of by an exact file list.
CONSOLE = "web/dist/"
REMOTE = "/root/knowledge-admin-release.tar.gz"


def _allowed(name):
    """Files we own, plus anything under the console build (tar stores dirs without a slash)."""
    clean = name.rstrip("/")
    return clean in REQUIRED or clean == CONSOLE.rstrip("/") or clean.startswith(CONSOLE)


def deployment_command(bundle, notion_page=None, notion_library=None, notion_title=None, *, tunnel=False):
    with tarfile.open(bundle, "r:gz") as archive:
        entries = archive.getmembers()
        names = {entry.name.rstrip("/") for entry in entries}
        if not entries:
            raise ValueError("Deployment archive is empty.")
        if not all(entry.isfile() or (entry.isdir() and _allowed(entry.name)) for entry in entries):
            raise ValueError("Deployment archive must contain regular files (plus web/dist directories) only.")
        if REQUIRED - names:
            raise ValueError("Deployment archive is missing: " + ", ".join(sorted(REQUIRED - names)))
        extra = {entry.name for entry in entries if not _allowed(entry.name)}
        if extra:
            raise ValueError("Deployment archive contains unexpected entries: " + ", ".join(sorted(extra)))
        if CONSOLE + "index.html" not in names:
            raise ValueError("Deployment archive has no console build. Run npm --prefix web run build first.")
    checksum = sha256(bundle.read_bytes()).hexdigest()
    script = (
        "set -euo pipefail\n"
        "umask 077\n"
        f"test -s {REMOTE}\n"
        f"(cd /root && printf '%s  %s\\n' '{checksum}' '{Path(REMOTE).name}' | sha256sum -c -)\n"
        "release=$(mktemp -d /root/knowledge-admin-release.XXXXXXXX)\n"
        f"tar -xzf {REMOTE} -C \"$release\"\n"
        f"rm -f -- {REMOTE}\n"
        'bash "$release/scripts/deploy-admin.sh"\n'
        'bash "$release/scripts/configure-feishu-sync.sh"\n'
    )
    if notion_page:
        title = shlex.quote(notion_title)
        script += (
            'bash "$release/scripts/configure-notion.sh" '
            f'{shlex.quote(notion_page)} {shlex.quote(notion_library)} {title}\n'
        )
    if tunnel:
        script += "printf '%s\\n' 'Admin: http://127.0.0.1:18086/admin' 'Keep this window open.'\nexec sleep infinity\n"
    return "bash -c " + shlex.quote(script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=IPv4Address)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).with_name("knowledge-admin.tar.gz"))
    parser.add_argument("--notion-page")
    parser.add_argument("--notion-library")
    parser.add_argument("--notion-title")
    parser.add_argument("--tunnel", action="store_true", help="Keep the existing local admin and MCP ports forwarded.")
    args = parser.parse_args()
    flags = (args.notion_page, args.notion_library, args.notion_title)
    if any(flags) and not all(flags):
        parser.error("Provide all three Notion options together.")
    ssh = which("ssh")
    if ssh is None:
        sys.exit("OpenSSH client is unavailable. Use Workbench to deploy instead.")
    command = deployment_command(args.bundle, *flags, tunnel=args.tunnel)
    options = ["-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1"]
    # Upload over the ssh channel instead of scp: scp is deprecated upstream, and on
    # Windows it cannot open local paths when the working directory is not ASCII.
    with args.bundle.open("rb") as archive:
        uploaded = subprocess.call(
            [ssh, *options, f"root@{args.host}", f"cat > {REMOTE}"], stdin=archive,
        )
    if uploaded:
        return uploaded
    forwarding = ["-L", "127.0.0.1:18085:127.0.0.1:18082", "-L", "127.0.0.1:18086:127.0.0.1:18081"] if args.tunnel else []
    return subprocess.call([
        ssh, *(["-t"] if args.notion_page else []), *options,
        "-o", "ExitOnForwardFailure=yes", *forwarding, f"root@{args.host}", command,
    ])


if __name__ == "__main__":
    sys.exit(main())
