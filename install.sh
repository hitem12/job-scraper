#!/bin/sh
# install.sh — installs job-scraper as a cron-driven service (OpenRC)
# POSIX sh / busybox ash compatible  •  must be run as root
#
# Usage:
#   sudo ./install.sh             # install
#   sudo ./install.sh --uninstall # remove
set -eu

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit before the first run
# ══════════════════════════════════════════════════════════════════════
APP_NAME="job-scraper"
APP_USER="jobscraper"          # system user, no login shell
APP_ENTRY="scraper.py"
CRON_SCHEDULE="0 3 * * *"

# Local ntfy — leave NTFY_TOPIC empty to skip ntfy args in cron
NTFY_SERVER="http://[::1]:2345"  # e.g. "https://ntfy.sh"
NTFY_TOPIC="jobs"                  # e.g. "job-alerts"

# Python deps — keep in sync with pyproject.toml [project.dependencies]
PYTHON_DEPS="requests>=2.31"

# ══════════════════════════════════════════════════════════════════════
# DERIVED PATHS — do not edit
# ══════════════════════════════════════════════════════════════════════
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
LOG_DIR="/var/log/${APP_NAME}"
CONF_DIR="/etc/${APP_NAME}"
TOKEN_FILE="${CONF_DIR}/ntfy-token"
WRAPPER="/usr/local/bin/${APP_NAME}-scan"
WEBUI_WRAPPER="/usr/local/bin/${APP_NAME}-ui"
WEBUI_SERVICE="${APP_NAME}-ui"
WEBUI_INITD="/etc/init.d/${WEBUI_SERVICE}"
LOCK_FILE="/run/${APP_NAME}/cron.lock"
RUNTIME_DIR="/run/${APP_NAME}"
LOCAL_START="/etc/local.d/${APP_NAME}.start"

# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$*"; }

need_root() { [ "$(id -u)" -eq 0 ] || die "Run as root (or with sudo)."; }

confirm() {
  printf '%s [y/N] ' "$1"
  read -r _ans
  case "${_ans}" in [Yy]*) return 0;; *) return 1;; esac
}

detect_env() {
  # Package manager
  if command -v apk >/dev/null 2>&1; then
    PKG_MGR="apk"
  elif command -v pacman >/dev/null 2>&1; then
    PKG_MGR="pacman"
  else
    PKG_MGR="unknown"
    warn "Unknown package manager — skipping system dependency check."
  fi

  # nologin shell
  if [ -x /sbin/nologin ]; then
    NOLOGIN="/sbin/nologin"
  elif [ -x /usr/bin/nologin ]; then
    NOLOGIN="/usr/bin/nologin"
  else
    NOLOGIN="/bin/false"
  fi

  # Cron directory style
  if [ -d /etc/crontabs ]; then
    CRON_STYLE="crontabs"        # busybox crond (Alpine)
    CRON_FILE="/etc/crontabs/${APP_USER}"
  elif [ -d /etc/cron.d ]; then
    CRON_STYLE="crond"           # cronie / dcron (Artix)
    CRON_FILE="/etc/cron.d/${APP_NAME}"
  else
    CRON_STYLE="none"
    CRON_FILE=""
  fi

  # flock
  if command -v flock >/dev/null 2>&1; then
    HAS_FLOCK=1
  else
    HAS_FLOCK=0
    warn "flock not found — parallel cron runs will not be serialised."
    warn "Install util-linux to get flock."
  fi

  # uv (preferred over venv+pip when available)
  if command -v uv >/dev/null 2>&1; then
    HAS_UV=1
  else
    HAS_UV=0
  fi

  # runuser bypasses PAM auth so root can switch user without a password prompt.
  # su may ask for the target user's password on some PAM configurations even
  # when invoked by root. Fall back to su only if runuser is absent.
  if command -v runuser >/dev/null 2>&1; then
    RUNAS="runuser -s /bin/sh"
  else
    RUNAS="su -s /bin/sh"
  fi
}

