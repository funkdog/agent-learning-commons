#!/bin/sh
# Run this yourself after cloning a trusted copy. It does not install a service.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.9 or newer is required.' >&2
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo 'Python 3.9 or newer is required.' >&2
  exit 1
}
if ! command -v gh >/dev/null 2>&1; then
  echo 'Install the official GitHub CLI, then authenticate with gh auth login.' >&2
  exit 1
fi
exec python3 "$script_dir/community_client.py" init "$@"
