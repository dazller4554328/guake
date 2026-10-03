# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access

import json

import pytest

from guake import serversync as sync
from guake.servers import SSH_CONFIG_ID_PREFIX
from guake.servers import GroupColor
from guake.servers import Server
from guake.servers import ServerStore
from guake.servers import ServersFile
from guake.servers import Tombstone

ME = 111


def server(name, updated_at, **fields):
    fields.setdefault("id", name)
    fields.setdefault("host", f"{name}.lan")
    return Server(name=name, updated_at=updated_at, **fields)


def peer(servers=(), deleted=None):
    return ServersFile(servers=list(servers), deleted=deleted or {})


# --- planning -------------------------------------------------------------------


def test_server_only_on_peer_is_added():
    changes = sync.plan_sync([], {}, {"laptop": peer([server("web", 10)])})
    assert [(c.kind, c.server.name, c.device, c.recommended) for c in changes] == [
        ("add", "web", "laptop", True)
    ]


def test_identical_servers_produce_no_change():
    assert sync.plan_sync([server("web", 10)], {}, {"laptop": peer([server("web", 20)])}) == []


def test_newer_peer_copy_is_an_update():
    local = server("web", 10)
    changes = sync.plan_sync([local], {}, {"laptop": peer([server("web", 20, user="root")])})
    assert len(changes) == 1
    change = changes[0]
    assert change.kind == sync.UPDATE and change.recommended
    assert change.server.user == "root" and change.previous == local


def test_older_peer_copy_is_ignored():
    local = server("web", 30, user="admin")
    assert sync.plan_sync([local], {}, {"laptop": peer([server("web", 20, user="root")])}) == []


def test_same_timestamp_different_settings_is_offered_but_not_recommended():
    changes = sync.plan_sync([server("web", 0)], {}, {"laptop": peer([server("web", 0, port=2)])})
    assert [(c.kind, c.recommended) for c in changes] == [("update", False)]


def test_update_matched_by_name_keeps_local_id():
    local = server("web", 10, id="local-id")
    changes = sync.plan_sync([local], {}, {"laptop": peer([server("WEB", 20, id="peer-id")])})
    assert changes[0].server.id == "local-id"


def test_peer_deletion_is_proposed_but_never_preselected():
    local = server("web", 10)
    changes = sync.plan_sync(
        [local], {}, {"laptop": peer(deleted={"web": Tombstone(at=20, name="web")})}
    )
    assert [(c.kind, c.server, c.recommended, c.when) for c in changes] == [
        ("delete", local, False, 20)
    ]


def test_deletion_matched_by_name_when_ids_differ():
    local = server("web", 10, id="local-id")
    changes = sync.plan_sync(
        [local], {}, {"laptop": peer(deleted={"peer-id": Tombstone(at=20, name="web")})}
    )
    assert [(c.kind, c.server.id) for c in changes] == [("delete", "local-id")]


def test_deletion_older_than_local_edit_is_ignored():
    local = server("web", 30)
    assert (
        sync.plan_sync([local], {}, {"laptop": peer(deleted={"web": Tombstone(20, "web")})}) == []
    )


def test_server_deleted_locally_is_not_added_back():
    changes = sync.plan_sync(
        [], {"web": Tombstone(at=20, name="web")}, {"laptop": peer([server("web", 10)])}
    )
    assert changes == []


def test_server_edited_on_peer_after_local_deletion_is_added_back():
    changes = sync.plan_sync(
        [], {"web": Tombstone(at=20, name="web")}, {"laptop": peer([server("web", 30)])}
    )
    assert [c.kind for c in changes] == ["add"]


def test_server_deleted_on_another_peer_is_not_added():
    peers = {
        "laptop": peer([server("web", 10)]),
        "desktop": peer(deleted={"web": Tombstone(at=20, name="web")}),
    }
    assert sync.plan_sync([], {}, peers) == []


def test_most_recent_event_wins_across_peers():
    local = server("web", 10)
    peers = {
        "laptop": peer([server("web", 30, user="new")]),
        "desktop": peer(deleted={"web": Tombstone(at=20, name="web")}),
    }
    changes = sync.plan_sync([local], {}, peers)
    assert [(c.kind, c.device) for c in changes] == [("update", "laptop")]


def test_same_new_server_on_two_peers_is_added_once():
    peers = {"laptop": peer([server("web", 10)]), "desktop": peer([server("web", 20, user="x")])}
    changes = sync.plan_sync([], {}, peers)
    assert [(c.kind, c.device) for c in changes] == [("add", "desktop")]


def test_changes_are_sorted_adds_updates_deletes():
    local = [server("b", 10), server("c", 10)]
    peers = {
        "p": peer([server("a", 5), server("b", 20, port=2)], {"c": Tombstone(at=30, name="c")}),
    }
    assert [c.kind for c in sync.plan_sync(local, {}, peers)] == ["add", "update", "delete"]


