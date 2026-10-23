#!/usr/bin/env bash

set -euo pipefail

# Render the three project LaunchAgents and, only when explicitly requested,
# install them into one exact user LaunchAgents directory. The default is a
# validation-only preview. This script intentionally does not read any
# environment/configuration file; the application reads its normal files
# after launchd sets WorkingDirectory.

SCRIPT_PATH=$0
case "$SCRIPT_PATH" in
	/*) ;;
	*) SCRIPT_PATH="$(pwd -P)/$SCRIPT_PATH" ;;
esac
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd -P)"

PROJECT_DIR="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
LAUNCH_AGENTS_DIR=""
INSTALL=0
LOAD=0
UNLOAD=0
REPLACE=0
STAGING_DIR=""

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
Usage:
  manage_launch_agents.sh [options]

Default mode renders and validates temporary plists, then removes them.
It does not write the user LaunchAgents target and does not call launchctl.

Options:
  --project-dir PATH          Absolute project directory (default: this project)
  --launch-agents-dir PATH    Absolute .../Library/LaunchAgents directory
  --install                   Install validated plists atomically
  --load                      Bootstrap agents after --install (explicit)
  --replace                   Allow replacing a different existing plist
  --unload                    Explicitly boot out the three known labels
  --dry-run, --preview        Force the non-mutating default mode
  -h, --help                  Show this help

--load requires --install. --replace is only accepted with --install.
--unload cannot be combined with --install, --load, or --replace.
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

assert_under() {
	case "$2" in
		"$1"/*) ;;
		*) die "$3 is outside its allowed root: $2" ;;
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

escape_sed_replacement() {
	printf '%s' "$1" | /usr/bin/sed 's/[\\&|]/\\&/g'
}

cleanup_staging() {
	local name
	if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
		case "$STAGING_DIR" in
			/tmp/life-inbox-launchagents.*) ;;
			*) die "refusing to clean an unexpected staging path: $STAGING_DIR" ;;
		esac
		for name in com.life-inbox.bot.plist com.life-inbox.web.plist com.life-inbox.backup.plist; do
			if [ -e "$STAGING_DIR/$name" ] || [ -L "$STAGING_DIR/$name" ]; then
				/bin/rm -f "$STAGING_DIR/$name"
			fi
		done
		/bin/rmdir "$STAGING_DIR" 2>/dev/null || true
	fi
}

trap cleanup_staging EXIT HUP INT TERM

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
		--install)
			INSTALL=1
			shift
			;;
		--load)
			LOAD=1
			shift
			;;
		--replace)
			REPLACE=1
			shift
			;;
		--unload)
			UNLOAD=1
			shift
			;;
		--dry-run|--preview)
			[ "$INSTALL" -eq 0 ] || die "--dry-run/--preview cannot be combined with --install"
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

[ "$LOAD" -eq 0 ] || [ "$INSTALL" -eq 1 ] || die "--load requires explicit --install"
[ "$REPLACE" -eq 0 ] || [ "$INSTALL" -eq 1 ] || die "--replace requires explicit --install"
if [ "$UNLOAD" -eq 1 ]; then
	[ "$INSTALL" -eq 0 ] || die "--unload cannot be combined with --install"
	[ "$LOAD" -eq 0 ] || die "--unload cannot be combined with --load"
	[ "$REPLACE" -eq 0 ] || die "--unload cannot be combined with --replace"
fi

require_absolute_path "$PROJECT_DIR" "project directory"
[ -d "$PROJECT_DIR" ] || die "project directory does not exist: $PROJECT_DIR"
PROJECT_DIR="$(cd -P "$PROJECT_DIR" && pwd -P)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
DEFAULT_BACKUP_DIR="$PROJECT_DIR/data/backups"
assert_under "$PROJECT_DIR" "$PYTHON_BIN" "project Python"
assert_under "$PROJECT_DIR" "$LOG_DIR" "log directory"
assert_under "$PROJECT_DIR" "$DEFAULT_BACKUP_DIR" "default backup directory"
[ -x "$PYTHON_BIN" ] || die "project .venv Python is not executable: $PYTHON_BIN"
if [ -e "$LOG_DIR" ] && [ ! -d "$LOG_DIR" ]; then
	die "project log path is not a directory: $LOG_DIR"
fi
if [ -L "$LOG_DIR" ]; then
	die "project log directory must not be a symlink: $LOG_DIR"
fi

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

if [ "$UNLOAD" -eq 1 ]; then
	LAUNCHCTL=""
	if [ -x /bin/launchctl ]; then
		LAUNCHCTL=/bin/launchctl
	elif command -v launchctl >/dev/null 2>&1; then
		LAUNCHCTL="$(command -v launchctl)"
	else
		die "launchctl is unavailable; no agent was unloaded"
	fi
	USER_ID="$(/usr/bin/id -u 2>/dev/null || id -u)"
	DOMAIN="gui/$USER_ID"
	for LABEL in com.life-inbox.bot com.life-inbox.web com.life-inbox.backup; do
		if "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
			"$LAUNCHCTL" bootout "$DOMAIN/$LABEL"
			printf 'Unloaded %s\n' "$LABEL"
		else
			printf 'Not loaded: %s\n' "$LABEL"
		fi
	done
	exit 0
fi

PLUTIL=""
if command -v plutil >/dev/null 2>&1; then
	PLUTIL="$(command -v plutil)"
else
	die "plutil is required to validate rendered LaunchAgents"
fi

STAGING_DIR="$(/usr/bin/mktemp -d /tmp/life-inbox-launchagents.XXXXXX)"
case "$STAGING_DIR" in
	/tmp/life-inbox-launchagents.*) ;;
	*) die "mktemp returned an unexpected staging path: $STAGING_DIR" ;;
esac

PROJECT_REPLACEMENT="$(escape_sed_replacement "$PROJECT_DIR")"
PYTHON_REPLACEMENT="$(escape_sed_replacement "$PYTHON_BIN")"
LOG_REPLACEMENT="$(escape_sed_replacement "$LOG_DIR")"

render_one() {
	local template output
	template="$1"
	output="$2"
	[ -f "$SCRIPT_DIR/$template" ] || die "template not found: $SCRIPT_DIR/$template"
	/usr/bin/sed \
		-e "s|__ABS_PROJECT_DIR__|$PROJECT_REPLACEMENT|g" \
		-e "s|__ABS_PYTHON_BIN__|$PYTHON_REPLACEMENT|g" \
		-e "s|__ABS_LOG_DIR__|$LOG_REPLACEMENT|g" \
		"$SCRIPT_DIR/$template" > "$output"
	if /usr/bin/grep -Eq '__ABS_[A-Z0-9_]+__' "$output"; then
		die "unresolved absolute-path placeholder in $output"
	fi
	case "$output" in
		"$STAGING_DIR"/*) ;;
		*) die "rendered plist escaped staging directory: $output" ;;
	esac
	"$PLUTIL" -lint "$output" >/dev/null || die "plutil rejected rendered plist: $output"
	/usr/bin/grep -Fq "$PROJECT_DIR" "$output" || die "project path missing from rendered plist: $output"
	/usr/bin/grep -Fq "$PYTHON_BIN" "$output" || die "Python path missing from rendered plist: $output"
	/usr/bin/grep -Fq "$LOG_DIR" "$output" || die "log path missing from rendered plist: $output"
}

render_one com.life-inbox.bot.plist.template "$STAGING_DIR/com.life-inbox.bot.plist"
render_one com.life-inbox.web.plist.template "$STAGING_DIR/com.life-inbox.web.plist"
render_one com.life-inbox.backup.plist.template "$STAGING_DIR/com.life-inbox.backup.plist"

if [ "$INSTALL" -eq 0 ]; then
	printf 'DRY RUN: rendered and linted three LaunchAgents in a temporary directory.\n'
	printf 'Project: %s\n' "$PROJECT_DIR"
	printf 'Target (not modified): %s\n' "$LAUNCH_AGENTS_DIR"
	printf 'Would install: %s\n' "$LAUNCH_AGENTS_DIR/com.life-inbox.bot.plist"
	printf 'Would install: %s\n' "$LAUNCH_AGENTS_DIR/com.life-inbox.web.plist"
	printf 'Would install: %s\n' "$LAUNCH_AGENTS_DIR/com.life-inbox.backup.plist"
	printf 'No LaunchAgents files were changed; launchctl was not called.\n'
	exit 0
fi

if [ -e "$LAUNCH_AGENTS_DIR" ] && [ -L "$LAUNCH_AGENTS_DIR" ]; then
	die "LaunchAgents directory must not be a symlink: $LAUNCH_AGENTS_DIR"
fi
if [ ! -d "$LAUNCH_AGENTS_DIR" ]; then
	mkdir -p "$LAUNCH_AGENTS_DIR"
fi
[ -d "$LAUNCH_AGENTS_DIR" ] || die "LaunchAgents target is not a directory: $LAUNCH_AGENTS_DIR"
REQUESTED_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR"
LAUNCH_AGENTS_DIR="$(cd -P "$LAUNCH_AGENTS_DIR" && pwd -P)"
[ "$LAUNCH_AGENTS_DIR" = "$REQUESTED_LAUNCH_AGENTS_DIR" ] || die "LaunchAgents target contains a symlinked path component"
case "$LAUNCH_AGENTS_DIR" in
	*/Library/LaunchAgents) ;;
	*) die "resolved LaunchAgents target escaped the exact user directory" ;;
