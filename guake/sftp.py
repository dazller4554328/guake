# -*- coding: utf-8; -*-
"""
SFTP file transfers for saved servers.

This module drives OpenSSH's own ``sftp`` client so that a transfer uses
exactly the same configuration as the terminal connection (``~/.ssh/config``,
keys, agent, jump host, extra options) without any extra dependency. It has
no GTK dependency; the panel lives in :mod:`guake.sftppanel`.

One :class:`SftpSession` is one long-lived ``sftp`` process:

* commands are written to its stdin (a pipe, so sftp is not "interactive"
  and simply executes what it reads, echoing every command as ``sftp> ...``),
* its stdout is a pseudo-terminal, which keeps the progress meter enabled
  and lets ssh ask questions (password, passphrase, host key) that are
  forwarded to a prompt handler instead of hanging,
* its stderr is a second pseudo-terminal (sftp only shows progress when
  stderr is a tty), read separately so error messages can be attributed to
  the command that caused them,
* every command is prefixed with ``-`` so that a failure does not end the
  session, and followed by a marker command (``lpwd``) whose output tells us
  the command has finished.

All calls on a session are blocking and must happen off the GTK main loop;
:class:`SftpWorker` runs them on a background thread.
"""

import fcntl
import logging
import os
import pty
import queue
import re
import select
import shlex
import signal
import struct
import subprocess
import termios
import threading

from dataclasses import dataclass
from typing import Callable
from typing import List
from typing import Optional
from typing import Tuple

from guake.servers import DEFAULT_SSH_PORT
from guake.servers import Server
from guake.servers import is_ssh_config_server

log = logging.getLogger(__name__)

PTY_COLUMNS = 100
PTY_ROWS = 24
MARKER_COMMAND = "lpwd"
MARKER_PREFIX = "Local working directory: "
ECHO_PREFIX = "sftp> "
PWD_PREFIX = "Remote working directory: "
PWD_PREFIX_LEN = len(PWD_PREFIX)
# How long the output must stay silent before an unterminated line is taken
# for a question from ssh (password, host key...).
PROMPT_SILENCE_SECONDS = 0.3
CLOSE_TIMEOUT_SECONDS = 2.0
PROMPT_CONTEXT_LINES = 12

# One entry of ``ls -lan`` as formatted by the sftp client itself:
# perms nlink uid gid size month day time-or-year name
LS_LINE_RE = re.compile(
    r"^(?P<perms>[-dlbcpsD][rwxsStT-]{9}[+@.]?)\s+\S+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+(?P<modified>[A-Za-z]{3}\s+\d{1,2}\s+[\d:]{4,5})\s+(?P<name>.+)$"
)
# One update of the transfer progress meter: "name  45%  120MB  4.5MB/s  00:10 ETA"
PROGRESS_RE = re.compile(
    r"^(?P<name>.*?)\s+(?P<percent>\d{1,3})%\s+(?P<done>\S+)\s+(?P<rate>\S+/s)\s+(?P<eta>.*?)\s*$"
)
PROMPT_RE = re.compile(r"[:?]\s*$")
SECRET_PROMPT_RE = re.compile(r"password|passphrase|code|pin|token|secret", re.IGNORECASE)
PASSWORD_PROMPT_RE = re.compile(r"password", re.IGNORECASE)

KIND_DIR = "dir"
KIND_FILE = "file"
KIND_LINK = "link"
KIND_OTHER = "other"
_KIND_BY_PERM = {"d": KIND_DIR, "D": KIND_DIR, "-": KIND_FILE, "l": KIND_LINK}


class SftpError(Exception):
    """A command failed or the sftp process went away."""


@dataclass(frozen=True)
class RemoteEntry:
    """One file or directory of a remote listing."""

    name: str
    kind: str
    size: int
    modified: str
    permissions: str

    @property
    def is_dir(self) -> bool:
        return self.kind == KIND_DIR

    @property
    def is_link(self) -> bool:
        return self.kind == KIND_LINK


@dataclass(frozen=True)
class Progress:
    """One update of sftp's progress meter for the file being transferred."""

    name: str
    percent: int
    done: str
    rate: str
    eta: str