def test_apply_changes_writes_to_store(tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    store.apply_sync([server("old", 1e12), server("web", 1e12)], {})
    changes = sync.plan_sync(
        store.servers,
        store.deleted,
        {
            "p": peer(
                [server("new", 2e12), server("web", 2e12, user="root")],
                {"old": Tombstone(at=2e12, name="old")},
            )
        },
    )
    sync.apply_changes(store, changes)
    assert sorted((s.name, s.user) for s in store.servers) == [("new", ""), ("web", "root")]
    assert store.get("new").updated_at == 2e12
    assert "old" in store.deleted


def test_deleted_server_saved_under_another_id_is_not_added_back():
    changes = sync.plan_sync(
        [], {"a1": Tombstone(at=20, name="web")}, {"laptop": peer([server("web", 10, id="b1")])}
    )
    assert changes == []


def test_same_id_offered_by_two_peers_keeps_newest_copy_only():
    peers = {
        "laptop": peer([server("web", 10, id="x")]),
        "desktop": peer([server("web2", 20, id="x")]),
    }
    changes = sync.plan_sync([], {}, peers)
    assert [(c.server.name, c.device) for c in changes] == [("web2", "desktop")]


def test_added_server_with_ssh_options_needs_review():
    changes = sync.plan_sync([], {}, {"p": peer([server("web", 10, command="htop")])})
    assert not changes[0].recommended and "remote command" in changes[0].warning
    assert sync.needs_confirmation(changes[0])


def test_changed_destination_of_password_server_needs_review():
    local = server("web", 10, use_password=True)
    changes = sync.plan_sync(
        [local], {}, {"p": peer([server("web", 20, use_password=True, host="evil")])}
    )
    assert not changes[0].recommended and "password" in changes[0].warning


def test_plain_update_needs_no_review():
    changes = sync.plan_sync([server("web", 10)], {}, {"p": peer([server("web", 20, group="x")])})
    assert changes[0].recommended and not sync.needs_confirmation(changes[0])


# --- payload --------------------------------------------------------------------


def test_payload_round_trip_skips_ssh_config_entries():
    shared = ServersFile(
        servers=[server("web", 5), server("cfg", 0, id=SSH_CONFIG_ID_PREFIX + "cfg")],
        deleted={"x": Tombstone(at=3, name="x")},
    )
    parsed = sync.parse_payload(json.loads(json.dumps(sync.build_payload(shared))), now=100)
    assert [s.name for s in parsed.shared.servers] == ["web"]
    assert parsed.shared.servers[0].updated_at == 5
    assert parsed.shared.deleted == shared.deleted and parsed.refused == 0


def test_payload_carries_group_colors_and_clamps_their_time():
    shared = ServersFile(
        servers=[server("web", 5)],
        deleted={},
        groups={"Prod": GroupColor("#e62d42", 1e300), "Lab": GroupColor("", 4.0)},
    )
    parsed = sync.parse_payload(json.loads(json.dumps(sync.build_payload(shared))), now=100.0)
    assert parsed.shared.groups == {
        "Prod": GroupColor("#e62d42", 100.0),
        "Lab": GroupColor("", 4.0),
    }


def test_payload_from_an_older_guake_has_no_group_colors():
    data = {"schema_version": 1, "servers": []}
    assert sync.parse_payload(data).shared.groups == {}


def test_plan_group_colors_takes_only_newer_picks():
    local = {"Prod": GroupColor("#e62d42", 50.0), "Lab": GroupColor("#3a944a", 50.0)}
    peers = {
        "laptop": ServersFile(
            [server("web", 1, group="Prod")], {}, {"Prod": GroupColor("#3584e4", 60.0)}
        ),
        "desk": ServersFile(
            [server("nas", 1, group="Lab"), server("new", 1, group="New")],
            {},
            {
                "Lab": GroupColor("#9141ac", 10.0),
                "New": GroupColor("#c88800", 1.0),
                "Unused": GroupColor("#c88800", 99.0),
            },
        ),
    }
    assert sync.plan_group_colors(local, peers) == {
        "Prod": GroupColor("#3584e4", 60.0),
        "New": GroupColor("#c88800", 1.0),
    }


def test_payload_drops_invalid_servers():
    data = {"schema_version": 1, "servers": [{"name": "x", "host": "-oProxyCommand=evil"}, 3]}
    assert sync.parse_payload(data).shared.servers == []


def test_payload_clamps_future_timestamps():
    shared = ServersFile([server("web", 1e300)], {"x": Tombstone(at=1e300, name="x")})
    parsed = sync.parse_payload(sync.build_payload(shared), now=100.0)
    assert parsed.shared.servers[0].updated_at == 100.0
    assert parsed.shared.deleted["x"].at == 100.0


@pytest.mark.parametrize(
    "options",
    [
        "-o ProxyCommand=nc %h %p",
        "-oLocalCommand=touch /tmp/x -o PermitLocalCommand=yes",
        "-o 'KnownHostsCommand /bin/x'",
        "-F /tmp/evil_config",
        "-vF/tmp/evil",
        "-I /tmp/lib.so",
        "-o pkcs11provider=/tmp/lib.so",
        "-o Include=/tmp/x",
        "-o 'unbalanced",
    ],
)
def test_payload_refuses_options_that_run_local_code(options):
    shared = ServersFile([server("web", 5, options=options), server("ok", 5)], {})
    parsed = sync.parse_payload(sync.build_payload(shared), now=100)
    assert [s.name for s in parsed.shared.servers] == ["ok"] and parsed.refused == 1


@pytest.mark.parametrize(
    "options", ["", "-o ServerAliveInterval=30", "-A -L 8080:localhost:80", "-p 22 -i ~/.ssh/k"]
)
def test_ordinary_options_are_accepted(options):
    assert sync.risky_ssh_options(options) == []


def test_payload_refuses_control_characters_in_command():
    shared = ServersFile([server("web", 5, command="ls\x1b[2J")], {})
    assert sync.parse_payload(sync.build_payload(shared)).refused == 1


def test_payload_rejects_too_many_servers():
    data = {"schema_version": 1, "servers": [{}] * (sync.MAX_SYNCED_SERVERS + 1)}
    with pytest.raises(sync.SyncError, match="too many"):
        sync.parse_payload(data)


@pytest.mark.parametrize(
    "data", [None, [], {"servers": "x"}, {"schema_version": 99, "servers": []}]
)
def test_payload_rejects_bad_layout(data):
    with pytest.raises(sync.SyncError):
        sync.parse_payload(data)


# --- tailscale ------------------------------------------------------------------


STATUS = {
    "BackendState": "Running",
    "Self": {
        "HostName": "me",
        "DNSName": "me.tail.ts.net.",
        "TailscaleIPs": ["100.1.1.1", "fd7a::1"],
        "UserID": ME,
    },
    "Peer": {
        "a": {
            "HostName": "laptop",
            "DNSName": "laptop.tail.ts.net.",
            "TailscaleIPs": ["100.1.1.2"],
            "UserID": ME,
            "Online": True,
        },
        "b": {"HostName": "old", "TailscaleIPs": ["100.1.1.3"], "UserID": ME, "Online": False},
        "c": {"HostName": "friend", "TailscaleIPs": ["100.1.1.4"], "UserID": 999, "Online": True},
        "d": {
            "HostName": "ci",
            "TailscaleIPs": ["100.1.1.5"],
            "UserID": ME,
            "Tags": ["tag:ci"],
            "Online": True,
        },
    },
}


def test_own_devices_lists_same_user_untagged_online_first():
    assert sync.own_devices(STATUS) == [
        sync.Device("laptop", "100.1.1.2", True),
        sync.Device("old", "100.1.1.3", False),
    ]


def test_self_info_uses_ipv4():
    assert sync.self_info(STATUS) == sync.TailscaleSelf("me", "100.1.1.1", ME)


def test_tagged_device_cannot_sync():
    status = {**STATUS, "Self": {**STATUS["Self"], "Tags": ["tag:server"]}}
    with pytest.raises(sync.SyncError, match="tagged"):
        sync.self_info(status)


def test_missing_tailscale_is_reported(mocker):
    mocker.patch("guake.serversync.shutil.which", return_value=None)
    with pytest.raises(sync.SyncError, match="not installed"):
        sync.tailscale_status()


def test_stopped_tailscale_is_reported(mocker):
    mocker.patch("guake.serversync._run_tailscale", return_value={"BackendState": "Stopped"})
    with pytest.raises(sync.SyncError, match="Stopped"):
        sync.tailscale_status()


def _tailscale_answers(mocker, stdout):
    mocker.patch("guake.serversync.shutil.which", return_value="/usr/bin/tailscale")
    return mocker.patch(
        "guake.serversync.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout=stdout, stderr=""),
    )


def test_tailscale_json_flag_comes_before_arguments(mocker):
    run = _tailscale_answers(mocker, '{"UserProfile": {"ID": 111}}')
    assert sync.whois_user_id("100.1.1.2") == ME
    assert run.call_args[0][0] == ["/usr/bin/tailscale", "whois", "--json", "100.1.1.2"]


def test_whois_of_tagged_device_has_no_user(mocker):
    _tailscale_answers(mocker, '{"Node": {"Tags": ["tag:x"]}, "UserProfile": {"ID": 111}}')
    assert sync.whois_user_id("100.1.1.5") is None
