#!/usr/bin/env bash
# Install or upgrade Lucida on a Linux server (x86_64; Ubuntu 22.04 / 24.04 LTS, Red Hat
# Enterprise Linux 8 / 9 and its rebuilds, and other systemd distributions). Run from the unpacked package, as root:
#
#   sudo ./install.sh                      install with the defaults below and start the service
#   sudo ./install.sh --port 443           a different port
#   sudo ./install.sh --local-only         serve only this computer (http://127.0.0.1), no network
#   sudo ./install.sh --no-sign-in         no accounts (anyone who can reach the port can use it)
#   sudo ./install.sh --help               every option
#
# Defaults: programs in /opt/lucida, settings in /etc/lucida/workbench.ini, case data in
# /var/lib/lucida, HTTPS on port 8443 for browsers on the network, sign-in with accounts, a
# "lucida" system service that starts at boot. Upgrading keeps the settings and the case data.
# Makes no internet connection.
set -euo pipefail

APP=/opt/lucida
DATA=/var/lib/lucida
IMPORT=""
PORT=8443
NETWORK=lab
AUTH=accounts
START=1
SELFTEST=1
FIREWALL=1
ASSUME_YES=0
HOSTS=""

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; cat <<USAGE

Options:
  --prefix DIR        programs folder (default $APP)
  --data DIR          case data folder (default $DATA); kept on upgrade and uninstall
  --import DIR        folder agents and batch jobs may read from (default <data>/import)
  --port N            port browsers connect to (default $PORT)
  --hostname NAMES    extra names or addresses for the certificate, comma-separated
                      (e.g. lucida.lab.local,10.0.0.5); the server's own are added automatically
  --local-only        listen on 127.0.0.1 only, over plain HTTP
  --no-sign-in        do not require accounts
  --no-firewall       do not open the port in ufw or firewalld
  --no-start          install, but do not enable or start the service
  --skip-selftest     do not run the acceptance self-test at the end
  -y, --yes           do not ask before continuing past warnings
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) APP="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --import) IMPORT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --hostname) HOSTS="$2"; shift 2 ;;
    --local-only) NETWORK=local; shift ;;
    --no-sign-in) AUTH=none; shift ;;
    --no-firewall) FIREWALL=0; shift ;;
    --no-start) START=0; shift ;;
    --skip-selftest) SELFTEST=0; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
IMPORT="${IMPORT:-$DATA/import}"
PKG="$(cd "$(dirname "$0")" && pwd)"
CONF_DIR=/etc/lucida
CONF="$CONF_DIR/workbench.ini"
UNIT=/etc/systemd/system/lucida.service

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mInstallation stopped:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it as root: sudo $0 $*"
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be between 1 and 65535"
for d in "$APP" "$DATA"; do
  case "$d" in /|/usr|/usr/*|/etc|/etc/*|/bin|/sbin|/lib*|/boot*|/proc*|/sys*|/dev*|/root|/home|/var|/opt|"") die "refusing to use $d" ;; /*) ;; *) die "$d must be an absolute path" ;; esac
done
VERSION="$(sed -n '1s/^Lucida \([^ ]*\).*/\1/p' "$PKG/payload/BUILDINFO.txt")"

say "Lucida $VERSION: checking the package"
(cd "$PKG" && sha256sum --quiet -c PACKAGE.SHA256SUMS) || die "the package files are damaged (PACKAGE.SHA256SUMS); download it again"
(cd "$PKG/payload" && sha256sum --quiet -c SHA256SUMS) || die "the package files are damaged (payload/SHA256SUMS); download it again"
ok "every file matches its SHA-256"

say "Checking the server"
set +e
"$PKG/check-server.sh" --port "$PORT" --data "$DATA" --installing
check=$?
set -e
if [ $check -eq 2 ]; then
  die "a required check failed (marked FAIL above)"
elif [ $check -eq 1 ] && [ $ASSUME_YES = 0 ] && [ -t 0 ]; then
  read -r -p "  Some checks gave warnings (above). Continue anyway? [y/N] " a
  case "$a" in y|Y|yes|YES) ;; *) die "stopped at your request" ;; esac
fi

say "Service account"
if ! id lucida >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin \
    --comment "Lucida forensic workbench" lucida
  ok "created system user lucida"
else
  ok "system user lucida exists"
fi
for g in video render; do getent group $g >/dev/null && usermod -aG $g lucida; done
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && id "$SUDO_USER" >/dev/null 2>&1; then
  usermod -aG lucida "$SUDO_USER" && ok "added $SUDO_USER to group lucida (read access to logs; sign out and in again)"