# ══════════════════════════════════════════════════════════════════════
# INSTALL
# ══════════════════════════════════════════════════════════════════════
install() {
  need_root
  detect_env

  _token_created=0
  _no_logrotate=0

  # ── 1. System dependencies ─────────────────────────────────────────
  step "Checking system dependencies"
  case "${PKG_MGR}" in
    apk)
      NEEDED=""
      for pkg in python3 py3-pip; do
        apk info -e "${pkg}" >/dev/null 2>&1 || NEEDED="${NEEDED} ${pkg}"
      done
      if [ -n "${NEEDED}" ]; then
        printf '  Installing:%s\n' "${NEEDED}"
        # shellcheck disable=SC2086
        apk add --no-cache ${NEEDED}
      else
        ok "python3 and py3-pip already installed."
      fi
      ;;
    pacman)
      NEEDED=""
      for pkg in python python-pip; do
        pacman -Q "${pkg}" >/dev/null 2>&1 || NEEDED="${NEEDED} ${pkg}"
      done
      if [ -n "${NEEDED}" ]; then
        printf '  Installing:%s\n' "${NEEDED}"
        # shellcheck disable=SC2086
        pacman -S --noconfirm --needed ${NEEDED}
      else
        ok "python and python-pip already installed."
      fi
      ;;
    *)
      ok "Skipped (unknown package manager)."
      ;;
  esac

  # ── 2. System group and user ───────────────────────────────────────
  step "Ensuring system group '${APP_USER}'"
  if grep -q "^${APP_USER}:" /etc/group 2>/dev/null; then
    ok "Group '${APP_USER}' already exists."
  else
    case "${PKG_MGR}" in
      apk)
        addgroup -S "${APP_USER}" \
          || die "addgroup failed — see output above."
        ;;
      *)
        groupadd -r "${APP_USER}" \
          || die "groupadd failed — see output above."
        ;;
    esac
    grep -q "^${APP_USER}:" /etc/group \
      || die "Group '${APP_USER}' still not found after creation attempt."
    ok "Created system group '${APP_USER}'."
  fi

  step "Ensuring system user '${APP_USER}'"
  if id "${APP_USER}" >/dev/null 2>&1; then
    ok "User '${APP_USER}' already exists."
  else
    case "${PKG_MGR}" in
      apk)
        adduser -S -D -H -s "${NOLOGIN}" -G "${APP_USER}" "${APP_USER}" \
          || die "adduser failed — see output above."
        ;;
      *)
        useradd -r -M -s "${NOLOGIN}" -g "${APP_USER}" "${APP_USER}" \
          || die "useradd failed — see output above."
        ;;
    esac
    id "${APP_USER}" >/dev/null 2>&1 \
      || die "User '${APP_USER}' still not found after creation attempt."
    ok "Created system user '${APP_USER}' (no home, no shell)."
  fi

  # ── 3. Copy application files ──────────────────────────────────────
  step "Syncing application files → ${APP_DIR}"
  mkdir -p "${APP_DIR}"

  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude='.git/' \
      --exclude='__pycache__/' \
      --exclude='*.pyc' \
      --exclude='venv/' \
      --exclude='data/' \
      --exclude='matches.log' \
      --exclude='matches.md' \
      --exclude='install.sh' \
      "${SRC_DIR}/" "${APP_DIR}/"
  else
    cp -rp "${SRC_DIR}/." "${APP_DIR}/"
    rm -rf "${APP_DIR}/.git" \
           "${APP_DIR}/venv" \
           "${APP_DIR}/install.sh" \
           "${APP_DIR}/matches.log" \
           "${APP_DIR}/matches.md" 2>/dev/null || true
    find "${APP_DIR}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find "${APP_DIR}" -name '*.pyc' -delete 2>/dev/null || true
  fi

  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
  chmod 750 "${APP_DIR}"
  ok "Files synced."

  # data/ must be writable — app stores seen_urls.json and matches.json there
  mkdir -p "${APP_DIR}/data"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/data"
  chmod 750 "${APP_DIR}/data"

  # ── 4. Runtime directory (lock file lives here) ────────────────────
  step "Creating runtime directory → ${RUNTIME_DIR}"
  mkdir -p "${RUNTIME_DIR}"
  chown "${APP_USER}:${APP_USER}" "${RUNTIME_DIR}"
  chmod 750 "${RUNTIME_DIR}"
  # /run is tmpfs — recreate the dir on every boot via an OpenRC local.d script
  # (works on Alpine and Artix; does not require opentmpfiles)
  cat > "${LOCAL_START}" <<EOF
