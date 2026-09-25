#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: $0 [--ssh HOST]" >&2; exit 2; }
ssh_host=""
case "${1:-}" in
    "") ;;
    --ssh) [[ $# -eq 2 ]] || usage; ssh_host=$2 ;;
    *) usage ;;
esac

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_dir=$(mktemp -d "${TMPDIR:-/tmp}/actl-e2e-config.XXXXXX")
start_ns=$(date +%s%N)
session="actl-e2e-$(basename "$config_dir" | tr '.' '_')"
config="$config_dir/config.json"
run_home="$config_dir/home"
mkdir -p "$run_home/.commandcode/e2e"
created=0
steps=()
remote_config_dir="/tmp/actl-e2e-config-$session"
remote_home="/tmp/actl-e2e-home-$session"
remote() { ssh "$ssh_host" "$@"; }

failed() {
    local step=$1 steps_json=""
    if (( ${#steps[@]} )); then
        printf -v steps_json '"%s",' "${steps[@]}"
        steps_json=${steps_json%,}
    fi
    printf '{"ok":false,"steps":[%s],"failed":"%s","ms":%s}\n' \
        "$steps_json" "$step" "$((( $(date +%s%N) - start_ns ) / 1000000))"
    exit 1
}

cleanup() {
    if [[ $created -eq 1 ]]; then
        if [[ -n $ssh_host ]]; then
            remote tmux kill-session -t "$session" >/dev/null 2>&1 || true
        else
            tmux kill-session -t "$session" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -n $ssh_host ]]; then
        remote rm -rf "$remote_config_dir" "$remote_home" >/dev/null 2>&1 || true
    fi
    rm -rf "$config_dir"
}
trap cleanup EXIT INT TERM

if [[ -n $ssh_host ]]; then
    if remote tmux has-session -t "$session" >/dev/null 2>&1; then
        echo "session already exists: $session" >&2
        exit 1
    fi
else
    if tmux has-session -t "$session" >/dev/null 2>&1; then
        echo "session already exists: $session" >&2
        exit 1
    fi
fi

probe="ACTL_E2E_PROBE"
printf '{"clipboard_backend":"auto","agents":{"commandcode":{"target":"%s:0.0"}}}\n' "$session" >"$config"

wait_for_prompt() {
    local pane=$1 output="" deadline=$((SECONDS + 10))
    while (( SECONDS < deadline )); do
        output=$(tmux capture-pane -p -t "$pane" 2>/dev/null || true)
        [[ "$output" == *"STUB_PROMPT>"* ]] && return 0
        sleep 0.1
    done
    echo "stub prompt did not appear: $session" >&2
    return 1
}

if [[ -n $ssh_host ]]; then
    remote_stub="$remote_home/stub_agent.py"
    remote_session_file="$remote_home/.commandcode/e2e/session.jsonl"
    remote mkdir -p "$remote_home/.commandcode/e2e" "$remote_config_dir"
    base64 <"$root/tests/fixtures/stub_agent.py" | remote "base64 -d > '$remote_stub'"
    remote "printf '%s\\n' '{\"clipboard_backend\":\"auto\",\"agents\":{\"commandcode\":{\"target\":\"$session:0.0\"}}}' > '$remote_config_dir/config.json'"
    remote "tmux new-session -d -s '$session' -c '$remote_home' -- bash -lc 'exec -a commandcode python3 \"$remote_stub\" --session-file \"$remote_session_file\"'"
    created=1
    if ! remote bash -s -- "$session" <<'REMOTE_WAIT'
session=$1
for i in $(seq 1 100); do
    tmux capture-pane -p -t "$session:0.0" 2>/dev/null | grep -q 'STUB_PROMPT>' && exit 0
    sleep .1
done
exit 1
REMOTE_WAIT
    then failed prompt; fi
    if ! send_output=$(printf '%s' "$probe" | remote "ACTL_CONFIG_PATH='$remote_config_dir/config.json' HOME='$remote_home' ~/.local/bin/actl send commandcode"); then failed send; fi
    if [[ "$send_output" != *"에게 보냈어요"* ]]; then failed send; fi
    steps+=(send)
    if ! result=$(remote "ACTL_CONFIG_PATH='$remote_config_dir/config.json' HOME='$remote_home' ~/.local/bin/actl copy commandcode --print"); then failed result; fi
else
    session_file="$run_home/.commandcode/e2e/session.jsonl"
    tmux new-session -d -s "$session" -c "$root" -- bash -lc "exec -a commandcode python3 '$root/tests/fixtures/stub_agent.py' --session-file '$session_file'"
    created=1
    if ! wait_for_prompt "$session:0.0"; then failed prompt; fi
    if ! send_output=$(printf '%s' "$probe" | ACTL_CONFIG_PATH="$config" HOME="$run_home" "$root/scripts/actl" send commandcode); then failed send; fi
    if [[ "$send_output" != *"에게 보냈어요"* ]]; then failed send; fi
    steps+=(send)
    if ! result=$(ACTL_CONFIG_PATH="$config" HOME="$run_home" "$root/scripts/actl" copy commandcode --print); then failed result; fi
fi

if [[ "$result" != "RESULT::$probe" ]]; then failed copy; fi
steps+=(result copy)
ms=$((( $(date +%s%N) - start_ns ) / 1000000))
printf '{"ok":true,"steps":["send","result","copy"],"ms":%s,"session":"%s"}\n' "$ms" "$session"
