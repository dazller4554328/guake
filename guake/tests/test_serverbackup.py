# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import json
import stat

import pytest

from guake import serverbackup as bk
from guake.servers import Server
from guake.servers import save_servers


@pytest.fixture
def servers():
    return [
        Server(name="web-1", host="10.0.0.5", user="root", id="web1", use_password=True),
        Server(name="db", host="10.0.0.9", id="db1", identity_file="~/.ssh/id_db", color="#e01b24"),
    ]


@pytest.fixture
def secrets():
    return bk.Secrets(
        passwords={"web1": "s3cret"},
        keys={"~/.ssh/id_db": b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"},
    )


KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n"
MINE = KEY.replace(b"key", b"mine")
THEIRS = KEY.replace(b"key", b"theirs")

needs_crypto = pytest.mark.skipif(
    not bk.is_encryption_available(), reason="python3-cryptography is not installed"
)


def test_backup_without_secrets_round_trips(tmp_path, servers):
    path = tmp_path / "backup.json"
    bk.write_backup(path, bk.build_backup(servers))
    backup = bk.load_backup(path)
    assert backup.servers == servers
    assert not backup.has_secrets


def test_backup_file_is_private(tmp_path, servers):
    path = tmp_path / "backup.json"
    bk.write_backup(path, bk.build_backup(servers))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


@needs_crypto
def test_secrets_are_encrypted_and_round_trip(tmp_path, servers, secrets):
    path = tmp_path / "backup.json"
    bk.write_backup(path, bk.build_backup(servers, secrets, passphrase="correct horse"))
    text = path.read_text(encoding="utf-8")
    assert "s3cret" not in text
    assert "OPENSSH" not in text
    backup = bk.load_backup(path)
    assert backup.has_secrets
    assert bk.decrypt_secrets(backup, "correct horse") == secrets


@needs_crypto
def test_wrong_passphrase_is_reported(tmp_path, servers, secrets):
    path = tmp_path / "backup.json"
    bk.write_backup(path, bk.build_backup(servers, secrets, passphrase="right"))
    with pytest.raises(bk.WrongPassphrase):
        bk.decrypt_secrets(bk.load_backup(path), "wrong")


def test_secrets_need_a_passphrase(servers, secrets):
    with pytest.raises(ValueError):
        bk.build_backup(servers, secrets, passphrase="")


def test_plain_servers_file_can_be_imported(tmp_path, servers):
    path = tmp_path / "servers.json"
    save_servers(path, servers)
    backup = bk.load_backup(path)
    assert sorted(s.id for s in backup.servers) == ["db1", "web1"]
    assert not backup.has_secrets


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", json.dumps({"format": "something-else"}), json.dumps({"servers": 3})],
)
def test_unreadable_files_raise_backup_error(tmp_path, content):
    path = tmp_path / "x.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.load_backup(path)


def test_newer_backup_version_is_refused(tmp_path, servers):
    data = bk.build_backup(servers)
    data["version"] = bk.BACKUP_VERSION + 1
    path = tmp_path / "b.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bk.BackupError, match="newer"):
        bk.load_backup(path)


def test_invalid_server_entries_are_skipped(tmp_path, servers):
    data = bk.build_backup(servers)
    data["servers"].append({"name": "", "host": "x"})
    path = tmp_path / "b.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert len(bk.load_backup(path).servers) == 2


def test_collect_keys_reads_identity_files(tmp_path, servers):
    key = tmp_path / "id_db"
    key.write_bytes(KEY)
    listed = [servers[0], servers[1].with_changes(identity_file=str(key))]
    missing = servers[1].with_changes(id="x", name="x", identity_file=str(tmp_path / "nope"))
    assert bk.collect_keys([*listed, missing]) == {str(key): KEY}


# --- restoring private keys --------------------------------------------------


def test_restore_key_writes_a_missing_key_where_the_server_expects_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    path = bk.restore_key("~/.ssh/id_db", KEY, home=home)
    assert path == "~/.ssh/id_db"
    written = home / ".ssh" / "id_db"
    assert written.read_bytes() == KEY
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".ssh").stat().st_mode) == 0o700


def test_restore_key_keeps_an_identical_existing_key(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_bytes(KEY)
    assert bk.restore_key("~/.ssh/id_test", KEY, home=home) == "~/.ssh/id_test"


def test_restore_key_never_overwrites_a_different_key(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_bytes(MINE)
    path = bk.restore_key("~/.ssh/id_test", THEIRS, home=home)
    assert (home / ".ssh" / "id_test").read_bytes() == MINE
    assert path.startswith("~/.ssh/guake-imported/id_test-")
    assert (home / path[2:]).read_bytes() == THEIRS
    # Importing the same backup again reuses that copy.
    assert bk.restore_key("~/.ssh/id_test", THEIRS, home=home) == path


@pytest.mark.parametrize(
    "identity",
    [
        "/home/someone-else/.ssh/id_rsa",
        "~/.bashrc",
        "~/.ssh/../.profile",
        "/etc/passwd",
        "~/.ssh/config",
        "~/.ssh/authorized_keys",
        "~/.ssh/known_hosts",
        "~/.ssh/id_rsa.pub",
    ],
)
def test_restore_key_only_writes_inside_the_ssh_directory(tmp_path, identity):
    home = tmp_path / "home"
    home.mkdir()
    path = bk.restore_key(identity, KEY, home=home)
    assert path.startswith("~/.ssh/guake-imported/")
    assert (home / path[2:]).read_bytes() == KEY
    assert sorted(p.name for p in home.iterdir()) == [".ssh"]


# --- applying a backup to the store ------------------------------------------


class FakeKeyring:
    def __init__(self, passwords=None, works=True):
        self.passwords = dict(passwords or {})
        self.works = works

    def clear_password(self, server_id):
        return self.passwords.pop(server_id, None) is not None

    def lookup_password(self, server_id):
        return self.passwords.get(server_id)

    def store_password(self, server_id, password, label):
        if self.works:
            self.passwords[server_id] = password
        return self.works


def test_collect_secrets_reads_keyring_and_keys(tmp_path, servers):
    key = tmp_path / "id"
    key.write_bytes(KEY)
    listed = [servers[0], servers[1].with_changes(identity_file=str(key))]
    secrets = bk.collect_secrets(listed, FakeKeyring({"web1": "pw", "db1": "ignored"}))
    # Only servers marked use_password contribute a password.
    assert secrets == bk.Secrets(passwords={"web1": "pw"}, keys={str(key): KEY})


def test_import_adds_servers_passwords_and_keys(tmp_path, servers, secrets):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    keyring = FakeKeyring()
    home = tmp_path / "home"
    home.mkdir()

    summary = bk.import_backup(store, servers, secrets, keyring, home=home)

    assert [s.name for s in summary.added] == ["web-1", "db"]
    assert summary.passwords == 1 and summary.keys == 1
    assert keyring.passwords == {"web1": "s3cret"}
    assert store.get("web1").use_password
    assert (home / ".ssh" / "id_db").read_bytes().startswith(b"-----BEGIN")


def test_import_without_secrets_clears_password_flag_when_keyring_has_none(tmp_path, servers):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    bk.import_backup(store, servers, bk.Secrets(), FakeKeyring(), home=tmp_path)
    # The password was not brought over, so ssh will simply ask for it.
    assert not store.get("web1").use_password


def test_import_matching_by_name_stores_password_under_the_local_id(tmp_path, servers, secrets):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    store.add(Server(name="WEB-1", host="old", id="local"))
    keyring = FakeKeyring()

    summary = bk.import_backup(store, servers[:1], secrets, keyring, home=tmp_path)

    assert [s.id for s in summary.updated] == ["local"]
    assert keyring.passwords == {"local": "s3cret"}
    assert store.get("local").host == "10.0.0.5"
    assert store.get("local").use_password


def test_import_keeps_existing_keyring_password_when_backup_has_none(tmp_path, servers):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    store.add(servers[0].with_changes(color="#3584e4"))
    bk.import_backup(store, servers[:1], bk.Secrets(), FakeKeyring({"web1": "x"}), home=tmp_path)
    assert store.get("web1").use_password


def test_import_rewrites_identity_file_when_key_was_moved(tmp_path, servers, secrets):
    from guake.servers import ServerStore

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_db").write_bytes(b"a different key")
    store = ServerStore(tmp_path / "servers.json")

    bk.import_backup(store, servers[1:], secrets, FakeKeyring(), home=home)

    assert store.get("db1").identity_file.startswith("~/.ssh/guake-imported/id_db-")


# --- hostile backups -----------------------------------------------------------


@pytest.mark.parametrize("content", [b"Host *\n  ProxyCommand touch /tmp/pwn\n", b"x" * 100_000])
def test_restore_key_refuses_content_that_is_not_a_private_key(tmp_path, content):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(bk.BackupError):
        bk.restore_key("~/.ssh/id_rsa", content, home=home)
    assert not (home / ".ssh" / "id_rsa").exists()


def test_import_skips_bad_keys_but_imports_the_servers(tmp_path, servers):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    secrets = bk.Secrets(keys={"~/.ssh/id_db": b"not a key"})
    summary = bk.import_backup(store, servers[1:], secrets, FakeKeyring(), home=tmp_path)
    assert summary.keys == 0
    assert store.get("db1").identity_file == "~/.ssh/id_db"


def test_import_ignores_a_key_for_an_empty_identity_file(tmp_path, servers):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    secrets = bk.Secrets(keys={"": KEY})
    summary = bk.import_backup(store, servers[:1], secrets, FakeKeyring(), home=tmp_path)
    assert summary.keys == 0
    assert not (tmp_path / ".ssh").exists()


def test_import_gives_forged_ssh_config_ids_a_fresh_id(tmp_path):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    forged = Server(name="x", host="h", id="sshconfig:x")
    summary = bk.import_backup(store, [forged], bk.Secrets(), FakeKeyring(), home=tmp_path)
    assert not summary.added[0].id.startswith("sshconfig:")


def test_retargeted_server_loses_its_saved_password(tmp_path):
    from guake.servers import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    store.add(Server(name="prod", host="prod.example", id="local", use_password=True))
    keyring = FakeKeyring({"local": "real password"})
    evil = Server(name="prod", host="attacker.example", id="evil", use_password=True)

    bk.import_backup(store, [evil], bk.Secrets(), keyring, home=tmp_path)

    assert "local" not in keyring.passwords
    assert not store.get("local").use_password


def test_risky_servers_lists_fields_that_run_commands(servers):
    risky = [
        Server(name="a", host="h", options="-o ProxyCommand=nc %h %p"),
        Server(name="b", host="h", command="tmux"),
        Server(name="c", host="h", jump_host="bastion"),
    ]
    assert [s.name for s in bk.risky_servers([*servers, *risky])] == ["a", "b", "c"]


@needs_crypto
@pytest.mark.parametrize(
    "field,value",
    [("n", 2**24), ("r", 64), ("p", 16), ("kdf", "pbkdf2"), ("cipher", "none"), ("salt", "AAAA")],
)
def test_hostile_kdf_parameters_are_refused(tmp_path, servers, secrets, field, value):
    data = bk.build_backup(servers, secrets, passphrase="long enough pass")
    data["secrets"][field] = value
    path = tmp_path / "b.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.decrypt_secrets(bk.load_backup(path), "long enough pass")


@needs_crypto
def test_kdf_parameters_are_authenticated(tmp_path, servers, secrets):
    data = bk.build_backup(servers, secrets, passphrase="long enough pass")
    data["secrets"]["n"] = 2**14  # weaker, still within limits
    path = tmp_path / "b.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.decrypt_secrets(bk.load_backup(path), "long enough pass")


def test_oversized_backup_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "MAX_BACKUP_BYTES", 10)
    path = tmp_path / "b.json"
    bk.write_backup(path, bk.build_backup([]))
    with pytest.raises(bk.BackupError, match="too large"):
        bk.load_backup(path)


def test_write_backup_does_not_follow_a_planted_tmp_symlink(tmp_path, servers):
    victim = tmp_path / "victim"
    victim.write_text("precious", encoding="utf-8")
    path = tmp_path / "backup.json"
    (tmp_path / "backup.json.tmp").symlink_to(victim)
    bk.write_backup(path, bk.build_backup(servers))
    assert victim.read_text(encoding="utf-8") == "precious"
    assert bk.load_backup(path).servers == servers


def test_collect_keys_only_exports_private_keys(tmp_path, servers):
    creds = tmp_path / "credentials"
    creds.write_bytes(b"aws_secret_access_key = x")
    server = servers[1].with_changes(identity_file=str(creds))
    assert bk.collect_keys([server]) == {}
