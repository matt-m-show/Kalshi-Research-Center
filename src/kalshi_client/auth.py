"""RSA-PSS request signing and credential storage.

Storage model (Windows-friendly):
  - PEM private key → file at ~/.kalshi/kalshi-{env}.pem
    File is created with owner-only ACL (icacls) so no other user can read it.
  - API Key ID (UUID) → Windows Credential Manager via keyring
    The UUID is short (~36 chars) and well within the 2.5KB CredWrite limit.

The raw private key NEVER enters an LLM prompt or appears in logs.

Setup (run once per environment):
  python -m src.kalshi_client.auth setup --env demo --key-id <UUID-from-Kalshi-UI>
  # Paste the PEM private key at the prompt.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import keyring
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYS_DIR = Path.home() / ".kalshi"
DEMO_SERVICE = "kalshi-agents-demo"
PROD_SERVICE = "kalshi-agents-prod"
_ENV = os.getenv("KALSHI_ENV", "demo").lower()


def _service_name() -> str:
    return PROD_SERVICE if _ENV == "prod" else DEMO_SERVICE


def _pem_path(env: str) -> Path:
    return KEYS_DIR / f"kalshi-{env}.pem"


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def _restrict_file_permissions(path: Path) -> None:
    """Set owner-only read permissions on the PEM file."""
    if sys.platform == "win32":
        # Remove inherited permissions, grant current user full control only
        try:
            username = os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
                check=True, capture_output=True,
            )
        except Exception:
            pass  # best-effort; file is still protected by its directory
    else:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def store_credentials(env: str, key_id: str, pem: str) -> None:
    """Persist credentials. Call once during setup.

    key_id: the UUID shown on the Kalshi API Keys page (stored in Credential Manager)
    pem:    the RSA private key PEM (stored as a restricted file in ~/.kalshi/)
    """
    # Store PEM as a file
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    pem_file = _pem_path(env)
    pem_file.write_text(pem.strip() + "\n", encoding="utf-8")
    _restrict_file_permissions(pem_file)

    # Store only the short key ID in the keychain
    service = PROD_SERVICE if env == "prod" else DEMO_SERVICE
    keyring.set_password(service, "key_id", key_id)

    print(f"PEM written to {pem_file}")
    print(f"Key ID stored in Windows Credential Manager under '{service}'")


def _load_private_key() -> tuple[str, "RSAPrivateKey"]:
    """Load (api_key_id, private_key).

    Resolution order (first match wins):
      1. KALSHI_KEY_ID + KALSHI_PEM_B64 environment variables  ← Docker / CI
      2. KALSHI_KEY_ID + ~/.kalshi/kalshi-{env}.pem file        ← mixed
      3. Windows Credential Manager + ~/.kalshi/kalshi-{env}.pem ← native setup
    """
    env = _ENV

    # --- env var path (Docker / CI) ---
    key_id_env = os.getenv("KALSHI_KEY_ID")
    pem_b64_env = os.getenv("KALSHI_PEM_B64")
    pem_path_env = os.getenv("KALSHI_PEM_PATH")

    if key_id_env:
        key_id = key_id_env
        if pem_b64_env:
            import base64 as _b64
            pem_bytes = _b64.b64decode(pem_b64_env)
        elif pem_path_env:
            pem_bytes = Path(pem_path_env).read_bytes()
        else:
            pem_file = _pem_path(env)
            if not pem_file.exists():
                raise RuntimeError(
                    f"KALSHI_KEY_ID is set but no PEM found. "
                    f"Set KALSHI_PEM_B64, KALSHI_PEM_PATH, or place the key at {pem_file}"
                )
            pem_bytes = pem_file.read_bytes()
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        return key_id, private_key  # type: ignore[return-value]

    # --- native path (Windows Credential Manager + file) ---
    service = _service_name()
    key_id = keyring.get_password(service, "key_id")
    if key_id is None:
        raise RuntimeError(
            f"No API Key ID found. Set KALSHI_KEY_ID env var, or run:\n"
            f"  python -m src.kalshi_client.auth setup --env {env} --key-id <UUID>"
        )

    pem_file = _pem_path(env)
    if not pem_file.exists():
        raise RuntimeError(
            f"Private key file not found: {pem_file}\n"
            "Run: python -m src.kalshi_client.auth setup"
        )
    pem_bytes = pem_file.read_bytes()
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    return key_id, private_key  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_request(method: str, path: str) -> dict[str, str]:
    """Return the three Kalshi auth headers for a given method + path.

    Signs: {timestamp_ms}{METHOD}{path_without_query}
    Headers:
        KALSHI-ACCESS-KEY       — UUID key ID
        KALSHI-ACCESS-TIMESTAMP — Unix ms timestamp
        KALSHI-ACCESS-SIGNATURE — base64(RSA-PSS signature)
    """
    api_key_id, private_key = _load_private_key()
    timestamp_ms = str(int(time.time() * 1000))

    # Strip query string before signing
    path_no_qs = path.split("?")[0]
    message = (timestamp_ms + method.upper() + path_no_qs).encode()

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    b64_sig = base64.b64encode(signature).decode()

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": b64_sig,
    }


# ---------------------------------------------------------------------------
# CLI setup helper
# ---------------------------------------------------------------------------


def _cli_setup() -> None:
    args = sys.argv[1:]
    if len(args) < 1 or args[0] != "setup":
        print("Usage: python -m src.kalshi_client.auth setup --env [demo|prod] --key-id <UUID>")
        sys.exit(1)

    env = args[args.index("--env") + 1] if "--env" in args else "demo"

    if "--key-id" not in args:
        print("Error: --key-id is required (the UUID from the Kalshi API Keys page)")
        sys.exit(1)
    key_id = args[args.index("--key-id") + 1]

    print(f"Paste your RSA private key PEM for '{env}' environment.")
    print("Paste all lines including -----BEGIN/END-----, then press Enter twice:")
    lines = []
    while True:
        line = input()
        if not line:
            break
        lines.append(line)
    pem = "\n".join(lines)

    # Validate it parses before storing
    serialization.load_pem_private_key(pem.encode(), password=None)

    store_credentials(env, key_id, pem)
    print(f"\nSetup complete for '{env}'. You can now run the system with KALSHI_ENV={env}.")


if __name__ == "__main__":
    _cli_setup()
