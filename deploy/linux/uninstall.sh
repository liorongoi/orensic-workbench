#!/usr/bin/env bash
# Remove Lucida from this server.
#
#   sudo /opt/lucida/uninstall.sh            remove the programs and the service; KEEP the case data and settings
#   sudo /opt/lucida/uninstall.sh --purge    also delete the case data, the settings and the lucida account
#                                            (asks twice; add --yes to skip the questions)
set -euo pipefail
APP="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF=/etc/lucida/workbench.ini
purge=0; yes=0
for a in "$@"; do
  case "$a" in
    --purge) purge=1 ;;
    -y|--yes) yes=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $a" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run it as root: sudo $0 $*" >&2; exit 1; }
[ -f "$APP/.lucida-install" ] || { echo "$APP is not a Lucida installation" >&2; exit 1; }

data=/var/lib/lucida; port=""
if [ -f "$CONF" ]; then
  d="$(sed -n 's/^data_dir *= *//p' "$CONF" | head -1)"; [ -n "$d" ] && data="$d"
  port="$(sed -n 's/^port *= *//p' "$CONF" | head -1)"
fi

if [ $purge = 1 ] && [ $yes = 0 ]; then
  echo "This deletes ALL case data in $data (evidence, derived files, audit log, accounts)."
  read -r -p "Type DELETE to confirm: " a; [ "$a" = DELETE ] || { echo "Nothing was removed."; exit 1; }
  read -r -p "Are you sure? Back it up first with: lucida backup --output <file> [y/N] " a
  case "$a" in y|Y|yes) ;; *) echo "Nothing was removed."; exit 1 ;; esac
fi

systemctl disable --now lucida >/dev/null 2>&1 || true
rm -f /etc/systemd/system/lucida.service
systemctl daemon-reload || true
echo "Removed the lucida service"
if [ -n "$port" ]; then
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw delete allow "$port/tcp" >/dev/null 2>&1 && echo "Removed the ufw rule for $port/tcp" || true
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --quiet --permanent --remove-port="$port/tcp" 2>/dev/null && firewall-cmd --quiet --reload && echo "Removed the firewalld rule for $port/tcp" || true
  fi
fi
[ "$(readlink -f /usr/local/bin/lucida 2>/dev/null)" = "$APP/bin/lucida" ] && rm -f /usr/local/bin/lucida
cd /
rm -rf "$APP"
echo "Removed $APP"

if [ $purge = 1 ]; then
  case "$data" in /|/home|/root|/var|/opt|/usr|/etc|"") echo "Refusing to delete $data; remove it yourself." >&2 ;;
    *) rm -rf "$data"; echo "Deleted the case data in $data" ;;
  esac
  rm -rf /etc/lucida && echo "Deleted /etc/lucida"
  if id lucida >/dev/null 2>&1; then userdel lucida 2>/dev/null || true; groupdel lucida 2>/dev/null || true; echo "Removed the lucida account"; fi
else
  echo "Kept the case data in $data and the settings in /etc/lucida (reinstalling picks them up again)."
fi
