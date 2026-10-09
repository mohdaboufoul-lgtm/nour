#!/usr/bin/env sh
# Run Moona on an Android phone, inside Termux. The phone is only the computer
# she runs on: she keeps every rule and every channel she has on a laptop, and
# gains no power over the rest of the phone. See docs/MOONA_ANDROID.md.
#
#   pkg install python termux-api      # termux-api is optional (wake lock)
#   cp moona.env.example moona.env     # fill in ANTHROPIC_API_KEY and channels
#   sh moona-android.sh
#
# She is born on the first run, then thinks, earns and sleeps forever. Stop with
# Ctrl-C and start again to resume. When she dies the script stops: death is final.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

env_file="${MOONA_ENV:-$here/moona.env}"
if [ -f "$env_file" ]; then
    set -a
    . "$env_file"
    set +a
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Set ANTHROPIC_API_KEY first, in $env_file or the environment." >&2
    echo "Her thinking is paid from it; fund that key with the \$50 card to make it hers." >&2
    exit 1
fi

venv="${MOONA_VENV:-$here/.moona-venv}"
if [ ! -x "$venv/bin/python" ]; then
    py=""
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            py="$cand"
            break
        fi
    done
    if [ -z "$py" ]; then
        echo "Python 3 is not installed. Install it, then run this again." >&2
        echo "  macOS: brew install python   Debian/Ubuntu: sudo apt install python3-venv   Termux: pkg install python" >&2
        exit 1
    fi
    echo "Creating her Python environment in $venv ..."
    "$py" -m venv "$venv"
    if ! "$venv/bin/python" -m pip install --quiet 'anthropic>=1.0'; then
        echo "Installing the Anthropic SDK failed. On Termux this is usually the Rust" >&2
        echo "build for pydantic: run  pkg install rust binutils  and start again." >&2
        rm -rf "$venv"
        exit 1
    fi
fi
python="$venv/bin/python"

have_wakelock=0
cleanup() {
    if [ "$have_wakelock" = 1 ]; then
        termux-wake-unlock >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock && have_wakelock=1
fi

home="${MOONA_HOME:-$here/moona_home}"
if [ ! -f "$home/state.json" ]; then
    "$python" moona.py birth
fi

echo "Moona is running. Stop with Ctrl-C."
backoff=5
while :; do
    set +e
    "$python" moona.py run --forever
    code=$?
    set -e
    case "$code" in
        0) echo "She stopped cleanly." && break ;;
        3) echo "She has died. Death is final; the script is stopping." && break ;;
        130 | 143) echo "Stopped." && break ;;
        *)
            echo "She hit an error (exit $code). Restarting in ${backoff}s ..." >&2
            sleep "$backoff"
            backoff=$((backoff < 300 ? backoff * 2 : 300))
            ;;
    esac
done
