# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import json
import shlex
import subprocess

import pytest

from guake import servers as srv
from guake.servers import Server
from guake.servers import ServerStore


@pytest.fixture
def store(tmp_path):
    return ServerStore(tmp_path / "servers.json")


@pytest.fixture
def web():
    return Server(name="web-1", host="10.0.0.5", user="root", group="Prod", id="web1")


# --- model -------------------------------------------------------------------


def test_server_gets_an_id_when_none_given():
    server = Server(name="a", host="b")
    assert len(server.id) == 32


def test_server_target_includes_user_only_when_set():
    assert Server(name="a", host="example.org").target == "example.org"
    assert Server(name="a", host="example.org", user="bob").target == "bob@example.org"


@pytest.mark.parametrize("bad", [0, 70000, "22"])
def test_server_rejects_invalid_port(bad):
    with pytest.raises(ValueError):
        Server(name="a", host="b", port=bad)


def test_server_rejects_blank_name_or_host():
    with pytest.raises(ValueError):
        Server(name="  ", host="b")
    with pytest.raises(ValueError):
        Server(name="a", host="")


def test_with_changes_returns_new_object(web):
    changed = web.with_changes(port=2222)
    assert changed.port == 2222
    assert web.port == 22
    assert changed.id == web.id


def test_from_dict_ignores_unknown_keys_and_coerces_port():
    server = Server.from_dict({"name": "a", "host": "b", "port": "2200", "bogus": 1})
    assert server.port == 2200


def test_group_servers_orders_ungrouped_first_then_groups_alphabetically():
    servers = [
        Server(name="z", host="h", group="beta"),
        Server(name="b", host="h"),
        Server(name="a", host="h", group="Alpha"),
        Server(name="c", host="h", group="beta"),
    ]
    groups = srv.group_servers(servers)
    assert [g for g, _ in groups] == ["", "Alpha", "beta"]
    assert [s.name for s in dict(groups)["beta"]] == ["c", "z"]


# --- persistence ---------------------------------------------------------------


def test_load_missing_file_returns_empty_list(tmp_path):
    assert srv.load_servers(tmp_path / "nope.json") == []


def test_load_broken_json_returns_empty_list(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text("{not json", encoding="utf-8")
    assert srv.load_servers(path) == []


def test_load_newer_schema_is_refused(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"schema_version": 99, "servers": []}), encoding="utf-8")
    assert srv.load_servers(path) == []


