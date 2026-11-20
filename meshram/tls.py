"""Self-signed identity per machine + certificate pinning (no CA, no hostnames: LAN IPs change)."""
import datetime
import hashlib
import os
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def ensure_identity(d):
    """Create <d>/node.crt + node.key once. Returns (cert_path, key_path)."""
    crt, key = os.path.join(d, "node.crt"), os.path.join(d, "node.key")
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    os.makedirs(d, exist_ok=True)
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"meshram-{socket.gethostname()}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(k, hashes.SHA256()))
    with open(key, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    with open(crt, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key


def fingerprint_pem(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def short_fp(fp: str) -> str:
    """Human-comparable form shown on both screens during pairing."""
    return "-".join(fp[i:i + 4].upper() for i in range(0, 16, 4))


def server_ctx(crt, key):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    c.load_cert_chain(crt, key)
    return c


def pinned_client_ctx(pem: str):
    """Trust exactly this one certificate. Hostname is ignored on purpose (DHCP LAN addresses)."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    c.check_hostname = False
    c.verify_mode = ssl.CERT_REQUIRED
    c.load_verify_locations(cadata=pem)
    return c


def insecure_probe_ctx():
    """Only used once, at pairing, to fetch the peer cert the human then confirms by fingerprint."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def fetch_peer_cert(host, port, timeout=5):
    with socket.create_connection((host, port), timeout=timeout) as s:
        with insecure_probe_ctx().wrap_socket(s) as t:
            return ssl.DER_cert_to_PEM_cert(t.getpeercert(binary_form=True))
