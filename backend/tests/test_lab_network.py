"""Lab-network access: the local certificate authority and the MCP bearer token."""

import socket
import ssl
import threading

import pytest
from cryptography import x509
from fastapi.testclient import TestClient

from forensic import mcp_server
from forensic.tls import ensure_certificate


def test_certificate_names_the_machine_and_verifies(tmp_path):
    t = ensure_certificate(tmp_path, ["localhost", "workbench.lab.example"], ["127.0.0.1", "10.0.0.5"])
    cert = x509.load_pem_x509_certificate(t["cert"].read_bytes())
    alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(alt.get_values_for_type(x509.DNSName)) == {"localhost", "workbench.lab.example"}
    assert {str(a) for a in alt.get_values_for_type(x509.IPAddress)} == {"127.0.0.1", "10.0.0.5"}
    assert len(t["authority_sha256"].split(":")) == 32

    # A TLS client that trusts only the authority accepts the server certificate.
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(t["cert"], t["key"])
    client_ctx = ssl.create_default_context(cafile=str(t["authority"]))
    with socket.socket() as ls:
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)

        def serve():
            conn, _ = ls.accept()
            with server_ctx.wrap_socket(conn, server_side=True) as s:
                s.recv(1)

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        with socket.create_connection(ls.getsockname()) as raw, client_ctx.wrap_socket(raw, server_hostname="localhost") as c:
            assert c.version()
            c.send(b"x")
        th.join(5)


def test_certificate_is_kept_until_the_names_change(tmp_path):
    first = ensure_certificate(tmp_path, ["localhost"], ["127.0.0.1"])
    pem, ca = first["cert"].read_bytes(), first["authority"].read_bytes()
    assert ensure_certificate(tmp_path, ["localhost"], ["127.0.0.1"])["cert"].read_bytes() == pem
    again = ensure_certificate(tmp_path, ["localhost", "new-name"], ["127.0.0.1"])
    assert again["cert"].read_bytes() != pem and again["authority"].read_bytes() == ca


def test_mcp_over_the_network_needs_a_token():
    srv = mcp_server.build("case")
    with pytest.raises(SystemExit, match="FORENSIC_MCP_TOKEN"):
        mcp_server.http_app(srv, "0.0.0.0", "")
    with pytest.raises(SystemExit):
        mcp_server.http_app(srv, "0.0.0.0", "short")
    token = "t" * 32
    with TestClient(mcp_server.http_app(srv, "0.0.0.0", token)) as c:
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": f"Bearer {token}"}).status_code != 401
    # On loopback a token stays optional.
    assert mcp_server.http_app(srv, "127.0.0.1", "")
