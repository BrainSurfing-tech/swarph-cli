#!/usr/bin/env bash
# release.sh VERSION — post-merge deploy for swarph-cli (card #1065).
#
# Opens the release PR and stops. Resumes only when a review is APPROVED on
# that PR's head. Restarts importers one unit at a time and reads each
# process's loaded version from its own image.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
LIB="$HERE/scripts/release_check.py"
REPO="${RELEASE_REPO:-BrainSurfing-tech/swarph-cli}"
REMOTE="${RELEASE_REMOTE:-https://github.com/BrainSurfing-tech/swarph-cli.git}"

DRY=0
RESUME=0
RESTART_ONLY=0
VERSION=""

usage() {
  echo "usage: release.sh [--dry-run] [--resume] [--restart-only] X.Y.Z" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --resume) RESUME=1 ;;
    --restart-only) RESTART_ONLY=1 ;;
    -h|--help) usage ;;
    *)
      if [ -n "$VERSION" ]; then usage; fi
      VERSION="$1"
      ;;
  esac
  shift
done

if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "release: version must look like X.Y.Z, got '${VERSION}'" >&2
  usage
fi

plan() {
  cat <<EOF
release ${VERSION} would:
1. scratch-clone ${REMOTE}
2. bump version to ${VERSION} in pyproject.toml, src/swarph_cli/__init__.py, plugins/swarph/.claude-plugin/plugin.json
3. commit and push branch release/${VERSION}
4. open the release PR and stop
5. resume only when a review is APPROVED at that PR head; otherwise refuse to merge
6. merge as orchestrators-hue
7. tag v${VERSION} and push the tag
8. wait for PyPI to serve ${VERSION}, retrying
9. pip install --no-cache-dir swarph-cli==${VERSION}
10. discover swarph units whose running process imports swarph_cli
11. restart those units one at a time
12. verify each process started after the install and its loaded version is ${VERSION}, read from the process image
EOF
}

if [ "$DRY" -eq 1 ]; then
  plan
  exit 0
fi

refuse_without_approval() {
  local number head reviews
  number=$(gh pr list --repo "$REPO" --head "release/${VERSION}" --state open --json number --jq '.[0].number')
  if [ -z "$number" ] || [ "$number" = "null" ]; then
    echo "refusing to merge ${VERSION}: no open release PR for release/${VERSION}" >&2
    exit 2
  fi
  head=$(gh pr view "$number" --repo "$REPO" --json headRefOid --jq .headRefOid)
  reviews=$(gh api "repos/${REPO}/pulls/${number}/reviews")
  if ! printf '%s' "$reviews" | python3 "$LIB" approval-at-head --head "$head"; then
    echo "refusing to merge ${VERSION}: no APPROVED review at head ${head}" >&2
    exit 2
  fi
  echo "$number" "$head"
}

merge_as_orchestrators_hue() {
  local number="$1"
  local token
  token=$(gh auth token --user orchestrators-hue)
  GH_TOKEN="$token" gh pr merge "$number" --repo "$REPO" --merge
}

wait_for_pypi() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    if curl -fsS "https://pypi.org/pypi/swarph-cli/json" | python3 -c 'import json,sys; raise SystemExit(0 if "'"$VERSION"'" in json.load(sys.stdin).get("releases", {}) else 1)'; then
      echo "PyPI is serving ${VERSION}"
      return 0
    fi
    echo "PyPI does not have ${VERSION} yet (try ${i}); retrying" >&2
    sleep "${RELEASE_PYPI_SLEEP:-20}"
  done
  echo "release: PyPI never served ${VERSION}" >&2
  exit 1
}

install_release() {
  pip install --no-cache-dir "swarph-cli==${VERSION}"
}

restart_importers() {
  local install_epoch="$1"
  local unit pid
  local units=()
  # bash 3.2 (macOS) has no mapfile. One name per line, one restart per name.
  if [ -n "${RELEASE_UNITS_FILE:-}" ]; then
    while IFS= read -r unit || [ -n "$unit" ]; do
      [ -n "$unit" ] || continue
      units+=("$unit")
    done < "$RELEASE_UNITS_FILE"
  else
    while IFS= read -r unit || [ -n "$unit" ]; do
      [ -n "$unit" ] || continue
      units+=("$unit")
    done < <(python3 "$LIB" discover)
  fi
  if [ "${#units[@]}" -eq 0 ]; then
    echo "release: no running swarph unit imports swarph_cli" >&2
    exit 1
  fi
  for unit in "${units[@]}"; do
    [ -n "$unit" ] || continue
    echo "restarting one unit: ${unit}"
    sudo -n systemctl restart "$unit"
    pid=$(systemctl show -p MainPID --value "$unit")
    if [ -n "${RELEASE_VERIFY_CMD:-}" ]; then
      # shellcheck disable=SC2086
      $RELEASE_VERIFY_CMD "$unit"
    else
      sudo -n python3 "$LIB" verify --pid "$pid" --expected "$VERSION" --install-epoch "$install_epoch"
    fi
  done
}

open_release_pr() {
  local scratch
  scratch=$(mktemp -d)
  git clone --depth 1 "$REMOTE" "$scratch"
  python3 "$LIB" bump --kind pyproject --version "$VERSION" "$scratch/pyproject.toml"
  python3 "$LIB" bump --kind init --version "$VERSION" "$scratch/src/swarph_cli/__init__.py"
  python3 "$LIB" bump --kind plugin --version "$VERSION" "$scratch/plugins/swarph/.claude-plugin/plugin.json"
  git -C "$scratch" -c user.email="${RELEASE_GIT_EMAIL:-release.sh@local}" -c user.name="${RELEASE_GIT_NAME:-release.sh}" checkout -b "release/${VERSION}"
  git -C "$scratch" add pyproject.toml src/swarph_cli/__init__.py plugins/swarph/.claude-plugin/plugin.json
  git -C "$scratch" -c user.email="${RELEASE_GIT_EMAIL:-release.sh@local}" -c user.name="${RELEASE_GIT_NAME:-release.sh}" commit -m "release: swarph-cli ${VERSION}"
  git -C "$scratch" push -u origin "release/${VERSION}"
  gh pr create --repo "$REPO" --base main --head "release/${VERSION}" \
    --title "release: swarph-cli ${VERSION}" \
    --body "Release ${VERSION}. Opened by release.sh. Merge stays with release.sh --resume after a review is APPROVED at this head."
  echo "stopped after opening the release PR for ${VERSION}. Resume with: release.sh --resume ${VERSION}"
}

if [ "$RESTART_ONLY" -eq 1 ]; then
  install_epoch="${RELEASE_INSTALL_EPOCH:-$(date +%s)}"
  restart_importers "$install_epoch"
  exit 0
fi

if [ "$RESUME" -eq 1 ]; then
  meta=$(refuse_without_approval)
  read -r number head <<< "$meta"
  echo "approval present at head ${head}; merging as orchestrators-hue"
  merge_as_orchestrators_hue "$number"
  git tag "v${VERSION}" "$head"
  git push origin "v${VERSION}"
  wait_for_pypi
  install_release
  install_epoch=$(date +%s)
  restart_importers "$install_epoch"
  exit 0
fi

open_release_pr