fi

if systemctl is-active --quiet lucida 2>/dev/null; then
  say "Stopping the running Lucida for the upgrade"
  systemctl stop lucida
  ok "stopped"
fi

say "Programs in $APP"
if [ -e "$APP" ]; then
  [ -f "$APP/.lucida-install" ] || [ -z "$(ls -A "$APP")" ] || die "$APP exists and is not a Lucida installation; choose another --prefix"
  old="$(head -1 "$APP/BUILDINFO.txt" 2>/dev/null || true)"
  [ -n "$old" ] && ok "replacing: $old"
  # Clear the old runtime first, so no file from an older version is left behind.
  rm -rf "$APP"
fi
mkdir -p "$APP"
cp -a "$PKG/payload/." "$APP/"
touch "$APP/.lucida-install"
chown -R root:root "$APP"
chmod -R go-w "$APP"
chmod 0755 "$APP"
ln -sfn "$APP/bin/lucida" /usr/local/bin/lucida
ok "copied $(wc -l < "$APP/SHA256SUMS") files; command: lucida"

say "Settings in $CONF"
mkdir -p "$CONF_DIR"
if [ -f "$CONF" ]; then
  ok "kept your existing settings (upgrade)"
  for key in data_dir import_dir port; do
    sed -n "s/^$key *= *//p" "$CONF" | head -1 | grep -q . || warn "$key is not set in $CONF"
  done
  DATA="$(sed -n 's/^data_dir *= *//p' "$CONF" | head -1)"; DATA="${DATA:-/var/lib/lucida}"
  IMPORT="$(sed -n 's/^import_dir *= *//p' "$CONF" | head -1)"; IMPORT="${IMPORT:-$DATA/import}"
  PORT="$(sed -n 's/^port *= *//p' "$CONF" | head -1)"; PORT="${PORT:-8443}"
  NETWORK="$(sed -n 's/^network *= *//p' "$CONF" | head -1)"; NETWORK="${NETWORK:-local}"
else
  mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  threads=$(nproc)
  workers=$(( mem_gb / 16 )); [ $workers -lt 1 ] && workers=1
  half=$(( threads / 2 )); [ $half -lt 1 ] && half=1
  [ $workers -gt $half ] && workers=$half
  gpu_mb=0
  if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    gpu_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -dc 0-9)
  fi
  gpu_jobs=1; [ "${gpu_mb:-0}" -ge 40000 ] && gpu_jobs=2
  {
    echo "; Lucida settings. Restart after a change: sudo systemctl restart lucida"
    echo "; Every key is described in /opt/lucida/docs/deployment.md."
    echo "[workbench]"
    echo "data_dir = $DATA"
    echo "import_dir = $IMPORT"
    echo "port = $PORT"
    echo "; lab = browsers on other computers connect over HTTPS; local = this computer only"
    echo "network = $NETWORK"
    [ "$NETWORK" = local ] && echo "host = 127.0.0.1"
    echo "; accounts = sign-in with roles and per-case membership (the first visitor creates the administrator)"
    [ "$AUTH" = accounts ] && echo "auth = accounts" || echo "; auth = accounts"
    [ -n "$HOSTS" ] && echo "allowed_hosts = $HOSTS" || echo "; allowed_hosts = lucida.lab.local"
    echo "; Sized for this server: ${mem_gb} GB RAM, ${threads} threads, GPU memory ${gpu_mb} MB"
    echo "job_workers = $workers"
    echo "gpu_jobs = $gpu_jobs"
    echo "ai_device = auto"
  } > "$CONF"
  ok "wrote $CONF (job_workers = $workers, gpu_jobs = $gpu_jobs)"
fi
chown root:lucida "$CONF" && chmod 0640 "$CONF"

say "Case data in $DATA"
mkdir -p "$DATA" "$IMPORT" "$DATA/logs"
chown lucida:lucida "$DATA" "$IMPORT" "$DATA/logs"
chmod 0750 "$DATA"
chmod 0770 "$IMPORT"
chmod 0750 "$DATA/logs"
ok "owned by lucida; only lucida and members of group lucida can open it"
ok "files to import can be copied to $IMPORT (group lucida may write there)"

