"""Claude Code's sign-in on a Mac lives in the login Keychain, not in `~/.claude/.credentials.json`"""

from __future__ import annotations

import json
import stat

import pytest

from jarvis import claude_cli, keychain, usage_meter
from jarvis.claude_cli import CREDENTIALS_ENV

FAKE_SECURITY = r"""#!/usr/bin/env python3
import json, os, sys
item = os.environ["FAKE_KEYCHAIN_ITEM"]
args = sys.argv[1:]
if args[:1] != ["find-generic-password"] or args[1:3] != ["-s", "Claude Code-credentials"] \
        or not os.path.exists(item):
    sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n")
    sys.exit(44)
if "-w" in args:
    print(open(item).read())
else:
    print('keychain: "/Users/u/Library/Keychains/login.keychain-db"')
    print('    "mdat"<timedate>=0x32303236313032373038323731375A00  "20261027082717Z\\000"')
"""

#: 2026-10-27T08:27:17Z, the fake's `mdat`
MDAT = 1793089637.0


@pytest.fixture()
def fake_keychain(tmp_path, monkeypatch):
    """A `security` that knows one item; returns the file holding its secret"""
    binary = tmp_path / "security"
    binary.write_text(FAKE_SECURITY)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    item = tmp_path / "keychain-item.json"
    monkeypatch.setenv(keychain.SECURITY_BIN_ENV, str(binary))
    monkeypatch.setenv("FAKE_KEYCHAIN_ITEM", str(item))
    # The default credentials path, pointed at a config dir with no file in it
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return item


def _sign_in(item, token="kc-token", refresh_expires_at=4102444800000):
    item.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "rt",
        "refreshTokenExpiresAt": refresh_expires_at}}))


def test_the_keychain_item_answers_when_there_is_no_file(fake_keychain):
    _sign_in(fake_keychain)
    stored = claude_cli.stored_signin()
    assert stored.data["claudeAiOauth"]["accessToken"] == "kc-token"
    assert stored.changed_at == MDAT
    assert "Keychain" in stored.source


def test_the_usage_meter_signs_in_with_the_keychain_token(fake_keychain):
    _sign_in(fake_keychain)
    assert usage_meter.token() == "kc-token"


def test_a_sign_in_on_a_mac_is_the_keychain_item_changing(fake_keychain):
    _sign_in(fake_keychain)
    assert claude_cli.signin_changed_at() == MDAT


def test_no_item_is_no_sign_in(fake_keychain):
    assert claude_cli.stored_signin() is None
    assert claude_cli.signin_changed_at() is None
    with pytest.raises(usage_meter.MeterError, match="no credentials file or Keychain item"):
        usage_meter.token()


def test_a_file_outranks_the_keychain(fake_keychain, tmp_path):
    _sign_in(fake_keychain)
    path = tmp_path / "home" / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}))
    assert usage_meter.token() == "file-token"


def test_an_explicit_credentials_file_never_falls_back(fake_keychain, tmp_path, monkeypatch):
    """`JARVIS_CLAUDE_CREDENTIALS` names THE file — the suite's isolation rests on it"""
    _sign_in(fake_keychain)
    monkeypatch.setenv(CREDENTIALS_ENV, str(tmp_path / "absent.json"))
    assert claude_cli.stored_signin() is None


def test_a_config_dir_is_not_the_default_keychain_item(fake_keychain, tmp_path, monkeypatch):
    """Claude Code names a `CLAUDE_CONFIG_DIR`'s item apart, so the default one is someone else's"""
    _sign_in(fake_keychain)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude_cli.stored_signin() is None


def test_a_keychain_secret_that_is_not_json_reads_as_unreadable(fake_keychain):
    fake_keychain.write_text("{not json")
    assert claude_cli.signin_changed_at() is None
    with pytest.raises(usage_meter.MeterError, match="unreadable"):
        usage_meter.token()


def test_no_security_binary_is_no_item(tmp_path, monkeypatch):
    monkeypatch.setenv(keychain.SECURITY_BIN_ENV, str(tmp_path / "missing"))
    assert keychain.generic_password("anything") is None


def test_mdat_parses_as_utc():
    text = '    "mdat"<timedate>=0x3230  "20261027082717Z\\000"\n'
    assert keychain._parse_mdat(text) == MDAT
    assert keychain._parse_mdat("no date here") is None