#!/bin/sh
mkdir -p '${RUNTIME_DIR}'
chown '${APP_USER}:${APP_USER}' '${RUNTIME_DIR}'
chmod 750 '${RUNTIME_DIR}'
EOF
  chmod 755 "${LOCAL_START}"
  ok "Done (boot script → ${LOCAL_START})"

  # ── 5. Log directory ───────────────────────────────────────────────
  step "Creating log directory → ${LOG_DIR}"
  mkdir -p "${LOG_DIR}"
  chown "${APP_USER}:${APP_USER}" "${LOG_DIR}"
  chmod 750 "${LOG_DIR}"
  ok "Done."

  # ── 6. Config directory & ntfy token ──────────────────────────────
  step "Setting up config directory → ${CONF_DIR}"
  mkdir -p "${CONF_DIR}"
  # Keep CONF_DIR readable by root only; token is readable by APP_USER only
  chmod 755 "${CONF_DIR}"
  if [ -f "${TOKEN_FILE}" ]; then
    ok "Token file already exists — not overwriting."
  else
    cat > "${TOKEN_FILE}" <<EOF
# ntfy authentication token for ${APP_NAME}
#
# If your ntfy server requires authentication, paste the token here.
# The token is read by the application at runtime and passed as:
#   Authorization: Bearer <token>
#
# How to obtain a token:
#   - Self-hosted ntfy (${NTFY_SERVER}):
#       ntfy token add ${APP_USER}
#     or via the web UI: Settings → Users & access control → Access tokens
#   - ntfy.sh (public cloud):
#       Log in at https://ntfy.sh and go to Account → Access tokens
#
# Leave this file empty or with only comments if your server has no auth.
#
# Example:
#   tk_AgCKGqGHznCdRvFHVFqKGGPYbRl0iy7v

EOF
    chown "${APP_USER}:${APP_USER}" "${TOKEN_FILE}"
    chmod 600 "${TOKEN_FILE}"
    _token_created=1
    ok "Created token file with instructions: ${TOKEN_FILE}"
  fi

  # ── 7. Python virtualenv ───────────────────────────────────────────
  # Write deps to a temp file so '>=' version specifiers are never
  # expanded inline in a shell command (where '>' is a redirect operator).
  _reqs=$(mktemp)
  cat > "${_reqs}" <<PYREQS
${PYTHON_DEPS}
PYREQS
  chmod 644 "${_reqs}"

  if [ "${HAS_UV}" -eq 1 ]; then
    step "Setting up Python virtualenv → ${VENV_DIR} (via uv)"
    if [ ! -d "${VENV_DIR}" ]; then
      ${RUNAS} "${APP_USER}" -c "UV_NO_CACHE=1 uv venv '${VENV_DIR}'"
      ok "Virtualenv created."
    else
      ok "Virtualenv already exists."
    fi

    step "Installing Python dependencies: ${PYTHON_DEPS} (via uv)"
    ${RUNAS} "${APP_USER}" -c \
      "UV_NO_CACHE=1 uv pip install --quiet --python '${VENV_DIR}/bin/python' -r '${_reqs}'"
  else
    step "Setting up Python virtualenv → ${VENV_DIR}"
    if [ ! -d "${VENV_DIR}" ]; then
      python3 -m venv "${VENV_DIR}"
      chown -R "${APP_USER}:${APP_USER}" "${VENV_DIR}"
      ok "Virtualenv created."
    else
      ok "Virtualenv already exists."
    fi

    step "Installing Python dependencies: ${PYTHON_DEPS}"
    ${RUNAS} "${APP_USER}" -c \
      "${VENV_DIR}/bin/pip install --quiet --upgrade pip && \
       ${VENV_DIR}/bin/pip install --quiet -r '${_reqs}'"
  fi
  rm -f "${_reqs}"
  ok "Dependencies installed."

  # ── 8. Wrapper scripts ─────────────────────────────────────────────
  step "Creating wrappers → ${WRAPPER}, ${WEBUI_WRAPPER}"
  cat > "${WRAPPER}" <<EOF
