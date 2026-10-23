#!/usr/bin/env bash

set -euo pipefail

# Read-only diagnostics for the three known project agents. Do not source,
# print, or parse application configuration here: the app's optional `check`
# command is the only configuration-aware path and is opt-in below.

SCRIPT_PATH=$0
case "$SCRIPT_PATH" in
	/*) ;;
	*) SCRIPT_PATH="$(pwd -P)/$SCRIPT_PATH" ;;
esac
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd -P)"
PROJECT_DIR="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
LAUNCH_AGENTS_DIR=""
APP_CHECK=0

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
Usage:
  status_launch_agents.sh [options]

Options:
  --project-dir PATH          Absolute project directory (default: this project)
  --launch-agents-dir PATH    Absolute .../Library/LaunchAgents directory
  --app-check                 Run `python -m life_inbox check` (safe summary only)
  -h, --help                  Show this help
EOF
}

require_absolute_path() {
	case "$1" in
		/*) ;;
		*) die "$2 must be an absolute path: $1" ;;
	esac
	case "$1" in
		*$'\n'*|*$'\r'*) die "$2 must not contain a newline" ;;
	esac
}

resolve_user_directory() {
	local current_user candidate
	current_user="$(/usr/bin/id -un 2>/dev/null || id -un)" || return 1
	candidate=""
	if [ -x /usr/bin/dscl ]; then
		candidate="$(/usr/bin/dscl . -read "/Users/$current_user" NFSHomeDirectory 2>/dev/null \
			| /usr/bin/awk '{print $2}')"
	elif command -v getent >/dev/null 2>&1; then
		candidate="$(getent passwd "$current_user" | /usr/bin/awk -F: '{print $6}')"
	fi
	case "$candidate" in
		/*) printf '%s\n' "$candidate" ;;
		*) return 1 ;;
	esac
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--project-dir)
			[ "$#" -ge 2 ] || die "--project-dir needs a path"
			PROJECT_DIR="$2"
			shift 2
			;;
		--launch-agents-dir)
			[ "$#" -ge 2 ] || die "--launch-agents-dir needs a path"
			LAUNCH_AGENTS_DIR="$2"
			shift 2
			;;
		--app-check)
			APP_CHECK=1
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			usage >&2
			die "unknown option: $1"
			;;
	esac
done

require_absolute_path "$PROJECT_DIR" "project directory"
[ -d "$PROJECT_DIR" ] || die "project directory does not exist: $PROJECT_DIR"
PROJECT_DIR="$(cd -P "$PROJECT_DIR" && pwd -P)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
BACKUP_DIR="$PROJECT_DIR/data/backups"

if [ -z "$LAUNCH_AGENTS_DIR" ]; then
	USER_DIRECTORY="$(resolve_user_directory 2>/dev/null || true)"
	[ -n "$USER_DIRECTORY" ] || die "could not resolve the user directory; pass --launch-agents-dir with an absolute path"
	LAUNCH_AGENTS_DIR="$USER_DIRECTORY/Library/LaunchAgents"
fi
require_absolute_path "$LAUNCH_AGENTS_DIR" "LaunchAgents directory"
case "$LAUNCH_AGENTS_DIR" in
	*/) LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR%/}" ;;
esac
case "$LAUNCH_AGENTS_DIR" in
	*/Library/LaunchAgents) ;;
	*) die "LaunchAgents directory must be an exact .../Library/LaunchAgents path: $LAUNCH_AGENTS_DIR" ;;
esac
case "$LAUNCH_AGENTS_DIR" in
	*"/../"*|*"/./"*|*/..|*/.) die "LaunchAgents directory must not contain . or .. components" ;;
esac
if [ -e "$LAUNCH_AGENTS_DIR" ]; then
	[ -d "$LAUNCH_AGENTS_DIR" ] || die "LaunchAgents target is not a directory: $LAUNCH_AGENTS_DIR"
	[ ! -L "$LAUNCH_AGENTS_DIR" ] || die "LaunchAgents target must not be a symlink: $LAUNCH_AGENTS_DIR"
	REQUESTED_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR"
	RESOLVED_LAUNCH_AGENTS_DIR="$(cd -P "$LAUNCH_AGENTS_DIR" && pwd -P)"
	[ "$RESOLVED_LAUNCH_AGENTS_DIR" = "$REQUESTED_LAUNCH_AGENTS_DIR" ] || die "LaunchAgents target contains a symlinked path component"
fi

printf 'Project: %s\n' "$PROJECT_DIR"
if [ -x "$PYTHON_BIN" ]; then
	printf 'Python: ready (%s)\n' "$PYTHON_BIN"
else
	printf 'Python: missing or not executable (%s)\n' "$PYTHON_BIN"
fi
if [ -d "$LOG_DIR" ] && [ ! -L "$LOG_DIR" ]; then
	printf 'Logs: ready (%s)\n' "$LOG_DIR"
else
	printf 'Logs: missing or unsafe (%s)\n' "$LOG_DIR"
fi
if [ -d "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR" ]; then
	printf 'Default backup directory: present (%s)\n' "$BACKUP_DIR"
else
	printf 'Default backup directory: missing or unsafe (%s)\n' "$BACKUP_DIR"
fi

PLUTIL=""
if command -v plutil >/dev/null 2>&1; then
	PLUTIL="$(command -v plutil)"
else
	printf 'Plist lint: unavailable (plutil not found)\n'
fi

STATUS=0
for LABEL in com.life-inbox.bot com.life-inbox.web com.life-inbox.backup; do
	PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
	case "$PLIST" in
		"$LAUNCH_AGENTS_DIR"/*) ;;
		*) die "plist path escaped LaunchAgents directory: $PLIST" ;;
	esac
	if [ -f "$PLIST" ] && [ ! -L "$PLIST" ]; then
		if [ -n "$PLUTIL" ] && "$PLUTIL" -lint "$PLIST" >/dev/null 2>&1; then
			printf '%s plist: present and valid\n' "$LABEL"
		else
			printf '%s plist: present but invalid or cannot be linted\n' "$LABEL"
			STATUS=1
		fi
	else
		printf '%s plist: missing or unsafe\n' "$LABEL"
		STATUS=1
	fi
done

if [ -x /bin/launchctl ]; then
	USER_ID="$(/usr/bin/id -u 2>/dev/null || id -u)"
	DOMAIN="gui/$USER_ID"
	for LABEL in com.life-inbox.bot com.life-inbox.web com.life-inbox.backup; do
		if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
			printf '%s launchd state: loaded\n' "$LABEL"
		else
			printf '%s launchd state: not loaded\n' "$LABEL"
		fi
	done
else
	printf 'launchd state: unavailable (launchctl not found)\n'
fi

if [ "$APP_CHECK" -eq 1 ]; then
	if [ -x "$PYTHON_BIN" ]; then
		printf 'Application check (safe summary):\n'
		"$PYTHON_BIN" -m life_inbox check
	else
		printf 'Application check: skipped because project Python is unavailable\n'
		STATUS=1
	fi
fi

exit "$STATUS"