esac

if [ ! -d "$LOG_DIR" ]; then
	mkdir -p "$LOG_DIR"
fi
[ -d "$LOG_DIR" ] || die "project log directory could not be created: $LOG_DIR"

install_one() {
	local staged destination temporary
	staged="$1"
	destination="$2"
	assert_under "$LAUNCH_AGENTS_DIR" "$destination" "LaunchAgent destination"
	if [ -L "$destination" ]; then
		die "refusing to replace a symlinked LaunchAgent: $destination"
	fi
	if [ -e "$destination" ]; then
		if /usr/bin/cmp -s "$staged" "$destination"; then
			printf 'Unchanged %s\n' "$destination"
			return 0
		fi
		[ "$REPLACE" -eq 1 ] || die "existing plist differs; use --replace explicitly: $destination"
	fi
	temporary="$(mktemp "$LAUNCH_AGENTS_DIR/.life-inbox-launchagent.XXXXXX")"
	assert_under "$LAUNCH_AGENTS_DIR" "$temporary" "temporary LaunchAgent"
	if ! /bin/cp "$staged" "$temporary"; then
		/bin/rm -f "$temporary"
		die "could not stage $destination"
	fi
	if ! /bin/chmod 600 "$temporary"; then
		/bin/rm -f "$temporary"
		die "could not set permissions on $destination"
	fi
	if ! /bin/mv -f "$temporary" "$destination"; then
		/bin/rm -f "$temporary"
		die "could not install $destination"
	fi
	printf 'Installed %s\n' "$destination"
}

