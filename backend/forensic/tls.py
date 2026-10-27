"""HTTPS for a workbench that examiners reach over the lab network, with no internet needed.

The first time the workbench serves the lab network it creates a small certificate authority
for this machine, and a server certificate from it naming the machine's host names and
addresses. Examiner PCs trust the workbench by installing the authority's certificate
(`workbench-ca.crt`) once; the server certificate is re-issued automatically when the
machine's names or addresses change. A lab that has its own certificate can use that instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_YEARS = 10
SERVER_DAYS = 825  # the longest validity browsers accept for a server certificate


def machine_names() -> tuple[list[str], list[str]]:
    """Host names and IP addresses this computer is reached by: (names, addresses)."""
    names = {"localhost"}
    for n in (socket.gethostname(), socket.getfqdn()):
        if n:
            names.add(n.lower())
    addrs = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0].split("%")[0])
    except OSError:
        pass
    # On Linux the host name usually resolves only to 127.0.1.1 (Debian and Ubuntu put that in
    # /etc/hosts), so the addresses examiners actually type come from the network interfaces.
    addrs.update(interface_addresses())
    return sorted(names), sorted(addrs)


def interface_addresses() -> set[str]:
    """IPv4 and global IPv6 addresses of this computer's network interfaces (Linux only).

    Read from the kernel, with no network traffic. Loopback and link-local addresses are left
    out: browsers on other computers cannot use them.
    """
    found: set[str] = set()
    if not hasattr(socket, "if_nameindex"):
        return found
    try:
        import fcntl
        import struct
    except ImportError:  # Windows
        return found
    try:
        interfaces = [name for _, name in socket.if_nameindex()]
    except OSError:
        return found
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for name in interfaces:
            try:
                # SIOCGIFADDR: the interface's IPv4 address.
                raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                found.add(socket.inet_ntoa(raw[20:24]))
            except OSError:
                continue
    try:
        for line in Path("/proc/net/if_inet6").read_text().splitlines():
            h = line.split()[0]
            found.add(str(ipaddress.ip_address(int(h, 16))))
    except (OSError, ValueError, IndexError):
        pass
    keep = set()
    for a in found:
        ip = ipaddress.ip_address(a)
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
            keep.add(a)
    return keep


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


def _name(cn: str) -> x509.Name:
    # The common name is only a label (browsers match the alternative names), and X.509 caps it
    # at 64 characters; a long domain name would otherwise stop the certificate being made.
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lucida"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn[:64])])


def fingerprint(cert_path: Path) -> str:
    der = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def ensure_certificate(folder: Path, names: list[str] | None = None, addresses: list[str] | None = None) -> dict:
    """Create or refresh the local authority and server certificate in `folder`.

    Returns the paths of the server certificate and key, the authority certificate examiners
    install, and the authority's SHA-256 fingerprint for them to compare.
    """
    if names is None or addresses is None:
        found_names, found_addrs = machine_names()
        names = names if names is not None else found_names
        addresses = addresses if addresses is not None else found_addrs
    folder.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key = folder / "workbench-ca.crt", folder / "workbench-ca.key"
    crt, key, meta = folder / "workbench.crt", folder / "workbench.key", folder / "workbench.json"
    now = dt.datetime.now(dt.timezone.utc)

    if not (ca_crt.exists() and ca_key.exists()):
        k = ec.generate_private_key(ec.SECP256R1())
        subject = _name(f"Lucida local authority ({socket.gethostname()})")
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(k.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=365 * CA_YEARS))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                             crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
                .sign(k, hashes.SHA256()))
        _write_key(ca_key, k)
        ca_crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        crt.unlink(missing_ok=True)

    wanted = {"names": sorted(set(names)), "addresses": sorted(set(addresses))}
    current = json.loads(meta.read_text()) if meta.exists() else None
    expires = None
    if crt.exists():
        expires = x509.load_pem_x509_certificate(crt.read_bytes()).not_valid_after_utc
    if not (crt.exists() and key.exists()) or current != wanted or expires - now < dt.timedelta(days=30):
        authority = x509.load_pem_x509_certificate(ca_crt.read_bytes())
        signer = serialization.load_pem_private_key(ca_key.read_bytes(), None)
        k = ec.generate_private_key(ec.SECP256R1())
        alt = [x509.DNSName(n) for n in wanted["names"]] + [
            x509.IPAddress(ipaddress.ip_address(a)) for a in wanted["addresses"]]
        cert = (x509.CertificateBuilder().subject_name(_name(wanted["names"][-1])).issuer_name(authority.subject)
                .public_key(k.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
                .add_extension(x509.SubjectAlternativeName(alt), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
                .sign(signer, hashes.SHA256()))
        _write_key(key, k)
        # The chain lets browsers that were given only the authority build the path.
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca_crt.read_bytes())
        meta.write_text(json.dumps(wanted))
    return {"cert": crt, "key": key, "authority": ca_crt, "authority_sha256": fingerprint(ca_crt),
            "names": wanted["names"], "addresses": wanted["addresses"]}
