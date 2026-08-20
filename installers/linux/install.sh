#!/usr/bin/env bash
# Лончер установщика для Linux (N5-P1): логики установки здесь нет — она в common/install-posix.sh.
set -euo pipefail
CDPATH=''
self_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
if [ -f "$self_dir/common/install-posix.sh" ]; then
  pkg_root=$self_dir
elif [ -f "$self_dir/../common/install-posix.sh" ]; then
  pkg_root=$(cd -- "$self_dir/.." && pwd -P)
else
  printf 'Не найден common/install-posix.sh рядом с %s\n' "$0" >&2
  exit 2
fi
OPENCODE_INSTALLER_PLATFORM=linux
OPENCODE_INSTALLER_PKG_ROOT=$pkg_root
export OPENCODE_INSTALLER_PLATFORM OPENCODE_INSTALLER_PKG_ROOT
exec bash "$pkg_root/common/install-posix.sh" "$@"
