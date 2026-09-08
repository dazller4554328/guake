# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""Tests for the sftp layer. The session tests drive a real OpenSSH sftp
client against the local sftp-server binary (no network, no ssh) and are
skipped when either is missing."""

import os
import shutil
import threading
import time

import pytest

from guake import sftp
from guake.servers import Server
from guake.sftp import RemoteEntry
from guake.sftp import SftpError
from guake.sftp import SftpSession
from guake.sftp import SftpWorker

SFTP_SERVER_PATHS = [
    "/usr/lib/openssh/sftp-server",
    "/usr/libexec/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server",
    "/usr/libexec/sftp-server",
]


def local_sftp_server():
    return next((p for p in SFTP_SERVER_PATHS if os.path.exists(p)), None)


needs_sftp = pytest.mark.skipif(
    not (shutil.which("sftp") and local_sftp_server()),
    reason="needs OpenSSH sftp and a local sftp-server",
)


# --- parsing -----------------------------------------------------------------


def test_parse_ls_line_file():
    entry = sftp.parse_ls_line("-rw-rw-r--    ? 1000     1000     67108864 Sep  8 20:18 big.bin")
    assert entry == RemoteEntry("big.bin", "file", 67108864, "Sep 8 20:18", "-rw-rw-r--")
    assert not entry.is_dir


def test_parse_ls_line_directory_with_spaces_and_old_date():
    entry = sftp.parse_ls_line("drwxrwxr-x    2 0        0            4096 Apr 22  2024 sub dir")
    assert entry.is_dir
    assert entry.name == "sub dir"
    assert entry.modified == "Apr 22 2024"


def test_parse_ls_line_symlink_strips_target():
    entry = sftp.parse_ls_line(
        "lrwxrwxrwx    1 0        0               7 Sep  8 20:18 link -> big.bin"
    )
    assert entry.is_link
    assert entry.name == "link"


@pytest.mark.parametrize("line", ["", "sftp> ls -lan", "Fetching /a to /b", "total 12"])
def test_parse_ls_line_ignores_other_lines(line):
    assert sftp.parse_ls_line(line) is None


def test_parse_listing_skips_dots_and_puts_directories_first():
    text = "\n".join(
        [
            "drwxr-xr-x    ? 0 0 4096 Sep  8 20:18 .",
            "drwxr-xr-x    ? 0 0 4096 Sep  8 20:18 ..",
            "-rw-r--r--    ? 0 0    3 Sep  8 20:18 b.txt",
            "drwxr-xr-x    ? 0 0 4096 Sep  8 20:18 zdir",
            "-rw-r--r--    ? 0 0    3 Sep  8 20:18 A.txt",
        ]
    )
    assert [e.name for e in sftp.parse_listing(text)] == ["zdir", "A.txt", "b.txt"]


def test_progress_regex_parses_meter_line():
    match = sftp.PROGRESS_RE.match("big.bin        75% 1131MB   1.1GB/s   00:00 ETA")
    assert match.group("percent") == "75"
    assert match.group("done") == "1131MB"
    assert match.group("rate") == "1.1GB/s"
    assert sftp.PROGRESS_RE.match("-rw-r--r--    ? 0 0 3 Sep  8 20:18 100% done.txt") is None


@pytest.mark.parametrize(
    "path, quoted",
    [
        ("/plain", '"/plain"'),
        ("/sub dir/we*ird?[x]", '"/sub dir/we*ird?[x]"'),
        ('/q"uote', '"/q\\"uote"'),
        ("/back\\slash", '"/back\\\\slash"'),
    ],
)
def test_quote_path(path, quoted):
    assert sftp.quote_path(path) == quoted


def test_remote_join_and_parent():
    assert sftp.remote_join("/", "a") == "/a"
    assert sftp.remote_join("/x", "a") == "/x/a"
    assert sftp.remote_parent("/x/a") == "/x"
    assert sftp.remote_parent("/x") == "/"
    assert sftp.remote_parent("/") == "/"


def test_format_size():
    assert sftp.format_size(512) == "512 B"
    assert sftp.format_size(1536) == "1.5 KB"
    assert sftp.format_size(67108864) == "64.0 MB"


# --- command line ----------------------------------------------------------


def test_build_sftp_argv_full():
    server = Server(
        name="web",
        host="10.0.0.5",
        user="root",
        port=2222,
        identity_file="~/.ssh/id",
        jump_host="bastion",
        options="-o ServerAliveInterval=30 -t -X -C -oCompression=yes",
    )
    assert sftp.build_sftp_argv(server) == [
        "sftp",
        "-P",
        "2222",
        "-i",
        os.path.expanduser("~/.ssh/id"),
        "-J",
        "bastion",
        "-o",
        "ServerAliveInterval=30",
        "-C",
        "-oCompression=yes",
        "root@10.0.0.5",
    ]


def test_build_sftp_argv_ssh_config_host_is_bare_alias():
    server = Server(name="alias", host="alias", user="bob", port=2200, id="sshconfig:alias")
    assert sftp.build_sftp_argv(server) == ["sftp", "alias"]


def test_build_sftp_argv_rejects_broken_options():
    with pytest.raises(ValueError):
        sftp.build_sftp_argv(Server(name="a", host="b", options='-o "unterminated'))


# --- live session against the local sftp-server ---------------------------


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remote"
    (root / "sub dir").mkdir(parents=True)
    (root / "we*ird").mkdir()
    (root / "sub dir" / "a file.txt").write_text("hi")
    (root / "we*ird" / "star.txt").write_text("x")
    (root / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
    return root


@pytest.fixture
def session(remote):
    # -D talks to a local sftp-server directly: no ssh, no auth, no network.
    sess = SftpSession(["sftp", "-D", local_sftp_server()])
    sess.start()
    yield sess
    sess.close()


@needs_sftp
def test_session_start_reports_home(session):
    assert session.home == os.getcwd()
    assert session.is_alive


@needs_sftp
def test_listdir_returns_canonical_path_and_entries(session, remote):
    path, entries = session.listdir(str(remote))
    assert path == str(remote)
    assert [(e.name, e.kind) for e in entries] == [
        ("sub dir", "dir"),
        ("we*ird", "dir"),
        ("big.bin", "file"),
    ]
    assert entries[2].size == 3 * 1024 * 1024


@needs_sftp
def test_listdir_glob_characters_are_literal(session, remote):
    (remote / "weXird").mkdir()
    path, entries = session.listdir(str(remote / "we*ird"))
    assert path.endswith("/we*ird")
    assert [e.name for e in entries] == ["star.txt"]


@needs_sftp
def test_listdir_missing_directory_raises_and_keeps_session(session, remote):
    with pytest.raises(SftpError):
        session.listdir(str(remote / "nope"))
    assert session.is_alive
    assert session.listdir(str(remote))[1]


@needs_sftp
def test_download_file_reports_progress(session, remote, tmp_path):
    target = tmp_path / "dl"
    target.mkdir()
    updates = []
    session.download(str(remote / "big.bin"), str(target), progress_cb=updates.append)
    assert (target / "big.bin").read_bytes() == (remote / "big.bin").read_bytes()
    assert updates and updates[-1].percent == 100
    assert updates[-1].name == "big.bin"


@needs_sftp
def test_download_directory_recursively(session, remote, tmp_path):
    target = tmp_path / "dl"
    target.mkdir()
    session.download(str(remote / "sub dir"), str(target), recursive=True)
    assert (target / "sub dir" / "a file.txt").read_text() == "hi"


@needs_sftp
def test_upload_file_and_directory(session, remote, tmp_path):
    local_file = tmp_path / "up load.txt"
    local_file.write_text("up")
    local_dir = tmp_path / "up dir"
    local_dir.mkdir()
    (local_dir / "inner.txt").write_text("in")
    session.upload(str(local_file), str(remote / "we*ird"))
    session.upload(str(local_dir), str(remote), recursive=True)
    assert (remote / "we*ird" / "up load.txt").read_text() == "up"
    assert (remote / "up dir" / "inner.txt").read_text() == "in"


@needs_sftp
def test_mkdir_rename_remove(session, remote):
    session.mkdir(str(remote / 'new "dir"'))
    assert (remote / 'new "dir"').is_dir()
    session.rename(str(remote / 'new "dir"'), str(remote / "renamed"))
    assert (remote / "renamed").is_dir()
    session.remove_file(str(remote / "big.bin"))
    assert not (remote / "big.bin").exists()
    session.remove_dir(str(remote / "sub dir"))
    assert not (remote / "sub dir").exists()
    with pytest.raises(SftpError):
        session.remove_file(str(remote / "big.bin"))


@needs_sftp
def test_cancel_stops_transfer_but_keeps_session(remote, tmp_path):
    # -l limits the bandwidth (Kbit/s) so the transfer is slow enough to cancel.
    session = SftpSession(["sftp", "-l", "80000", "-D", local_sftp_server()])
    session.start()
    (remote / "huge.bin").write_bytes(b"\0" * (100 * 1024 * 1024))
    target = tmp_path / "dl"
    target.mkdir()
    seen = []

    def cancel_soon(progress):
        seen.append(progress.percent)
        if progress.percent >= 5:
            session.cancel()

    with pytest.raises(SftpError, match="Cancelled"):
        session.download(str(remote / "huge.bin"), str(target), progress_cb=cancel_soon)
    assert max(seen) < 100
    assert session.is_alive
    assert session.listdir(str(remote))[0] == str(remote)
    session.close()


@needs_sftp
def test_cancel_after_completion_does_not_kill_session(session, remote, tmp_path):
    target = tmp_path / "dl"
    target.mkdir()
    session.download(str(remote / "big.bin"), str(target))
    session.cancel()
    assert session.listdir(str(remote))[0] == str(remote)


@needs_sftp
def test_start_failure_raises_sftp_error(tmp_path):
    sess = SftpSession(["sftp", "-D", str(tmp_path / "not-a-server")])
    with pytest.raises(SftpError):
        sess.start()
    assert not sess.is_alive


def test_prompt_handler_answers_question_and_secret_flag(tmp_path):
    # A fake "sftp" that asks a question on its tty like ssh does, then
    # behaves like sftp for the marker command.
    fake = tmp_path / "fake-sftp"
    fake.write_text(
        "#!/bin/sh\n"
        "printf 'host fingerprint is SHA256:abc\\n' > /dev/tty\n"
        "printf 'Continue (yes/no)? ' > /dev/tty\n"
        "read answer < /dev/tty\n"
        "printf 'Password: ' > /dev/tty\n"
        "stty -echo < /dev/tty; read pw < /dev/tty; stty echo < /dev/tty\n"
        "while read line; do\n"
        "  printf 'sftp> %s\\n' \"$line\"\n"
        '  case "$line" in\n'
        '    -pwd) printf \'Remote working directory: /home/%s-%s\\n\' "$answer" "$pw" ;;\n'
        "    lpwd) printf 'Local working directory: /x\\n' ;;\n"
        "    bye) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    fake.chmod(0o755)
    prompts = []

    def handler(text, secret):
        prompts.append((text, secret))
        return "yes"

    sess = SftpSession([str(fake)], prompt_handler=handler, password="s3cret")
    assert sess.start() == "/home/yes-s3cret"
    sess.close()
    assert prompts == [("host fingerprint is SHA256:abc\nContinue (yes/no)?", False)]


def test_prompt_without_handler_gives_up(tmp_path):
    fake = tmp_path / "fake-sftp"
    fake.write_text("#!/bin/sh\nprintf 'Password: ' > /dev/tty\nread pw < /dev/tty\n")
    fake.chmod(0o755)
    sess = SftpSession([str(fake)])
    with pytest.raises(SftpError):
        sess.start()
    assert not sess.is_alive


# --- worker ------------------------------------------------------------------


@needs_sftp
def test_worker_runs_jobs_in_order_and_reports(remote):
    worker = SftpWorker(SftpSession(["sftp", "-D", local_sftp_server()]))
    worker.start()
    done = threading.Event()
    results = []

    def record(result, error):
        results.append((result, error))
        if len(results) == 2:
            done.set()

    worker.submit(lambda s: s.listdir(str(remote))[0], record)
    worker.submit(lambda s: s.listdir(str(remote / "missing")), record)
    assert done.wait(10)
    worker.stop()
    worker.join(5)
    assert results[0] == (str(remote), None)
    assert results[1][0] is None and isinstance(results[1][1], SftpError)
    assert not worker.session.is_alive


@needs_sftp
def test_worker_cancels_queued_job(remote):
    worker = SftpWorker(SftpSession(["sftp", "-D", local_sftp_server()]))
    results = []
    job = worker.submit(lambda s: "ran", lambda r, e: results.append((r, str(e))))
    worker.cancel(job)
    worker.start()
    time.sleep(0.5)
    worker.stop()
    worker.join(5)
    assert results == [(None, "Cancelled")]
