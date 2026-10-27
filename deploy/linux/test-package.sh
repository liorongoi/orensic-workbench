#!/usr/bin/env bash
# Install the Linux server package on a fresh machine and check it end to end (used by CI):
# install as a service, reach it over HTTPS on the LAN address with the CA, load it in headless
# Chrome, run the self-test with no network, upgrade in place, uninstall keeping the data,
# reinstall, uninstall with --purge.
#
#   sudo deploy/linux/test-package.sh dist/Lucida-<version>-linux-x86_64.tar.gz
set -euo pipefail
tarball="$(readlink -f "${1:?usage: test-package.sh <package.tar.gz>}")"
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
work="$(mktemp -d)"
step() { printf '\n== %s\n' "$*"; }
fail() { echo "TEST FAILED: $*" >&2; journalctl -u lucida -n 60 --no-pager >&2 || true; exit 1; }

step "Check the download against its .sha256"
(cd "$(dirname "$tarball")" && sha256sum -c "$(basename "$tarball").sha256")
tar -C "$work" -xzf "$tarball"
pkg="$(echo "$work"/Lucida-*-linux-x86_64)"

step "check-server.sh runs and reports"
"$pkg/check-server.sh" || [ $? -lt 2 ] || fail "check-server.sh reported FAIL"

step "Install"
"$pkg/install.sh" --yes
systemctl is-active --quiet lucida || fail "service not active"
systemctl is-enabled --quiet lucida || fail "service not enabled"
[ "$(stat -c %U /var/lib/lucida)" = lucida ] || fail "data folder not owned by lucida"
[ "$(stat -c %a /var/lib/lucida)" = 750 ] || fail "data folder mode is not 750"
grep -q '^auth = accounts' /etc/lucida/workbench.ini || fail "sign-in not on by default"
pid="$(systemctl show -p MainPID --value lucida)"
[ "$(ps -o user= -p "$pid")" = lucida ] || fail "service does not run as lucida"

step "Reach Lucida over HTTPS on the LAN address, verifying the certificate"
ca=/var/lib/lucida/tls/workbench-ca.crt
ip="$(hostname -I | tr ' ' '\n' | grep -v ':' | grep -v '^127\.' | head -1)"
[ -n "$ip" ] || fail "no LAN address"
port="$(sed -n 's/^port *= *//p' /etc/lucida/workbench.ini)"
curl -fsS --cacert "$ca" "https://$ip:$port/api/health" | tee /dev/stderr | grep -q . || fail "health over the LAN address"
curl -fsS --cacert "$ca" "https://$(hostname):$port/api/health" >/dev/null || fail "health over the host name"
curl -fsS --cacert "$ca" "https://$ip:$port/" | grep -q '<title>Lucida</title>' || fail "UI not served"
curl -fsS --cacert "$ca" "https://$ip:$port/api/auth/status" | tee /dev/stderr | grep -q . || fail "auth status"
code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$ca" "https://$ip:$port/api/cases")"
[ "$code" = 401 ] || [ "$code" = 403 ] || fail "cases answered $code without sign-in"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://$ip:$port/api/health" || true)"
[ "$code" != 200 ] || fail "plain HTTP must not work on the lab network"

step "Open it in headless Chrome, trusting only Lucida's authority"
chrome="$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)"
if [ -n "$chrome" ]; then
  command -v certutil >/dev/null || (apt-get update -qq && apt-get install -y -qq libnss3-tools >/dev/null)
  chome="$work/chrome-home" && mkdir -p "$chome/.pki/nssdb"
  certutil -d "sql:$chome/.pki/nssdb" -N --empty-password
  certutil -d "sql:$chome/.pki/nssdb" -A -t "C,," -n lucida-ca -i "$ca"
  dom="$(HOME="$chome" timeout 90 "$chrome" --headless=new --no-sandbox --disable-gpu --user-data-dir="$chome/profile" \
          --virtual-time-budget=15000 --dump-dom "https://$ip:$port/" 2>"$work/chrome.err" || true)"
  echo "$dom" | head -c 600; echo
  echo "$dom" | grep -q '<title>Lucida</title>' || { cat "$work/chrome.err" >&2; fail "Chrome did not load Lucida"; }
  echo "$dom" | grep -qiE 'administrator|sign in|user name|password' || { cat "$work/chrome.err" >&2; fail "Chrome loaded the page but the app did not render"; }
else
  echo "no Chrome on this machine; skipped"
fi

step "Self-test and GPU report as the service account, with no network access"
if [ -z "${TEST_NO_IPDENY:-}" ]; then
  systemd-run --wait --pipe --quiet -p User=lucida -p IPAddressDeny=any -p IPAddressAllow=localhost \
    -E LUCIDA_CONFIG=/etc/lucida/workbench.ini /opt/lucida/bin/lucida selftest || fail "offline self-test"
else
  # In a container systemd cannot filter traffic (no BPF); the self-test still runs as lucida.
  lucida selftest || fail "self-test"
fi
/opt/lucida/bin/lucida gpu || echo "(no GPU on this machine: models on the CPU, as expected)"
lucida version
lucida verify-install || fail "installed files changed after running"

step "Restart keeps working, installed files unchanged"
systemctl restart lucida
for _ in $(seq 1 60); do curl -fsS --cacert "$ca" "https://$ip:$port/api/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS --cacert "$ca" "https://$ip:$port/api/health" >/dev/null || fail "not back after restart"
lucida verify-install || fail "files changed"

step "Upgrade in place keeps settings and data"
echo "; test marker" >> /etc/lucida/workbench.ini
touch /var/lib/lucida/keep-me
fp_before="$(sha256sum "$ca" | cut -d' ' -f1)"
"$pkg/install.sh" --yes --skip-selftest
grep -q '; test marker' /etc/lucida/workbench.ini || fail "settings lost on upgrade"
[ -f /var/lib/lucida/keep-me ] || fail "data lost on upgrade"
[ "$(sha256sum "$ca" | cut -d' ' -f1)" = "$fp_before" ] || fail "certificate authority changed on upgrade"

step "Uninstall keeps the case data"
/opt/lucida/uninstall.sh
[ ! -e /opt/lucida ] || fail "/opt/lucida still there"
[ ! -e /etc/systemd/system/lucida.service ] || fail "unit still there"
[ ! -e /usr/local/bin/lucida ] || fail "command still there"
[ -f /var/lib/lucida/keep-me ] || fail "data removed without --purge"
[ -f /etc/lucida/workbench.ini ] || fail "settings removed without --purge"

step "Reinstall, then uninstall with --purge"
"$pkg/install.sh" --yes --skip-selftest
[ -f /var/lib/lucida/keep-me ] || fail "reinstall lost data"
/opt/lucida/uninstall.sh --purge --yes
[ ! -e /var/lib/lucida ] || fail "data still there after --purge"
[ ! -e /etc/lucida ] || fail "settings still there after --purge"
! id lucida >/dev/null 2>&1 || fail "lucida account still there"

rm -rf "$work"
echo
echo "All package tests passed."