if command -v selinuxenabled >/dev/null && selinuxenabled && command -v restorecon >/dev/null; then
  # Red Hat: give the copied files the labels their folders expect (programs, state, settings).
  restorecon -RF "$APP" "$DATA" "$CONF_DIR" 2>/dev/null || true
  [ "$IMPORT" != "$DATA/import" ] && restorecon -RF "$IMPORT" 2>/dev/null || true
  ok "SELinux labels restored on $APP, $DATA and $CONF_DIR"
fi

say "System service"
sed -e "s#@APP_DIR@#$APP#g" -e "s#@DATA_DIR@#$DATA#g" -e "s#@IMPORT_DIR@#$IMPORT#g" \
  "$APP/systemd/lucida.service" > "$UNIT"
command -v restorecon >/dev/null && restorecon -F "$UNIT" 2>/dev/null || true
systemctl daemon-reload
ok "installed $UNIT"
if command -v nvidia-modprobe >/dev/null; then nvidia-modprobe -u -c=0 2>/dev/null || true; fi

if [ "$NETWORK" = lab ] && [ $FIREWALL = 1 ]; then
  say "Firewall"
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" comment "Lucida" >/dev/null && ok "ufw: allowed $PORT/tcp"
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --quiet --permanent --add-port="$PORT/tcp" && firewall-cmd --quiet --reload && ok "firewalld: allowed $PORT/tcp"
  else
    ok "no active ufw or firewalld; nothing to open (check any network firewall between the PCs and this server)"
  fi
fi

if [ $START = 0 ]; then
  say "Installed, not started"
  echo "  Start it with: sudo systemctl enable --now lucida"
  exit 0
fi

say "Starting Lucida"
systemctl enable --now lucida >/dev/null 2>&1 || systemctl enable --now lucida
scheme=http; [ "$NETWORK" = lab ] && scheme=https
ca="$DATA/tls/workbench-ca.crt"
up=0
for _ in $(seq 1 120); do
  if ! systemctl is-active --quiet lucida && [ "$(systemctl show -p NRestarts --value lucida)" -gt 2 ]; then break; fi
  if "$APP/python/bin/python3" -I - "$scheme://127.0.0.1:$PORT/api/health" "$ca" <<'PY' 2>/dev/null
import os, ssl, sys, urllib.request
url, ca = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(cafile=ca) if url.startswith("https") else None
if ctx is not None and not os.path.exists(ca):
    sys.exit(1)
urllib.request.urlopen(url, timeout=2, context=ctx).read()
PY
  then up=1; break; fi
  sleep 1
done
if [ $up = 0 ]; then
  journalctl -u lucida -n 40 --no-pager || true
  die "Lucida did not answer on port $PORT (log above; also: journalctl -u lucida)"
fi
ok "the lucida service is running and answers on $scheme://127.0.0.1:$PORT"

if [ $SELFTEST = 1 ]; then
  say "Acceptance self-test (a few minutes)"
  if runuser -u lucida -- "$APP/bin/lucida" selftest; then ok "self-test passed (log in $DATA/logs)"; else warn "the self-test FAILED; see its output above and the guide's troubleshooting section"; fi
  say "Graphics card"
  runuser -u lucida -- "$APP/bin/lucida" gpu || warn "the models run on the CPU; see the guide's troubleshooting section"
fi

say "Done"
if [ "$NETWORK" = lab ]; then
  fp="$(runuser -u lucida -- "$APP/bin/lucida" ca | sed -n 's/^SHA-256 fingerprint: *//p')"
  copy=""
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    if [ -d "$home" ]; then
      cp "$ca" "$home/lucida-ca.crt" && chown "$SUDO_USER": "$home/lucida-ca.crt" && copy="$home/lucida-ca.crt"
    fi
  fi
  echo "  Open Lucida in Chrome on an examiner PC at:"
  for a in $(hostname -I 2>/dev/null); do case "$a" in *:*) ;; *) echo "      https://$a:$PORT" ;; esac; done
  echo "      https://$(hostname -f 2>/dev/null || hostname):$PORT"
  echo
  echo "  First install the certificate authority on each examiner PC (once):"
  echo "      $ca${copy:+  (copy: $copy)}"
  echo "      SHA-256 $fp"
  [ "$(sed -n 's/^auth *= *//p' "$CONF" | head -1)" = accounts ] && \
    echo "  The first person to open Lucida creates the administrator account."
else
  echo "  Open http://127.0.0.1:$PORT on this server."
fi
echo
echo "  Status: systemctl status lucida    Log: journalctl -u lucida -f    Settings: $CONF"
echo "  Uninstall: sudo $APP/uninstall.sh"