def test_load_skips_invalid_entries_but_keeps_good_ones(tmp_path):
    path = tmp_path / "servers.json"
    payload = {
        "schema_version": 1,
        "servers": [{"name": "ok", "host": "h"}, {"name": "", "host": "h"}, {"host": "x"}],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert [s.name for s in srv.load_servers(path)] == ["ok"]


def test_save_then_load_round_trip(tmp_path, web):
    path = tmp_path / "sub" / "servers.json"
    srv.save_servers(path, [web])
    loaded = srv.load_servers(path)
    assert loaded == [web]
    assert not path.with_suffix(".json.tmp").exists()


def test_password_is_never_written_to_disk(tmp_path, web):
    path = tmp_path / "servers.json"
    srv.save_servers(path, [web.with_changes(use_password=True)])
    text = path.read_text(encoding="utf-8")
    assert "password" not in text.replace("use_password", "")


def test_store_add_update_remove_persist_and_do_not_mutate(store, web):
    before = store.servers
    store.add(web)
    assert before == []
    assert store.get("web1") == web
    assert ServerStore(store.path).get("web1") == web

    store.update(web.with_changes(host="10.0.0.6"))
    assert store.get("web1").host == "10.0.0.6"

    store.remove("web1")
    assert store.servers == []
    assert ServerStore(store.path).servers == []


def test_store_update_unknown_server_raises(store, web):
    with pytest.raises(KeyError):
        store.update(web)


def test_store_find_by_name_is_case_insensitive(store, web):
    store.add(web)
    assert store.find_by_name("WEB-1") == web
    assert store.find_by_name("missing") is None


def test_store_groups_lists_distinct_non_empty_groups(store):
    store.add(Server(name="a", host="h", group="Prod"))
    store.add(Server(name="b", host="h", group="dev"))
    store.add(Server(name="c", host="h"))
    assert store.groups() == ["dev", "Prod"]


def test_store_import_skips_names_already_saved(store, web):
    store.add(web)
    added = store.import_servers([Server(name="WEB-1", host="x"), Server(name="new", host="y")])
    assert [s.name for s in added] == ["new"]
    assert len(store.servers) == 2


# --- ssh command line ---------------------------------------------------------


def test_build_ssh_argv_minimal():
    assert srv.build_ssh_argv(Server(name="a", host="example.org")) == ["ssh", "example.org"]


def test_build_ssh_argv_full(tmp_path):
    server = Server(
        name="a",
        host="example.org",
        user="bob",
        port=2222,
        identity_file="~/.ssh/key",
        jump_host="bastion",
        options="-o ServerAliveInterval=30 -C",
        command="tmux attach || tmux",
    )
    argv = srv.build_ssh_argv(server)
    assert argv[:1] == ["ssh"]
    assert argv[1:3] == ["-p", "2222"]
    assert argv[3] == "-i" and argv[4].endswith("/.ssh/key") and "~" not in argv[4]
    assert argv[5:7] == ["-J", "bastion"]
    assert argv[7:10] == ["-o", "ServerAliveInterval=30", "-C"]
    assert argv[10:] == ["-t", "bob@example.org", "tmux attach || tmux"]


def test_build_ssh_argv_for_ssh_config_entry_is_bare_alias():
    server = Server(name="box", host="box", user="u", port=2200, id="sshconfig:box")
    assert srv.build_ssh_argv(server) == ["ssh", "box"]
    assert srv.build_ssh_argv(server.with_changes(command="htop")) == ["ssh", "-t", "box", "htop"]


def test_build_launch_wraps_ssh_in_reconnect_loop(web):
    argv, env = srv.build_launch(web)
    assert argv[:2] == [srv.WRAPPER_SHELL, "-c"]
    script = argv[2]
    assert "ssh root@10.0.0.5" in script
    assert "read -r answer" in script
    assert "Connection to web-1 closed" in script
    assert env == []


def test_build_launch_uses_sshpass_via_environment(mocker, web):
    mocker.patch("guake.servers.shutil.which", return_value="/usr/bin/sshpass")
    argv, env = srv.build_launch(web, password="s3cret")
    assert "sshpass -e ssh root@10.0.0.5" in argv[2]
    assert "s3cret" not in argv[2]
    assert env == ["SSHPASS=s3cret"]


def test_build_launch_without_sshpass_falls_back_and_warns_in_terminal(mocker, web):
    mocker.patch("guake.servers.shutil.which", return_value=None)
    argv, env = srv.build_launch(web, password="s3cret")
    assert env == []
    assert "sshpass -e" not in argv[2]
    assert "sshpass" in argv[2]  # the notice printed to the user


def test_build_launch_quotes_server_names_safely():
    server = Server(name='it\'s "weird" 100% $(name)', host="h")
    argv, _ = srv.build_launch(server)
    printf_line = next(line for line in argv[2].splitlines() if line.startswith("  printf '\\n'"))
    # The message is a single printf format argument followed by "$status".
    words = shlex.split(printf_line)
    assert words[0] == "printf"
    assert words[1] == '\\nConnection to it\'s "weird" 100%% $(name) closed (exit status %s).\\n'
    assert words[2] == "$status"


@pytest.fixture
def fake_ssh_bin(tmp_path):
    """A PATH directory whose ``ssh`` just exits with status 7."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ssh").write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    (fake_bin / "ssh").chmod(0o755)
    return fake_bin


def run_wrapper(argv, stdin, fake_bin):
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )


def test_build_launch_prints_real_exit_status(fake_ssh_bin):
    argv, _ = srv.build_launch(Server(name="box", host="h"))
    result = run_wrapper(argv, "\n", fake_ssh_bin)
    assert result.returncode == 7
    assert "Connection to box closed (exit status 7)." in result.stdout


def test_build_launch_reconnects_on_r(fake_ssh_bin):
    argv, _ = srv.build_launch(Server(name="box", host="h"))
    result = run_wrapper(argv, "r\n\n", fake_ssh_bin)
    assert result.returncode == 7
    assert result.stdout.count("Connection to box closed") == 2


def test_build_launch_deferred_waits_for_reconnect_request(fake_ssh_bin):
    argv, _ = srv.build_launch(Server(name="box", host="h"), deferred=True)

    closed = run_wrapper(argv, "\n", fake_ssh_bin)
    assert closed.returncode == 0
    assert "Tab for box restored, not connected yet." in closed.stdout
    assert "Connection to box closed" not in closed.stdout

    reconnected = run_wrapper(argv, "r\n\n", fake_ssh_bin)
    assert reconnected.returncode == 7
    assert "Connection to box closed (exit status 7)." in reconnected.stdout


def test_build_ssh_argv_rejects_unbalanced_options():
    with pytest.raises(ValueError, match="SSH options for server 'a'"):
        srv.build_ssh_argv(Server(name="a", host="h", options="-o 'oops"))


# --- ~/.ssh/config -------------------------------------------------------------


SSH_CONFIG = """
# comment
Host *
    ServerAliveInterval 30

Host web web-alias
    HostName 10.0.0.5
    User deploy
    Port 2200
    IdentityFile ~/.ssh/deploy
    IdentityFile ~/.ssh/other
    ProxyJump bastion

Host bastion
  User root

Host tokens
  IdentityFile %d/.ssh/key

Host bad-*
  User nobody

Match host something
  User matched
"""


def test_parse_ssh_config(tmp_path):
    path = tmp_path / "config"
    path.write_text(SSH_CONFIG, encoding="utf-8")
    parsed = srv.parse_ssh_config(path)
    assert [s.name for s in parsed] == ["bastion", "tokens", "web"]
    web = parsed[-1]
    assert web.host == "web"
    assert web.user == "deploy"
    assert web.port == 2200
    assert web.identity_file == "~/.ssh/deploy"
    assert web.jump_host == "bastion"
    assert web.group == srv.SSH_CONFIG_GROUP
    assert srv.is_ssh_config_server(web)
    assert parsed[1].identity_file == ""


def test_parse_ssh_config_missing_file(tmp_path):
    assert srv.parse_ssh_config(tmp_path / "config") == []


def test_as_saved_server_gets_fresh_id_and_no_group(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host box\n  User me\n", encoding="utf-8")
    saved = srv.as_saved_server(srv.parse_ssh_config(path)[0])
    assert not srv.is_ssh_config_server(saved)
    assert saved.group == ""
    assert saved.user == "me"
    assert srv.build_ssh_argv(saved) == ["ssh", "me@box"]


# --- first connection: unknown host key ----------------------------------------


def test_build_hostkey_argv_only_checks_the_host_key(tmp_path):
    key = tmp_path / "id"
    server = Server(
        name="a",
        host="h",
        user="u",
        port=2222,
        identity_file=str(key),
        jump_host="bastion",
        options="-o ServerAliveInterval=30",
        command="tmux",
    )
    assert srv.build_hostkey_argv(server) == [
        "ssh",
        "-o",
        "PreferredAuthentications=none",
        "-p",
        "2222",
        "-i",
        str(key),
        "-J",
        "bastion",
        "-o",
        "ServerAliveInterval=30",
        "u@h",
        "true",
    ]


def test_build_hostkey_argv_for_ssh_config_entry():
    server = Server(name="db", host="db", id="sshconfig:db", command="tmux")
    assert srv.build_hostkey_argv(server) == [
        "ssh",
        "-o",
        "PreferredAuthentications=none",
        "db",
        "true",
    ]


@pytest.fixture
def fake_sshpass_bin(tmp_path):
    """PATH directory with an ``sshpass`` that exits with the statuses listed
    in ``statuses`` (one per call, last one repeats) and an ``ssh`` that logs
    its arguments, standing in for the host key question."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / "statuses").write_text("6\n0\n", encoding="utf-8")
    (fake_bin / "sshpass").write_text(
        "#!/bin/sh\n"
        f"f={tmp_path}/statuses\n"
        'status=$(head -n1 "$f")\n'
        'if [ "$(wc -l < "$f")" -gt 1 ]; then sed -i 1d "$f"; fi\n'
        f"echo sshpass >> {tmp_path}/calls\n"
        "exit $status\n",
        encoding="utf-8",
    )
    (fake_bin / "ssh").write_text(
        f'#!/bin/sh\necho "ssh $*" >> {tmp_path}/calls\nexit 255\n', encoding="utf-8"
    )
    for name in ("sshpass", "ssh"):
        (fake_bin / name).chmod(0o755)
    return fake_bin


def _launch_with_password(mocker, server):
    mocker.patch("guake.servers.shutil.which", return_value="/usr/bin/sshpass")
    argv, env = srv.build_launch(server, password="pw")
    return argv, env


def test_unknown_host_key_asks_ssh_to_confirm_then_retries(mocker, tmp_path, fake_sshpass_bin):
    argv, _ = _launch_with_password(mocker, Server(name="box", host="h"))
    result = run_wrapper(argv, "\n", fake_sshpass_bin)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert calls == [
        "sshpass",
        "ssh -o PreferredAuthentications=none h true",
        "sshpass",
    ]
    assert "First connection to box" in result.stdout
    assert result.returncode == 0


def test_rejected_host_key_is_asked_once_and_explained(mocker, tmp_path, fake_sshpass_bin):
    (tmp_path / "statuses").write_text("6\n", encoding="utf-8")
    argv, _ = _launch_with_password(mocker, Server(name="box", host="h"))
    result = run_wrapper(argv, "\n", fake_sshpass_bin)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert calls.count("sshpass") == 2
    assert len([c for c in calls if c.startswith("ssh ")]) == 1
    assert "host key is not trusted" in result.stdout
    assert result.returncode == 6


def test_reconnect_asks_about_the_host_key_again(mocker, tmp_path, fake_sshpass_bin):
    (tmp_path / "statuses").write_text("6\n", encoding="utf-8")
    argv, _ = _launch_with_password(mocker, Server(name="box", host="h"))
    run_wrapper(argv, "r\n\n", fake_sshpass_bin)
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert len([c for c in calls if c.startswith("ssh ")]) == 2


def test_changed_host_key_is_explained(mocker, tmp_path, fake_sshpass_bin):
    (tmp_path / "statuses").write_text("7\n", encoding="utf-8")
    argv, _ = _launch_with_password(mocker, Server(name="box", host="h", port=2222))
    result = run_wrapper(argv, "\n", fake_sshpass_bin)
    assert "HOST KEY HAS CHANGED" in result.stdout
    assert "ssh-keygen -R '[h]:2222'" in result.stdout
    assert "ssh " not in (tmp_path / "calls").read_text(encoding="utf-8")


# --- colour ------------------------------------------------------------------


def test_server_color_defaults_to_none_and_accepts_hex():
    assert Server(name="a", host="b").color == ""
    assert Server(name="a", host="b", color="#3584E4").color == "#3584e4"


@pytest.mark.parametrize("bad", ["red", "#12345", "#gggggg", "3584e4"])
def test_server_rejects_invalid_color(bad):
    with pytest.raises(ValueError):
        Server(name="a", host="b", color=bad)


def test_from_dict_drops_an_invalid_color_instead_of_the_server():
    server = Server.from_dict({"name": "a", "host": "b", "color": "nope"})
    assert server.color == ""


# --- merging (backup import) -------------------------------------------------


def test_store_merge_adds_new_and_updates_by_id_or_name(store, web):
    other = Server(name="db", host="10.0.0.9", id="db1")
    store.add(web)
    store.add(other)
    incoming = [
        web.with_changes(host="10.0.0.6"),  # same id
        Server(name="DB", host="10.0.0.10", id="elsewhere"),  # same name
        Server(name="new", host="10.0.0.11", id="new1"),
    ]
    result = store.merge(incoming)
    assert [s.name for s in result.added] == ["new"]
    assert sorted(s.id for s in result.updated) == ["db1", "web1"]
    assert store.get("web1").host == "10.0.0.6"
    # A server matched by name keeps the local id so its keyring entry stays valid.
    assert store.get("db1").host == "10.0.0.10"
    assert store.get("elsewhere") is None
    assert len(ServerStore(store.path).servers) == 3


def test_store_merge_does_not_write_when_nothing_changes(store, web):
    store.add(web)
    mtime = store.path.stat().st_mtime_ns
    result = store.merge([web])
    assert result.added == [] and result.updated == []
    assert result.unchanged == [web]
    assert store.path.stat().st_mtime_ns == mtime


# --- hostile values ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "-oProxyCommand=touch /tmp/pwn"),
        ("host", "a b"),
        ("user", "-oProxyCommand=x"),
        ("user", "a b"),
        ("user", "a@b"),
        ("jump_host", "-oProxyCommand=x"),
        ("name", "evil\x1b]0;title\x07"),
        ("host", "h\n"),
    ],
)
def test_server_rejects_values_ssh_would_read_as_options_or_control_chars(field, value):
    kwargs = {"name": "a", "host": "h", field: value}
    with pytest.raises(ValueError):
        Server(**kwargs)