@dataclass(frozen=True)
class CommandResult:
    """What one :meth:`SftpSession.run` call produced: the stdout of each
    command (in order) and everything written to stderr meanwhile."""

    outputs: List[str]
    stderr: str
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return not self.interrupted and not self.stderr.strip()

    @property
    def error(self) -> str:
        return "Cancelled" if self.interrupted else self.stderr.strip()


def parse_ls_line(line: str) -> Optional[RemoteEntry]:
    """Turn one ``ls -lan`` line into a :class:`RemoteEntry`, or ``None`` for
    lines that are not entries (blank, headers, unexpected format)."""
    match = LS_LINE_RE.match(line.rstrip("\r"))
    if not match:
        return None
    perms = match.group("perms")
    kind = _KIND_BY_PERM.get(perms[0], KIND_OTHER)
    name = match.group("name")
    if kind == KIND_LINK and " -> " in name:
        name = name.split(" -> ", 1)[0]
    return RemoteEntry(
        name=name,
        kind=kind,
        size=int(match.group("size")),
        modified=re.sub(r"\s+", " ", match.group("modified")),
        permissions=perms,
    )


def parse_listing(text: str) -> List[RemoteEntry]:
    """Entries of a ``ls -lan`` output, without ``.`` and ``..``, directories
    first then files, both sorted case-insensitively by name."""
    entries = []
    for line in text.splitlines():
        entry = parse_ls_line(line)
        if entry is not None and entry.name not in (".", ".."):
            entries.append(entry)
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))


def quote_path(path: str) -> str:
    """Quote a path for the sftp command line. Double quotes protect spaces
    and glob characters; only backslash and the quote itself need escaping."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def remote_join(directory: str, name: str) -> str:
    if directory.endswith("/"):
        return directory + name
    return f"{directory}/{name}"


def remote_parent(path: str) -> str:
    if path in ("", "/"):
        return "/"
    parent = path.rstrip("/").rsplit("/", 1)[0]
    return parent or "/"


def format_size(size: int) -> str:
    """Human readable size (``1.2 MB``); exact bytes below one kilobyte."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover - unreachable


# Options of ssh that sftp understands with the same meaning. Anything else
# in a server's "extra options" is dropped for sftp (with a log message).
_PASSTHROUGH_FLAGS = {"-C", "-4", "-6"}
_PASSTHROUGH_WITH_VALUE = {"-o", "-F", "-c", "-i", "-J", "-l"}


def sftp_options_from_ssh(options: str) -> List[str]:
    """Keep the parts of a server's ssh options that also apply to sftp."""
    try:
        tokens = shlex.split(options)
    except ValueError as e:
        raise ValueError(f"Invalid SSH options: {e}") from e
    kept: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _PASSTHROUGH_FLAGS:
            kept.append(token)
        elif token in _PASSTHROUGH_WITH_VALUE and i + 1 < len(tokens):
            kept += [token, tokens[i + 1]]
            i += 1
        elif len(token) > 2 and token[:2] in _PASSTHROUGH_WITH_VALUE:
            kept.append(token)  # "-oServerAliveInterval=30"
        else:
            log.debug("Dropping ssh option %r for sftp", token)
        i += 1
    return kept


def build_sftp_argv(server: Server) -> List[str]:
    """The ``sftp`` command line for a server, mirroring
    :func:`guake.servers.build_ssh_argv`."""
    argv = ["sftp"]
    if is_ssh_config_server(server):
        return [*argv, server.host]
    if server.port != DEFAULT_SSH_PORT:
        argv += ["-P", str(server.port)]
    if server.identity_file:
        argv += ["-i", os.path.expanduser(server.identity_file)]
    if server.jump_host:
        argv += ["-J", server.jump_host]
    if server.options:
        try:
            argv += sftp_options_from_ssh(server.options)
        except ValueError as e:
            raise ValueError(f"Invalid SSH options for server {server.name!r}: {e}") from e
    argv.append(server.target)
    return argv


PromptHandler = Callable[[str, bool], Optional[str]]
ProgressCallback = Callable[[Progress], None]


class SftpSession:
    """One ``sftp`` process, driven command by command.

    ``prompt_handler(text, secret)`` is called (on the calling thread) when
    ssh asks a question during a command; it returns the answer, or ``None``
    to give up, which ends the session. ``password`` answers the first
    password prompt by itself.
    """

    def __init__(
        self,
        argv: List[str],
        prompt_handler: Optional[PromptHandler] = None,
        password: Optional[str] = None,
        env: Optional[dict] = None,
    ):
        self.argv = list(argv)
        self.prompt_handler = prompt_handler
        self._password = password
        self._env = env
        self._proc: Optional[subprocess.Popen] = None
        self._master: Optional[int] = None
        self._stderr_master: Optional[int] = None
        self._lock = threading.Lock()
        self._interrupted = False
        self.home: Optional[str] = None
        self.connect_messages = ""

    # -- lifecycle -------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> str:
        """Spawn sftp, answer its questions, and return the remote home
        directory. Raises :class:`SftpError` when the connection fails."""
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0))
        stderr_master, stderr_slave = pty.openpty()

        def make_controlling_tty():
            # ssh reads passwords from /dev/tty: make our pty that tty.
            fcntl.ioctl(1, termios.TIOCSCTTY, 0)

        log.info("Starting sftp: %s", shlex.join(self.argv))
        try:
            self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
                self.argv,
                stdin=subprocess.PIPE,
                stdout=slave,
                stderr=stderr_slave,
                start_new_session=True,
                preexec_fn=make_controlling_tty,
                env=self._env,
            )
        except OSError as e:
            for fd in (master, slave, stderr_master, stderr_slave):
                os.close(fd)
            raise SftpError(f"Cannot run {self.argv[0]}: {e}") from e
        os.close(slave)
        os.close(stderr_slave)
        self._master = master
        self._stderr_master = stderr_master
        flags = fcntl.fcntl(stderr_master, fcntl.F_GETFL)
        fcntl.fcntl(stderr_master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        result = self.run(["pwd"])
        self.connect_messages = result.stderr
        self.home = _parse_pwd(result.outputs[0])
        if self.home is None:
            self.close()
            raise SftpError(result.stderr.strip() or "sftp did not report the remote directory")
        return self.home

    def close(self) -> None:
        """End the session, politely first."""
        proc, self._proc = self._proc, None
        masters = [fd for fd in (self._master, self._stderr_master) if fd is not None]
        self._master = self._stderr_master = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.write(b"bye\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=CLOSE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            proc.stdin.close()
        except OSError:
            pass
        for fd in masters:
            os.close(fd)

    def cancel(self) -> None:
        """Interrupt the transfer in progress; the session stays usable.
        The signal itself is sent by the output reader, and only while a
        transfer is visibly running: an idle sftp dies on SIGINT."""
        self._interrupted = True

    def _interrupt_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)

    # -- commands --------------------------------------------------------------

    def run(
        self, commands: List[str], progress_cb: Optional[ProgressCallback] = None
    ) -> CommandResult:
        """Execute ``commands`` in order and wait for them all to finish."""
        with self._lock:
            if not self.is_alive:
                raise SftpError("Not connected")
            script = "".join(f"-{command}\n" for command in commands) + f"{MARKER_COMMAND}\n"
            try:
                self._proc.stdin.write(script.encode("utf-8", "surrogateescape"))
                self._proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise SftpError(f"Connection lost: {e}") from e
            self._interrupted = False
            reader = _OutputReader(self, progress_cb)
            result = reader.read(len(commands))
            if self._interrupted:
                result = CommandResult(result.outputs, result.stderr, interrupted=True)
            return result

    def listdir(self, path: str) -> Tuple[str, List[RemoteEntry]]:
        """``(canonical_path, entries)`` of a remote directory."""
        result = self.run([f"cd {quote_path(path)}", "pwd", "ls -lan"])
        if not result.ok:
            raise SftpError(result.error)
        real_path = _parse_pwd(result.outputs[1])
        if real_path is None:
            raise SftpError("sftp did not report the remote directory")
        return real_path, parse_listing(result.outputs[2])

    def download(
        self,
        remote_path: str,
        local_dir: str,
        recursive: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> None:
        flags = "-r " if recursive else ""
        self._check(
            self.run([f"get {flags}{quote_path(remote_path)} {quote_path(local_dir)}"], progress_cb)
        )

    def upload(
        self,
        local_path: str,
        remote_dir: str,
        recursive: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> None:
        flags = "-r " if recursive else ""
        self._check(
            self.run([f"put {flags}{quote_path(local_path)} {quote_path(remote_dir)}"], progress_cb)
        )

    def mkdir(self, path: str) -> None:
        self._check(self.run([f"mkdir {quote_path(path)}"]))

    def rename(self, old: str, new: str) -> None:
        self._check(self.run([f"rename {quote_path(old)} {quote_path(new)}"]))

    def remove_file(self, path: str) -> None:
        self._check(self.run([f"rm {quote_path(path)}"]))

    def remove_dir(self, path: str) -> None:
        """Remove a directory and everything below it (sftp has no recursive
        remove, so the tree is walked here, deepest entries first)."""
        _real_path, entries = self.listdir(path)
        for entry in entries:
            child = remote_join(path, entry.name)
            if entry.is_dir:
                self.remove_dir(child)
            else:
                self.remove_file(child)
        self._check(self.run([f"rmdir {quote_path(path)}"]))

    @staticmethod
    def _check(result: CommandResult) -> None:
        if not result.ok:
            raise SftpError(result.error)


def _parse_pwd(output: str) -> Optional[str]:
    for line in output.splitlines():
        if line.startswith(PWD_PREFIX):
            return line[PWD_PREFIX_LEN:].strip()
    return None


class _OutputReader:
    """Reads the pty and stderr of a session until the marker line shows up,
    reporting progress and forwarding questions from ssh."""

    def __init__(self, session: SftpSession, progress_cb):
        self.session = session
        self.progress_cb = progress_cb
        self.lines: List[str] = []
        self.partial = ""
        self.stderr = b""
        self.password_used = False
        self.done = False
        self.last_percent: Optional[int] = None
        self.signalled = False

    def read(self, command_count: int) -> CommandResult:
        proc = self.session._proc  # pylint: disable=protected-access
        master = self.session._master  # pylint: disable=protected-access
        stderr_fd = self.session._stderr_master  # pylint: disable=protected-access
        while not self.done:
            readable, _, _ = select.select([master, stderr_fd], [], [], PROMPT_SILENCE_SECONDS)
            if stderr_fd in readable:
                self._drain_stderr()
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    self._connection_lost()
                self._feed(data.decode("utf-8", "replace"))
            elif not readable:
                if proc.poll() is not None:
                    self._connection_lost()
                self._maybe_answer_prompt()
            self._maybe_interrupt()
        self._drain_stderr()
        return CommandResult(_split_outputs(self.lines, command_count), self._stderr_text())

    def _connection_lost(self):
        self._drain_stderr()
        self.session.close()
        message = self._stderr_text().strip() or self.partial.strip()
        raise SftpError(message or "Connection closed")

    def _drain_stderr(self):
        fd = self.session._stderr_master  # pylint: disable=protected-access
        while fd is not None:
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, OSError, ValueError):
                return
            if not data:
                return
            self.stderr += data

    def _stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace").replace("\r", "")

    def _feed(self, text: str) -> None:
        # "\r\n" ends an ordinary line; a lone "\r" separates progress
        # meter updates. A trailing "\r" may be half of a "\r\n" split
        # across two reads, so it waits for the next chunk.
        buffer = (self.partial + text).replace("\r\n", "\n")
        self.partial = ""
        while buffer and not self.done:
            newline = buffer.find("\n")
            carriage = buffer.find("\r")
            if carriage != -1 and (newline == -1 or carriage < newline):
                if carriage == len(buffer) - 1:
                    break
                rest = carriage + 1
                segment, buffer = buffer[:carriage], buffer[rest:]
                self._line(segment)
            elif newline != -1:
                rest = newline + 1
                line, buffer = buffer[:newline], buffer[rest:]
                self._line(line)
            else:
                break
        self.partial = buffer

    def _line(self, line: str) -> None:
        if line.startswith(MARKER_PREFIX):
            self.done = True
        elif line:
            self._progress(line) or self.lines.append(line)

    def _progress(self, segment: str) -> bool:
        match = PROGRESS_RE.match(segment) if segment.strip() else None
        if match is None:
            return False
        self.last_percent = int(match.group("percent"))
        if self.progress_cb is not None:
            self.progress_cb(
                Progress(
                    name=match.group("name").strip(),
                    percent=self.last_percent,
                    done=match.group("done"),
                    rate=match.group("rate"),
                    eta=match.group("eta").replace("ETA", "").strip(),
                )
            )
        return True

    def _maybe_interrupt(self) -> None:
        """Send SIGINT once a cancel was requested, but only while a file is
        being transferred (progress seen, not yet complete)."""
        session = self.session
        if self.signalled or not session._interrupted:  # pylint: disable=protected-access
            return
        if self.last_percent is not None and self.last_percent < 100:
            self.signalled = True
            session._interrupt_process()  # pylint: disable=protected-access

    def _maybe_answer_prompt(self) -> None:
        prompt = self.partial.strip()
        if not prompt or not PROMPT_RE.search(prompt):
            return
        self.partial = ""
        # Show the lines printed just before the question too: for a host
        # key check that is where the fingerprint is.
        context = []
        for line in reversed(self.lines[-PROMPT_CONTEXT_LINES:]):
            if line.startswith(ECHO_PREFIX):
                break
            context.insert(0, line)
        if context:
            prompt = "\n".join([*context, prompt])
        answer = None
        password = self.session._password  # pylint: disable=protected-access
        if password and not self.password_used and PASSWORD_PROMPT_RE.search(prompt):
            self.password_used = True
            answer = password
        elif self.session.prompt_handler is not None:
            if self.password_used and PASSWORD_PROMPT_RE.search(prompt):
                prompt = "The saved password was not accepted.\n" + prompt
            answer = self.session.prompt_handler(prompt, bool(SECRET_PROMPT_RE.search(prompt)))
        if answer is None:
            log.info("No answer for sftp prompt %r, giving up", prompt)
            self.session.close()
            raise SftpError("Cancelled")
        master = self.session._master  # pylint: disable=protected-access
        os.write(master, (answer + "\n").encode("utf-8", "surrogateescape"))


def _split_outputs(lines: List[str], command_count: int) -> List[str]:
    """Group output lines by the ``sftp> command`` echo that precedes them."""
    outputs: List[List[str]] = []
    for line in lines:
        if line.startswith(ECHO_PREFIX):
            outputs.append([])
        elif outputs:
            outputs[-1].append(line)
    # The last group is the marker command's own echo.
    groups = outputs[:command_count]
    while len(groups) < command_count:
        groups.append([])
    return ["\n".join(group) for group in groups]


class Job:
    """One unit of work for :class:`SftpWorker`: ``func(session)`` runs on
    the worker thread, then ``on_done(result, error)`` is called there too."""

    def __init__(self, func, on_done=None, description=""):
        self.func = func
        self.on_done = on_done
        self.description = description
        self.cancelled = False
        self.running = False


class SftpWorker(threading.Thread):
    """Runs jobs one after another on one session in a background thread.
    The session is started by the first job; a job that finds the session
    dead fails with :class:`SftpError`."""

    def __init__(self, session: SftpSession, name: str = "sftp"):
        super().__init__(name=f"guake-{name}", daemon=True)
        self.session = session
        self._queue: "queue.Queue[Optional[Job]]" = queue.Queue()
        self.current: Optional[Job] = None

    def submit(self, func, on_done=None, description="") -> Job:
        job = Job(func, on_done, description)
        self._queue.put(job)
        return job

    def stop(self) -> None:
        self._queue.put(None)
        self.session.cancel()

    def cancel(self, job: Job) -> None:
        job.cancelled = True
        if job is self.current:
            self.session.cancel()

    def run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                break
            if job.cancelled:
                self._finish(job, None, SftpError("Cancelled"))
                continue
            self.current = job
            job.running = True
            result = error = None
            try:
                if not self.session.is_alive:
                    self.session.start()
                result = job.func(self.session)
            except SftpError as e:
                error = e
            except Exception as e:  # pylint: disable=broad-except
                log.exception("sftp job %s failed", job.description)
                error = SftpError(str(e))
            self.current = None
            if job.cancelled and error is None:
                error = SftpError("Cancelled")
            self._finish(job, result, error)
        self.session.close()

    @staticmethod
    def _finish(job: Job, result, error) -> None:
        job.running = False
        if job.on_done is not None:
            try:
                job.on_done(result, error)
            except Exception:  # pylint: disable=broad-except
                log.exception("sftp job callback failed")
