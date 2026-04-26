"""Tests for Hugging Face token storage helpers."""

import os

import pytest

from dataclaw import auth


class FakeKeyring:
    def __init__(self):
        self.store = {}
        self.raise_get = False
        self.deleted = []

    def get_password(self, service, account):
        if self.raise_get:
            raise RuntimeError("backend unavailable")
        return self.store.get((service, account))

    def set_password(self, service, account, token):
        self.store[(service, account)] = token

    def delete_password(self, service, account):
        self.deleted.append((service, account))
        self.store.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(auth, "keyring", fake)
    return fake


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "huggingface" / "token"
    monkeypatch.setattr(auth, "HF_STANDARD_TOKEN", path)
    return path


def test_resolve_hf_token_prefers_env_then_keyring_then_file(monkeypatch, fake_keyring, token_path):
    token_path.parent.mkdir(parents=True)
    token_path.write_text("hf_file\n", encoding="utf-8")
    fake_keyring.set_password(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT, "hf_keyring")

    monkeypatch.setenv("HF_TOKEN", "hf_env")
    assert auth._resolve_hf_token() == "hf_env"

    monkeypatch.delenv("HF_TOKEN")
    assert auth._resolve_hf_token() == "hf_keyring"

    fake_keyring.store.clear()
    assert auth._resolve_hf_token() == "hf_file"


def test_store_hf_token_writes_keyring_and_mirrors_with_chmod_600(fake_keyring, token_path):
    auth._store_hf_token("hf_store")

    assert fake_keyring.get_password(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT) == "hf_store"
    assert token_path.read_text(encoding="utf-8") == "hf_store"
    assert oct(token_path.stat().st_mode)[-3:] == "600"


def test_store_hf_token_no_mirror_skips_file_write(fake_keyring, token_path):
    auth._store_hf_token("hf_store", mirror_to_hf_path=False)

    assert fake_keyring.get_password(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT) == "hf_store"
    assert not token_path.exists()


def test_delete_hf_token_removes_both(fake_keyring, token_path):
    auth._store_hf_token("hf_store")

    auth._delete_hf_token()

    assert fake_keyring.get_password(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT) is None
    assert not token_path.exists()
    assert fake_keyring.deleted == [(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT)]


def test_delete_hf_token_no_mirror_keeps_file(fake_keyring, token_path):
    auth._store_hf_token("hf_store")

    auth._delete_hf_token(also_remove_hf_path=False)

    assert fake_keyring.get_password(auth.KEYRING_SERVICE, auth.KEYRING_ACCOUNT) is None
    assert token_path.exists()


def test_resolve_hf_token_falls_back_when_keyring_raises(monkeypatch, fake_keyring, token_path):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    fake_keyring.raise_get = True
    token_path.parent.mkdir(parents=True)
    token_path.write_text("hf_file\n", encoding="utf-8")

    assert auth._resolve_hf_token() == "hf_file"
