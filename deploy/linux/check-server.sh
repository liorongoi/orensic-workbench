#!/usr/bin/env bash
# Check that this server is ready for Lucida. Changes nothing; safe to run at any time.
#
#   ./check-server.sh [--port 8443] [--data /var/lib/lucida]
#
# Each line is OK, WARN (Lucida works, but not as well) or FAIL (fix it first).
# Exit code: 0 all OK, 1 warnings only, 2 at least one FAIL.
set -uo pipefail
PORT=8443; DATA=/var/lib/lucida; installing=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --installing) installing=1; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) shift ;;
  esac
done
worst=0
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; [ $worst -lt 1 ] && worst=1; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; worst=2; }
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# Operating system
if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:26.04) ok "operating system: $PRETTY_NAME (Ubuntu family)" ;;
    rhel:9*|rhel:10*|rocky:9*|rocky:10*|almalinux:9*|almalinux:10*|ol:9*|ol:10*|centos:9|centos:10)
      ok "operating system: $PRETTY_NAME (Red Hat family)" ;;
    *) warn "operating system: ${PRETTY_NAME:-unknown} (tested on Ubuntu 22.04/24.04 and Red Hat Enterprise Linux 9; other systemd distributions usually work)" ;;
  esac
fi
[ "$(uname -m)" = x86_64 ] && ok "processor architecture: x86_64" || fail "processor architecture: $(uname -m) (this package is for x86_64)"
glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
if [ -n "$glibc" ] && ver_ge "$glibc" 2.34; then ok "C library: glibc $glibc"
else fail "C library: glibc ${glibc:-unknown}; 2.34 or newer is needed (Ubuntu 22.04 or newer, Red Hat 9 or newer; Red Hat 8 is too old)"; fi
command -v systemctl >/dev/null && [ -d /run/systemd/system ] && ok "systemd is running" || fail "systemd is not running (the lucida service needs it)"

# Memory, processor, disk
mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
if [ $mem_gb -ge 60 ]; then ok "memory: ${mem_gb} GB"
elif [ $mem_gb -ge 30 ]; then warn "memory: ${mem_gb} GB (64 GB recommended; long video ranges are refused sooner)"
else warn "memory: ${mem_gb} GB (64 GB recommended; under 32 GB only small cases are practical)"; fi
ok "processor: $(nproc) threads ($(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//'))"
probe="$DATA"; while [ ! -d "$probe" ]; do probe="$(dirname "$probe")"; done
free_gb=$(( $(df -Pk "$probe" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if [ $free_gb -ge 200 ]; then ok "free disk for case data ($probe): ${free_gb} GB"
elif [ $free_gb -ge 20 ]; then warn "free disk for case data ($probe): ${free_gb} GB (evidence and derived video need room; 500 GB or more is comfortable)"
else fail "free disk for case data ($probe): ${free_gb} GB (at least 20 GB is needed)"; fi
opt_free=$(( $(df -Pk /opt | awk 'NR==2 {print $4}') / 1024 / 1024 ))
[ $opt_free -ge 8 ] && ok "free disk for programs (/opt): ${opt_free} GB" || fail "free disk for programs (/opt): ${opt_free} GB (8 GB is needed)"

# NVIDIA graphics card and driver
if command -v lspci >/dev/null; then
  card="$(lspci -nn | grep -i 'nvidia' | grep -Ei 'vga|3d|display' | head -1 | sed 's/^[^ ]* //')"
  [ -n "$card" ] && ok "graphics card on the PCI bus: $card" || warn "no NVIDIA card found on the PCI bus (models will run on the CPU)"
else
  warn "lspci is not installed, so the PCI bus was not checked (Ubuntu: sudo apt install pciutils; Red Hat: sudo dnf install pciutils)"
fi
if lsmod 2>/dev/null | grep -q '^nouveau'; then
  fail "the open-source nouveau driver is loaded; install the NVIDIA driver (guide, step 2), which disables it"
fi
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  name="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  mem="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)"
  cuda="$(nvidia-smi | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)"
  ok "NVIDIA driver $drv, supports CUDA ${cuda:-?}; card $name ($mem)"
  if ver_ge "$drv" 550; then :
  elif ver_ge "$drv" 525; then warn "driver $drv works, but 550 or newer is recommended for the L40S"
  else fail "driver $drv is too old: CUDA 12 needs 525 or newer (guide, step 2)"; fi
  case "$name" in *L40S*) ok "the card is an L40S, as this package is tuned for" ;; esac
  mig="$(nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader 2>/dev/null | head -1)"
  [ "$mig" = Enabled ] && warn "MIG mode is on; Lucida uses the first GPU instance only"
  pm="$(nvidia-smi --query-gpu=persistence_mode --format=csv,noheader | head -1)"
  [ "$pm" = Enabled ] && ok "persistence mode is on" || warn "persistence mode is off (the first model load after idle is slower): sudo systemctl enable --now nvidia-persistenced"
elif command -v nvidia-smi >/dev/null; then
  fail "nvidia-smi is installed but cannot talk to the driver (reboot after installing the driver; if Secure Boot is on, enrol the driver key; guide, troubleshooting)"
else
  warn "no NVIDIA driver (nvidia-smi not found): Lucida works, but models run on the CPU (guide, step 2)"
fi
if command -v getenforce >/dev/null; then
  se="$(getenforce 2>/dev/null)"
  [ -n "$se" ] && ok "SELinux: $se (the installer labels Lucida's folders; the service runs as a normal system service)"
fi
if command -v mokutil >/dev/null; then
  sb="$(mokutil --sb-state 2>/dev/null | head -1)"; [ -n "$sb" ] && ok "Secure Boot: $sb"
fi

# Network
if command -v ss >/dev/null; then
  if ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
    who="$(ss -Hltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*users:((\"\([^\"]*\)\".*/\1/p' | head -1)"
    if case "$who" in python3*) true ;; *) false ;; esac && systemctl is-active --quiet lucida 2>/dev/null; then ok "port $PORT is used by Lucida (an upgrade stops it first)"
    else fail "port $PORT is already used by ${who:-another program}; choose another with --port"; fi
  else
    ok "port $PORT is free"
  fi
fi
addrs="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':' | grep . | tr '\n' ' ')"
[ -n "$addrs" ] && ok "network addresses: $addrs" || warn "no IPv4 address found; browsers on other PCs need one to reach Lucida"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw status | grep -qE "^$PORT/tcp +ALLOW" && ok "ufw allows $PORT/tcp" || ok "ufw is active; the installer opens $PORT/tcp"
fi
command -v timedatectl >/dev/null && { timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes && ok "the clock is synchronised (audit-log times are right)" || warn "the clock is not synchronised; audit-log and report times come from this clock"; }

case $worst in 0) echo "  All checks passed." ;; 1) echo "  Checks passed with warnings." ;; *) echo "  At least one check FAILED." ;; esac
exit $worst
