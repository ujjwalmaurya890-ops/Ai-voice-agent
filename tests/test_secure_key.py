import logging

import pytest

from campusvoice import secure_key


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secure_key, "_keyring", lambda: fake)
    return fake


def test_no_key_anywhere(fake_keyring):
    assert secure_key.get_api_key() == (None, "none")


def test_env_var_wins_over_credential_store(fake_keyring, monkeypatch):
    fake_keyring.store[(secure_key.SERVICE_NAME, secure_key.USERNAME)] = "stored-" + "x" * 30
    monkeypatch.setenv("GEMINI_API_KEY", "env-" + "y" * 30)
    key, source = secure_key.get_api_key()
    assert source == "env" and key.startswith("env-")


def test_store_and_read_back(fake_keyring):
    secure_key.store_api_key("  " + "k" * 32 + "\n")
    key, source = secure_key.get_api_key()
    assert key == "k" * 32 and source == "credential-manager"
    secure_key.delete_api_key()
    assert secure_key.get_api_key() == (None, "none")


@pytest.mark.parametrize("bad", ["", "   ", "short", "has space inside " + "x" * 30])
def test_rejects_malformed_keys(fake_keyring, bad):
    with pytest.raises(ValueError):
        secure_key.store_api_key(bad)
    assert fake_keyring.store == {}


def test_broken_backend_is_reported_not_crashed(monkeypatch):
    class Broken:
        def get_password(self, *a):
            raise RuntimeError("no backend")

        def set_password(self, *a):
            raise RuntimeError("no backend")

    monkeypatch.setattr(secure_key, "_keyring", lambda: Broken())
    assert secure_key.get_api_key() == (None, "none")
    with pytest.raises(secure_key.SecureStorageError):
        secure_key.store_api_key("k" * 32)


def test_redact_removes_explicit_and_url_keys():
    key = "AIza" + "A" * 35
    text = f"failed wss://host/ws?key={key}&x=1 for {key}"
    cleaned = secure_key.redact(text, key)
    assert key not in cleaned and "key=***" in cleaned


def test_redact_handles_generic_non_aiza_key_via_explicit_value():
    # Not every provider's key matches the AIza... pattern; when the literal
    # value is known (e.g. the key this app is currently using) it must still
    # be scrubbed even though the generic regex would miss it.
    key = "sk-generic-" + "q" * 40
    text = f"auth failed for token {key} in header"
    cleaned = secure_key.redact(text, key)
    assert key not in cleaned


def test_redact_handles_key_repeated_multiple_times():
    key = "AIza" + "C" * 35
    text = f"{key} ... retry with {key} ... final attempt {key}"
    cleaned = secure_key.redact(text, key)
    assert cleaned.count("***") >= 3
    assert key not in cleaned


def test_redact_without_explicit_key_still_scrubs_aiza_pattern():
    # Covers the case where an exception message contains a key CampusVoice
    # was never explicitly given (e.g. a key baked into a URL by the SDK).
    key = "AIza" + "D" * 35
    cleaned = secure_key.redact(f"GET https://x/?key={key} -> 403", key=None)
    assert key not in cleaned


def test_logging_filter_scrubs_records(caplog):
    logger = logging.getLogger("redact-test")
    logger.addFilter(secure_key.RedactingFilter())
    key = "AIza" + "B" * 35
    with caplog.at_level(logging.INFO, logger="redact-test"):
        logger.info("connect failed key=%s", key)
    assert key not in caplog.text