def test_backslashes_in_the_name_are_printed_literally(fake_ssh_bin):
    argv, _ = srv.build_launch(Server(name="a\\033[31mb", host="h"))
    result = run_wrapper(argv, "\n", fake_ssh_bin)
    assert "Connection to a\\033[31mb closed" in result.stdout


# --- timestamps and tombstones (used by server sync) ----------------------------


def test_store_stamps_added_and_changed_servers(store, web, mocker):
    mocker.patch("guake.servers.time.time", return_value=100.0)
    store.add(web)
    assert store.get("web1").updated_at == 100.0
    mocker.patch("guake.servers.time.time", return_value=200.0)
    store.update(web.with_changes(host="10.0.0.6"))
    assert store.get("web1").updated_at == 200.0


def test_store_keeps_timestamp_when_saving_unchanged_server(store, web, mocker):
    mocker.patch("guake.servers.time.time", return_value=100.0)
    store.add(web)
    mocker.patch("guake.servers.time.time", return_value=200.0)
    store.update(web)
    assert store.get("web1").updated_at == 100.0


def test_updated_at_is_ignored_when_comparing_servers(web):
    assert web.with_changes(updated_at=5.0) == web


def test_remove_records_a_tombstone_that_survives_reload(store, web, mocker):
    mocker.patch("guake.servers.time.time", return_value=100.0)
    store.add(web)
    store.remove("web1")
    assert store.deleted == {"web1": srv.Tombstone(at=100.0, name="web-1")}
    assert ServerStore(store.path).deleted == store.deleted


def test_tombstone_is_dropped_when_server_comes_back(store, web):
    store.add(web)
    store.remove("web1")
    store.add(web)
    assert store.deleted == {}


def test_old_tombstones_are_pruned(store, web, mocker):
    mocker.patch("guake.servers.time.time", return_value=100.0)
    store.add(web)
    store.remove("web1")
    mocker.patch("guake.servers.time.time", return_value=100.0 + srv.TOMBSTONE_MAX_AGE + 1)
    store.add(Server(name="other", host="h"))
    assert store.deleted == {}


def test_apply_sync_keeps_incoming_timestamps_and_records_removals(store, web, mocker):
    mocker.patch("guake.servers.time.time", return_value=100.0)
    store.add(web)
    db = Server(name="db", host="db.lan", id="db", updated_at=42.0)
    store.apply_sync([db], {"web1": srv.Tombstone(at=50.0, name="web-1")})
    assert [s.id for s in store.servers] == ["db"]
    assert store.get("db").updated_at == 42.0
    assert store.deleted["web1"].at == 50.0


def test_load_ignores_malformed_tombstones_and_timestamps(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "servers": [
                    {"name": "ok", "host": "h", "updated_at": 3},
                    {"name": "bad", "host": "h", "updated_at": "soon"},
                ],
                "deleted": {"x": {"at": -1}, "y": "nope", "z": {"at": 7, "name": "gone"}},
            }
        )
    )
    loaded = srv.load_servers_file(path)
    assert [(s.name, s.updated_at) for s in loaded.servers] == [("ok", 3.0)]
    assert loaded.deleted == {"z": srv.Tombstone(at=7.0, name="gone")}


def test_file_without_sync_fields_still_loads(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"schema_version": 1, "servers": [{"name": "a", "host": "h"}]}))
    loaded = srv.load_servers_file(path)
    assert loaded.servers[0].updated_at == 0.0 and loaded.deleted == {}