#!/bin/sh
exec ${VENV_DIR}/bin/python ${APP_DIR}/${APP_ENTRY} "\$@"
EOF
  chown root:root "${WRAPPER}"
  chmod 755 "${WRAPPER}"

  cat > "${WEBUI_WRAPPER}" <<EOF
#!/bin/sh
exec ${VENV_DIR}/bin/python ${APP_DIR}/webui.py "\$@"
EOF
  chown root:root "${WEBUI_WRAPPER}"
  chmod 755 "${WEBUI_WRAPPER}"
  ok "Wrappers created."

  # ── 9. Web UI OpenRC service ───────────────────────────────────────
  step "Installing OpenRC service → ${WEBUI_INITD}"
  cat > "${WEBUI_INITD}" <<EOF
#!/sbin/openrc-run
description="job-scraper match browser"

command="${VENV_DIR}/bin/python"
command_args="${APP_DIR}/webui.py --no-browser"
command_user="${APP_USER}:${APP_USER}"
command_background=true
pidfile="/run/${WEBUI_SERVICE}.pid"
output_log="${LOG_DIR}/webui.log"
error_log="${LOG_DIR}/webui.log"
directory="${APP_DIR}"

depend() {
    need net
    after logger
}
EOF
  chmod 755 "${WEBUI_INITD}"
  chown root:root "${WEBUI_INITD}"

  if command -v rc-update >/dev/null 2>&1; then
    rc-update add "${WEBUI_SERVICE}" default 2>/dev/null || true
    ok "Service enabled in default runlevel."
    rc-service "${WEBUI_SERVICE}" start \
      && ok "${WEBUI_SERVICE} started." \
      || warn "Failed to start ${WEBUI_SERVICE} — check logs: tail -f ${LOG_DIR}/webui.log"
  else
    warn "rc-update not found — enable the service manually:"
    printf '      rc-update add %s default\n' "${WEBUI_SERVICE}"
    printf '      rc-service %s start\n' "${WEBUI_SERVICE}"
  fi

  # ── 10. Logrotate ─────────────────────────────────────────────────
  step "Configuring log rotation"
  if command -v logrotate >/dev/null 2>&1; then
    cat > "/etc/logrotate.d/${APP_NAME}" <<EOF
