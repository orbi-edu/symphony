"""A private CA and a server certificate for the mock's HTTPS port, written to the state dir
(`ca.pem`, `server.pem`, `server-key.pem`) and reused on the next start, so clients that insist
on TLS (`gh` against a GitHub Enterprise host) trust the mock with `SSL_CERT_FILE=<state>/ca.pem`
or `git -c http.sslCAInfo=<state>/ca.pem`."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl


def ensure(state_dir: str, hosts: list[str]) -> tuple[str, str, str]:
    ca_path = os.path.join(state_dir, "ca.pem")
    cert_path = os.path.join(state_dir, "server.pem")
    key_path = os.path.join(state_dir, "server-key.pem")
    names_path = os.path.join(state_dir, "server-hosts.txt")
    wanted = ",".join(sorted(set(hosts)))
    if all(os.path.exists(p) for p in (ca_path, cert_path, key_path, names_path)):
        with open(names_path) as fh:
            if fh.read().strip() == wanted:
                return ca_path, cert_path, key_path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    if os.path.exists(ca_path) and os.path.exists(ca_path + ".key"):
        with open(ca_path, "rb") as fh:
            ca_cert = x509.load_pem_x509_certificate(fh.read())
        with open(ca_path + ".key", "rb") as fh:
            ca_key = serialization.load_pem_private_key(fh.read(), None)
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Symphony GitHub mock CA")])
        ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
                   .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
        with open(ca_path, "wb") as fh:
            fh.write(ca_cert.public_bytes(serialization.Encoding.PEM))
        with open(ca_path + ".key", "wb") as fh:
            fh.write(ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        os.chmod(ca_path + ".key", 0o600)
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list = []
    for host in sorted(set(hosts)):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            sans.append(x509.DNSName(host))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
        fh.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    with open(names_path, "w") as fh:
        fh.write(wanted)
    return ca_path, cert_path, key_path


def context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx
