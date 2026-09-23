#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--ssh HOST]" >&2
    exit 2
}

ssh_host=""
case "${1:-}" in
    "") ;;
    --ssh)
        [[ $# -eq 2 ]] || usage
        ssh_host=$2
        ;;
    *) usage ;;
esac

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
start_ns=$(date +%s%N)
session="actl-e2e-$(date +%s)"
config_dir=$(mktemp -d "${TMPDIR:-/tmp}/actl-e2e-config.XXXXXX")
config="$config_dir/config.json"
run_home="$config_dir/home"
mkdir -p "$run_home/.commandcode/projects/e2e"
created=0

cleanup() {
    if [[ $created -eq 1 ]]; then
        if [[ -n $ssh_host ]]; then
            ssh "$ssh_host" tmux kill-session -t "$session" >/dev/null 2>&1 || true
        else
            tmux kill-session -t "$session" >/dev/null 2>&1 || true
        fi
    fi
    rm -rf "$config_dir"
}
trap cleanup EXIT INT TERM

if [[ -n $ssh_host ]]; then
    if ssh "$ssh_host" tmux has-session -t "$session" >/dev/null 2>&1; then
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
stub="$root/tests/fixtures/stub_agent.py"
session_file="$run_home/.commandcode/projects/e2e/session.jsonl"
printf '{"clipboard_backend":"auto","agents":{"commandcode":{"target":"%s:0.0"}}}\n' "$session" >"$config"

if [[ -n $ssh_host ]]; then
    # The remote command is intentionally self-contained; only tmux is shared.
    remote_stub="import json,sys,time; p=sys.argv[1]; open(p,'w').write(json.dumps({'type':'session','cwd':__import__('os').getcwd()})+'\\n'); print('STUB_PROMPT>',flush=True); [((lambda l: (open(p,'a').write(json.dumps({'type':'message','message':{'role':'user','content':[{'type':'text','text':l.rstrip() }]}})+'\\n'+json.dumps({'type':'message','message':{'role':'assistant','content':[{'type':'text','text':'RESULT::'+l.rstrip()}]}})+'\\n'),print('RESULT::'+l.rstrip(),flush=True),print('STUB_PROMPT>',flush=True)))(line) for line in sys.stdin]"
    remote_home="/tmp/actl-e2e-home-$session"
    remote_config="/tmp/actl-e2e-config-$session/config.json"
    ssh "$ssh_host" mkdir -p "$remote_home/.commandcode/projects/e2e" "/tmp/actl-e2e-config-$session"
    ssh "$ssh_host" "printf '%s\\n' '$remote_config' >/dev/null; printf '{\"clipboard_backend\":\"auto\",\"agents\":{\"commandcode\":{\"target\":\"$session:0.0\"}}}\\n' > '$remote_config'"
    ssh "$ssh_host" "HOME='$remote_home' tmux new-session -d -s '$session' -c '$root' -- bash -lc 'exec -a commandcode python3 -c \"$remote_stub\" -- '$remote_home'/.commandcode/projects/e2e/session.jsonl'"
    created=1
    send_output=$(printf '%s\n' "$probe" | ACTL_CONFIG_PATH="$config" HOME="$run_home" "$root/scripts/actl" send commandcode --ssh "$ssh_host")
    result=$(ACTL_CONFIG_PATH="$remote_config" HOME="$remote_home" "$root/scripts/actl" copy commandcode --print --ssh "$ssh_host")
else
    tmux new-session -d -s "$session" -c "$root" -- bash -lc "exec -a commandcode python3 '$stub' --session-file '$session_file'"
    created=1
    send_output=$(printf '%s\n' "$probe" | ACTL_CONFIG_PATH="$config" HOME="$run_home" "$root/scripts/actl" send commandcode)
    result=$(ACTL_CONFIG_PATH="$config" HOME="$run_home" "$root/scripts/actl" copy commandcode --print)
fi

[[ "$send_output" == *"sent to"* ]]
[[ "$result" == "RESULT::$probe" ]]
ms=$((( $(date +%s%N) - start_ns ) / 1000000))
printf '{"ok":true,"steps":["send","result","copy"],"ms":%s,"session":"%s"}\n' "$ms" "$session"
