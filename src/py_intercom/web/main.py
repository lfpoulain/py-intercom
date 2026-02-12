import argparse
import os
import ssl

from loguru import logger

from ..common.logging import setup_logging
from .app import create_app


def _build_san_list():
    """Build SAN list with localhost + all local IPv4 addresses."""
    import ipaddress
    import socket
    from cryptography import x509

    names = [x509.DNSName("localhost"), x509.DNSName("*.local")]
    seen_ips = set()
    seen_ips.add("127.0.0.1")
    names.append(x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")))

    # Discover all local IPs
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in seen_ips:
                seen_ips.add(ip)
                names.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
    except Exception:
        pass

    # Fallback: connect trick to find default LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in seen_ips:
            seen_ips.add(ip)
            names.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
    except Exception:
        pass

    return names


def _ensure_self_signed_cert(cert_dir: str) -> tuple[str, str]:
    """Generate a self-signed certificate if it doesn't exist yet."""
    cert_path = os.path.join(cert_dir, "web_cert.pem")
    key_path = os.path.join(cert_dir, "web_key.pem")

    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "py-intercom-web")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(_build_san_list()), critical=False)
            .sign(key, hashes.SHA256())
        )

        os.makedirs(cert_dir, exist_ok=True)

        with open(key_path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("generated self-signed certificate in {}", cert_dir)
        return cert_path, key_path

    except ImportError:
        logger.warning("cryptography package not installed — cannot generate HTTPS cert, falling back to HTTP")
        raise
    except Exception as e:
        logger.warning("failed to generate self-signed cert: {} — falling back to HTTP", e)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-ssl", action="store_true", help="disable HTTPS (getUserMedia won't work on remote devices)")

    args = parser.parse_args()

    setup_logging(bool(args.debug))

    app, socketio = create_app()

    ssl_ctx = None
    scheme = "http"
    if not args.no_ssl:
        cert_dir = os.path.join(os.path.expanduser("~"), "py-intercom")
        try:
            cert_path, key_path = _ensure_self_signed_cert(cert_dir)
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert_path, key_path)
            scheme = "https"
        except Exception:
            logger.warning("HTTPS disabled — mobile mic will NOT work over LAN")

    logger.info("starting web client server on {}://{}:{}", scheme, args.host, args.port)

    socketio.run(app, host=args.host, port=int(args.port), debug=bool(args.debug), allow_unsafe_werkzeug=True, ssl_context=ssl_ctx)
    return 0
