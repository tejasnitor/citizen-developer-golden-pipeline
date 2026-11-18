#!/usr/bin/env bash
# working_directory ($WORKDIR) must be a real folder of the checkout. The runner has already gone into it, following
# any symlink a PR put on its path (app -> ../elsewhere), so walk it down from $GITHUB_WORKSPACE and stop on one, then
# check the step really runs there. gp-security.yml's sast, dependencies and image steps run this first:
#   bash "$TOOLS/check_workdir.sh"
set -eo pipefail
ws="$(cd -- "$GITHUB_WORKSPACE" && pwd -P)"
wd="$ws"
IFS=/ read -ra parts <<< "$WORKDIR"
for c in "${parts[@]}"; do
  case "$c" in ''|.) continue ;; ..) echo "::error::working_directory ($WORKDIR) leaves the checkout"; exit 1 ;; esac
  wd="$wd/$c"
  if [ -L "$wd" ]; then
    echo "::error::${wd#"$ws"/} is a symlink in this checkout, so working_directory ($WORKDIR) can't be trusted; make it a folder"
    exit 1
  fi
done
if [ "$(pwd -P)" != "$wd" ]; then echo "::error::the step runs in $(pwd -P), not in $wd"; exit 1; fi