if [ "$LOAD" -eq 1 ]; then
	LAUNCHCTL=""
	if [ -x /bin/launchctl ]; then
		LAUNCHCTL=/bin/launchctl
	elif command -v launchctl >/dev/null 2>&1; then
		LAUNCHCTL="$(command -v launchctl)"
	else
		die "launchctl is unavailable; no agent was loaded"
	fi
	USER_ID="$(/usr/bin/id -u 2>/dev/null || id -u)"
	DOMAIN="gui/$USER_ID"
	for LABEL in com.life-inbox.bot com.life-inbox.web com.life-inbox.backup; do
		if "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
			die "$LABEL is already loaded; run --unload explicitly before --install --load"
		fi
	done
fi

install_one "$STAGING_DIR/com.life-inbox.bot.plist" "$LAUNCH_AGENTS_DIR/com.life-inbox.bot.plist"
install_one "$STAGING_DIR/com.life-inbox.web.plist" "$LAUNCH_AGENTS_DIR/com.life-inbox.web.plist"
install_one "$STAGING_DIR/com.life-inbox.backup.plist" "$LAUNCH_AGENTS_DIR/com.life-inbox.backup.plist"

if [ "$LOAD" -eq 1 ]; then
	for LABEL in com.life-inbox.bot com.life-inbox.web com.life-inbox.backup; do
		"$LAUNCHCTL" bootstrap "$DOMAIN" "$LAUNCH_AGENTS_DIR/$LABEL.plist"
		printf 'Loaded %s\n' "$LABEL"
	done
else
	printf 'Installed only; launchctl was not called. Use --load explicitly after review.\n'
fi
