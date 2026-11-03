#!/usr/bin/env bash
# membrane_validate.sh -- card #1427. Validates the dispatch inputs of
# .github/workflows/membrane.yml BEFORE anything is checked out or run.
#
# Inputs arrive as environment variables set by the workflow's `env:` block
# (never interpolated into this script): IN_OS IN_REPO IN_REF IN_ARTIFACTS
# IN_NAME IN_TIMEOUT. A dispatch input is untrusted text, so every field is
# checked against a narrow pattern, and a rejection prints a STATIC message that
# never echoes the input (an echoed line starting with `::` would be a workflow
# command). Exit 0 and ok=true, or exit 1 and ok=false. When GITHUB_OUTPUT is
# set it also receives ok, verdict and runner.
#
# Bash 3.2 safe (macOS /bin/bash): no associative arrays, no ${var,,}.
set -u

out() {
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        printf '%s\n' "$1" >> "$GITHUB_OUTPUT"
    fi
    return 0
}

reject() {
    printf 'REJECTED (%s): %s\n' "$1" "$2"
    out "ok=false"
    out "verdict=rejected"
    exit 1
}

IN_OS="${IN_OS-}"
IN_REPO="${IN_REPO-}"
IN_REF="${IN_REF-}"
IN_ARTIFACTS="${IN_ARTIFACTS-}"
IN_NAME="${IN_NAME-}"
IN_TIMEOUT="${IN_TIMEOUT-}"

case "$IN_OS" in
    macos) runner="macos-latest" ;;
    windows) runner="windows-latest" ;;
    ubuntu) runner="ubuntu-latest" ;;
    *) reject "os" "os must be exactly macos, windows or ubuntu" ;;
esac

# owner/name, GitHub's own character set. No spaces, quotes, `;`, `$`, backticks.
re_repo='^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$'
[[ "$IN_REPO" =~ $re_repo ]] || reject "repo" "repo must be owner/name with letters, digits, dot, dash, underscore"

# A branch, tag or sha. No leading dash (an option), no `..`, no spaces or shell characters.
re_ref='^[A-Za-z0-9_./-]{1,200}$'
[[ "$IN_REF" =~ $re_ref ]] || reject "ref" "ref must be a plain branch, tag or commit name"
case "$IN_REF" in
    -* | *..* | */ | *//* | *.lock) reject "ref" "ref has a leading dash, a double dot, an empty path part or a .lock suffix" ;;
esac

re_name='^[A-Za-z0-9_.-]{1,60}$'
[[ "$IN_NAME" =~ $re_name ]] || reject "artifact_name" "artifact_name must be 1-60 letters, digits, dot, dash or underscore"

re_num='^[0-9]{1,3}$'
[[ "$IN_TIMEOUT" =~ $re_num ]] || reject "timeout" "timeout_minutes must be a whole number"
if [ "$((10#$IN_TIMEOUT))" -lt 1 ] || [ "$((10#$IN_TIMEOUT))" -gt 60 ]; then
    reject "timeout" "timeout_minutes must be between 1 and 60"
fi

# Artifact paths: one per line, relative to the checkout, plain characters only.
re_path='^[A-Za-z0-9_./@+-]{1,200}$'
count=0
while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [ -z "$line" ] && continue
    count=$((count + 1))
    [ "$count" -le 20 ] || reject "artifacts" "at most 20 artifact paths"
    [[ "$line" =~ $re_path ]] || reject "artifacts" "an artifact path has a character outside letters, digits, dot, slash, at, plus, dash, underscore"
    case "$line" in
        /* | *..* | -*) reject "artifacts" "an artifact path is absolute, climbs with a double dot or starts with a dash" ;;
    esac
done <<EOF
$IN_ARTIFACTS
EOF

printf 'ACCEPTED: os=%s runner=%s repo and ref have the plain shape, %s artifact path(s), timeout %s min\n' "$IN_OS" "$runner" "$count" "$((10#$IN_TIMEOUT))"
out "ok=true"
out "verdict=accepted"
out "runner=$runner"
exit 0