${LOG_DIR}/cron.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 640 ${APP_USER} ${APP_USER}
}
EOF
    ok "logrotate config → /etc/logrotate.d/${APP_NAME}"
  else
    _no_logrotate=1
    warn "logrotate not found — adding a weekly truncate line to the cron job."
  fi

  # ── 11. Cron job ───────────────────────────────────────────────────
  step "Installing cron job"

  [ -n "${CRON_FILE}" ] || die "No cron directory (/etc/crontabs or /etc/cron.d) found. Install crond first."

  # Build ntfy args (empty string if no topic configured)
  NTFY_ARGS=""
  if [ -n "${NTFY_TOPIC}" ]; then
    NTFY_ARGS=" --ntfy-topic ${NTFY_TOPIC} --ntfy-server ${NTFY_SERVER} --ntfy-token-file ${TOKEN_FILE}"
  fi

  INNER_CMD="cd ${APP_DIR} && ${VENV_DIR}/bin/python ${APP_DIR}/${APP_ENTRY}${NTFY_ARGS}"

  if [ "${HAS_FLOCK}" -eq 1 ]; then
    RUN_CMD="flock -n ${LOCK_FILE} /bin/sh -c '${INNER_CMD}'"
  else
    RUN_CMD="/bin/sh -c '${INNER_CMD}'"
  fi

  MAIN_CRON_CMD="${RUN_CMD} >> ${LOG_DIR}/cron.log 2>&1"

  case "${CRON_STYLE}" in
    crontabs)
      # busybox crond — file per user under /etc/crontabs/, no username field
      touch "${CRON_FILE}"
      chown root:root "${CRON_FILE}"
      chmod 600 "${CRON_FILE}"
      if grep -qF "${APP_DIR}/${APP_ENTRY}" "${CRON_FILE}" 2>/dev/null; then
        ok "Cron entry already present — skipping."
      else
        printf '%s %s\n' "${CRON_SCHEDULE}" "${MAIN_CRON_CMD}" >> "${CRON_FILE}"
        if [ "${_no_logrotate}" -eq 1 ]; then
          # Weekly log truncation at 03:00 Sunday
          printf '0 3 * * 0 truncate -s 0 %s/cron.log\n' "${LOG_DIR}" >> "${CRON_FILE}"
        fi
        ok "Entry added to ${CRON_FILE}"
      fi
      ;;
    crond)
      # cronie / dcron — /etc/cron.d/, includes username field
      if [ -f "${CRON_FILE}" ] && grep -qF "${APP_DIR}/${APP_ENTRY}" "${CRON_FILE}" 2>/dev/null; then
        ok "Cron entry already present — skipping."
      else
        printf '# managed by install.sh — do not edit the main entry manually\n' > "${CRON_FILE}"
        printf '%s %s %s\n' "${CRON_SCHEDULE}" "${APP_USER}" "${MAIN_CRON_CMD}" >> "${CRON_FILE}"
        if [ "${_no_logrotate}" -eq 1 ]; then
          printf '0 3 * * 0 %s truncate -s 0 %s/cron.log\n' "${APP_USER}" "${LOG_DIR}" >> "${CRON_FILE}"
        fi
        chown root:root "${CRON_FILE}"
        chmod 644 "${CRON_FILE}"
        ok "Created ${CRON_FILE}"
      fi
      ;;
  esac

  # ── 12. Smoke test — import check ─────────────────────────────────
  step "Running import smoke test"
  ${RUNAS} "${APP_USER}" -c \
    "cd ${APP_DIR} && ${VENV_DIR}/bin/python -c 'import scraper, profile, sources'" \
    && ok "Imports OK." \
    || die "Import test failed — check virtualenv and source files above."

  # ── 13. First manual run ───────────────────────────────────────────
  step "Running application once as '${APP_USER}'"
  printf '    (this will discover and score live offers — may take several minutes)\n'
  ${RUNAS} "${APP_USER}" -c \
    "cd ${APP_DIR} && ${VENV_DIR}/bin/python ${APP_DIR}/${APP_ENTRY}" \
    && ok "First run completed successfully." \
    || warn "First run exited non-zero — check output above."

  # ── 14. Check cron daemon ──────────────────────────────────────────
  step "Checking cron daemon"
  if command -v rc-service >/dev/null 2>&1; then
    if rc-service crond status >/dev/null 2>&1; then
      ok "crond is running."
    else
      warn "crond is NOT running. Enable and start it manually:"
      printf '      rc-update add crond default\n'
      printf '      rc-service crond start\n'
    fi
  else
    warn "rc-service not found — verify crond is running manually."
  fi

  # ── Summary ────────────────────────────────────────────────────────
  printf '\n\033[1;32m══ Installation complete ══\033[0m\n'
  printf '\n'
  printf '  %-28s %s:%s  %s\n' "${APP_DIR}/"              "${APP_USER}" "${APP_USER}" "750"
  printf '  %-28s %s:%s  %s\n' "${APP_DIR}/venv/"         "${APP_USER}" "${APP_USER}" "750"
  printf '  %-28s %s:%s  %s\n' "${APP_DIR}/data/"         "${APP_USER}" "${APP_USER}" "750"
  printf '  %-28s %s:%s  %s\n' "${CONF_DIR}/"             "root"        "root"        "755"
  printf '  %-28s %s:%s  %s\n' "${TOKEN_FILE}"            "${APP_USER}" "${APP_USER}" "600"
  printf '  %-28s %s:%s  %s\n' "${LOG_DIR}/"              "${APP_USER}" "${APP_USER}" "750"
  printf '  %-28s %s:%s  %s\n' "${WRAPPER}"               "root"        "root"        "755"
  printf '  %-28s %s:%s  %s\n' "${WEBUI_WRAPPER}"         "root"        "root"        "755"
  printf '  %-28s %s:%s  %s\n' "${WEBUI_INITD}"           "root"        "root"        "755"
  printf '  %-28s %s:%s  %s\n' "${CRON_FILE}"             "root"        "root"        "600/644"
  printf '\n'
  printf '  Cron line:\n'
  case "${CRON_STYLE}" in
    crontabs) printf '    %s %s\n' "${CRON_SCHEDULE}" "${MAIN_CRON_CMD}" ;;
    crond)    printf '    %s %s %s\n' "${CRON_SCHEDULE}" "${APP_USER}" "${MAIN_CRON_CMD}" ;;
  esac
  printf '\n'
  printf '  Scan now:    %s\n' "${WRAPPER}"
  printf '  Web UI:      http://127.0.0.1:8765  (rc-service %s status)\n' "${WEBUI_SERVICE}"
  printf '  Watch logs:  tail -f %s/cron.log\n' "${LOG_DIR}"

  if [ "${_token_created}" -eq 1 ]; then
    printf '\n\033[1;33m▶ ACTION REQUIRED — fill in the ntfy auth token:\033[0m\n'
    printf '    echo "your-ntfy-token" > %s\n' "${TOKEN_FILE}"
    printf '    chmod 600 %s\n' "${TOKEN_FILE}"
  fi

  if [ -z "${NTFY_TOPIC}" ]; then
    printf '\n\033[1;33m▶ ntfy topic not configured — edit NTFY_TOPIC at the top of this script\033[0m\n'
    printf '  then re-run install.sh, or manually add to %s:\n' "${CRON_FILE}"
    printf '    --ntfy-topic YOUR_TOPIC --ntfy-server %s\n' "${NTFY_SERVER}"
  fi
  printf '\n'
}

