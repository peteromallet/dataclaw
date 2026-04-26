"""Hugging Face token storage helpers."""

import os
from pathlib import Path

try:
    import keyring
except ImportError:
    keyring = None

try:
    from keyring.errors import PasswordDeleteError
except ImportError:
    PasswordDeleteError = Exception

KEYRING_SERVICE = "io.dataclaw.app"
KEYRING_ACCOUNT = "hf_token"
HF_STANDARD_TOKEN = Path.home() / ".cache" / "huggingface" / "token"


def _resolve_hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN")
    if token:
        return token

    if keyring is not None:
        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if token:
                return token
        except Exception:
            pass

    try:
        token = HF_STANDARD_TOKEN.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _store_hf_token(tok: str, *, mirror_to_hf_path: bool = True) -> None:
    if keyring is None:
        raise ImportError("keyring is not installed")

    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, tok)
    if mirror_to_hf_path:
        HF_STANDARD_TOKEN.parent.mkdir(parents=True, exist_ok=True)
        HF_STANDARD_TOKEN.write_text(tok, encoding="utf-8")
        os.chmod(HF_STANDARD_TOKEN, 0o600)


def _delete_hf_token(*, also_remove_hf_path: bool = True) -> None:
    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except (PasswordDeleteError, Exception):
            pass

    if also_remove_hf_path:
        try:
            HF_STANDARD_TOKEN.unlink()
        except FileNotFoundError:
            pass