# ══════════════════════════════════════════════════════════════════════
# UNINSTALL
# ══════════════════════════════════════════════════════════════════════
uninstall() {
  need_root
  detect_env

  printf '\nThis will remove the %s installation.\n' "${APP_NAME}"
  confirm "Continue with uninstall?" || { echo "Aborted."; exit 0; }

  # Remove cron entry
  step "Removing cron entry: ${CRON_FILE:-none}"
  if [ -n "${CRON_FILE}" ] && [ -f "${CRON_FILE}" ]; then
    rm -f "${CRON_FILE}"
    ok "Removed."
  else
    ok "Cron file not found — skipping."
  fi

  # Data — ask before destroying
  step "Application data: ${APP_DIR}/data/"
  _keep_data=1
  if [ -d "${APP_DIR}/data" ]; then
    if confirm "DELETE application data (seen_urls.json, matches.json)?"; then
      _keep_data=0
    else
      ok "Data will be preserved."
    fi
  fi

  # Remove app directory (preserving data if requested)
  step "Removing application files: ${APP_DIR}"
  if [ -d "${APP_DIR}" ]; then
    if [ "${_keep_data}" -eq 0 ]; then
      rm -rf "${APP_DIR}"
      ok "Removed ${APP_DIR}"
    else
      # Remove everything except data/
      find "${APP_DIR}" -mindepth 1 -maxdepth 1 ! -name 'data' -exec rm -rf {} +
      ok "Removed code; preserved ${APP_DIR}/data/"
    fi
  else
    ok "Not found — skipping."
  fi

  # Web UI service
  step "Stopping and removing OpenRC service '${WEBUI_SERVICE}'"
  if [ -f "${WEBUI_INITD}" ]; then
    rc-service "${WEBUI_SERVICE}" stop 2>/dev/null || true
    rc-update del "${WEBUI_SERVICE}" 2>/dev/null || true
    rm -f "${WEBUI_INITD}"
    ok "Service removed."
  else
    ok "Init script not found — skipping."
  fi

  # Wrappers
  step "Removing wrappers"
  rm -f "${WRAPPER}" "${WEBUI_WRAPPER}"
  ok "Done."

  # Logs
  step "Log directory: ${LOG_DIR}"
  if [ -d "${LOG_DIR}" ]; then
    if confirm "DELETE logs (${LOG_DIR})?"; then
      rm -rf "${LOG_DIR}"
      ok "Deleted."
    else
      ok "Preserved at ${LOG_DIR}"
    fi
  else
    ok "Not found — skipping."
  fi
  rm -f "/etc/logrotate.d/${APP_NAME}" 2>/dev/null || true
  rm -f "${LOCAL_START}" 2>/dev/null || true

  # Config / token
  step "Config directory: ${CONF_DIR}"
  if [ -d "${CONF_DIR}" ]; then
    if confirm "DELETE config and ntfy token (${CONF_DIR})?"; then
      rm -rf "${CONF_DIR}"
      ok "Deleted."
    else
      ok "Preserved at ${CONF_DIR}"
    fi
  else
    ok "Not found — skipping."
  fi

  # System user and group
  step "Removing system user '${APP_USER}'"
  if id "${APP_USER}" >/dev/null 2>&1; then
    case "${PKG_MGR}" in
      apk) deluser "${APP_USER}" ;;
      *)   userdel "${APP_USER}" ;;
    esac
    ok "User removed."
  else
    ok "User not found — skipping."
  fi

  step "Removing system group '${APP_USER}'"
  if grep -q "^${APP_USER}:" /etc/group 2>/dev/null; then
    case "${PKG_MGR}" in
      apk) delgroup "${APP_USER}" ;;
      *)   groupdel "${APP_USER}" ;;
    esac
    ok "Group removed."
  else
    ok "Group not found — skipping."
  fi

  printf '\n\033[1;32mUninstall complete.\033[0m\n\n'
}

# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
usage() {
  cat <<EOF
Usage: $(basename "$0") [COMMAND]

Install or remove ${APP_NAME} as a cron-driven service (OpenRC).
Must be run as root.

Commands:
  install      Install the application (default when no command given)
  --uninstall  Remove the application interactively

What install does:
  1. Installs system packages (python3) if missing
  2. Creates system group and user '${APP_USER}' (no home, no shell)
  3. Copies source files to ${APP_DIR}/
  4. Creates Python virtualenv at ${APP_DIR}/venv/
     (uses uv if available, otherwise python3 -m venv + pip)
  5. Creates log directory ${LOG_DIR}/
  6. Creates config directory ${CONF_DIR}/ with empty ntfy token file
  7. Writes wrapper script to ${WRAPPER}
  8. Configures logrotate (or a weekly cron trim if logrotate is absent)
  9. Installs a cron job running every ${CRON_SCHEDULE}
     with flock to prevent overlapping runs
 10. Runs an import smoke test and one live scrape as '${APP_USER}'
 11. Checks that crond is running

Configuration (edit at the top of this script before running):
  APP_NAME        application name and directory prefix  [${APP_NAME}]
  APP_USER        system user / group to run as          [${APP_USER}]
  CRON_SCHEDULE   cron time expression                   [${CRON_SCHEDULE}]
  NTFY_TOPIC      ntfy topic for push notifications      [${NTFY_TOPIC:-<unset>}]
  NTFY_SERVER     ntfy server base URL                   [${NTFY_SERVER}]
  PYTHON_DEPS     pip/uv dependencies (space-separated)  [${PYTHON_DEPS}]

Key paths:
  ${APP_DIR}/          application code and virtualenv
  ${APP_DIR}/data/     runtime data (seen_urls.json, matches.json)
  ${LOG_DIR}/          cron.log
  ${CONF_DIR}/ntfy-token   ntfy auth token (chmod 600, fill in manually)
  ${WRAPPER}        run a scrape manually
  ${WEBUI_WRAPPER}     open the match browser UI

After install:
  Scan now:    ${WRAPPER}
  Web UI:      ${WEBUI_WRAPPER}
  Watch logs:  tail -f ${LOG_DIR}/cron.log
EOF
}

case "${1:-install}" in
  install|--install)         install   ;;
  uninstall|--uninstall)     uninstall ;;
  help|-h|--help)            usage     ;;
  *)
    printf 'Unknown command: %s\n' "$1" >&2
    usage >&2
    exit 1
    ;;
esac
