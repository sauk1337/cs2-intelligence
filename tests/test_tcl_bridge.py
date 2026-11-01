"""Tcl ⇄ Python integration: runs eggdrop/scripts/cs2intel.tcl in a real Tcl 8.6 interpreter
(with Eggdrop primitives stubbed) against the real backend served by uvicorn."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

import os
import sys

# On Windows with a venv, tkinter intermittently fails to find init.tcl: set it explicitly.
for _cand in (Path(sys.base_prefix) / "tcl" / "tcl8.6", Path(sys.base_prefix) / "lib" / "tcl8.6"):
    if (_cand / "init.tcl").exists():
        os.environ.setdefault("TCL_LIBRARY", str(_cand))
        break

tkinter = pytest.importorskip("tkinter")

from conftest import ROOT  # noqa: E402

from backend.api.server import build_api  # noqa: E402
from backend.app import App  # noqa: E402

SCRIPT = (ROOT / "eggdrop" / "scripts" / "cs2intel.tcl").as_posix()

STUBS = r"""
set ::server "irc.test.net:6667"
set ::botnick "CS2Intel"
set ::OUT {}
set ::LOG {}
proc putlog {m} { lappend ::LOG $m }
proc puthelp {m} { lappend ::OUT $m; set ::GOT 1 }
proc putserv {m} { lappend ::OUT $m }
proc putdcc {i m} { lappend ::OUT $m }
proc bind {type flags mask cmd} { set ::BINDS($type,$mask) $cmd }
proc setudef {t n} {}
proc channel {sub chan flag} { return 1 }
proc validchan {c} { return 1 }
proc botonchan {c} { return 1 }
proc channels {} { return {#test} }
proc matchattr {h f {c ""}} { return [expr {$h eq "owner"}] }
proc queuesize {q} { return 0 }
proc utimer {s cmd} { return timer0 }
proc utimers {} { return {} }
proc killutimer {id} {}
proc wait_out {} {
    set ::GOT 0
    set id [after 8000 {set ::GOT timeout}]
    vwait ::GOT
    after cancel $id
    after 150 {set ::DRAIN 1}
    vwait ::DRAIN
    return $::GOT
}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend(threading.Thread):
    def __init__(self, cfg, db_path: Path, port: int) -> None:
        super().__init__(daemon=True)
        self.cfg, self.db_path, self.port = cfg, db_path, port
        self.ready = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app: App | None = None
        self.server: uvicorn.Server | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self.app = App(self.cfg, db_url=f"sqlite+aiosqlite:///{self.db_path.as_posix()}",
                       http_transport=httpx.MockTransport(lambda r: httpx.Response(599)))
        await self.app.start(run_scheduler=False)
        self.server = uvicorn.Server(uvicorn.Config(build_api(self.app), host="127.0.0.1", port=self.port,
                                                    log_level="error"))
        task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        self.ready.set()
        await task
        await self.app.stop()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(10)


@pytest.fixture
def bridge(cfg, tmp_path):
    port = free_port()
    be = Backend(cfg, tmp_path / "tcl.db", port)
    be.start()
    assert be.ready.wait(20), "the backend did not start"
    # On Windows, creating a Tcl interpreter from a venv sometimes fails to locate init.tcl: retry
    for attempt in range(5):
        try:
            tcl = tkinter.Tcl()
            break
        except tkinter.TclError:
            if attempt == 4:
                raise
            time.sleep(0.3)
    tcl.eval(STUBS)
    tcl.eval(f'set cs2intel(api_url) "http://127.0.0.1:{port}"')
    tcl.eval('set cs2intel(token) "test-token"; set cs2intel(network) "testnet"; set cs2intel(user_cooldown) 0')
    # Python reads the file (Tcl's `source` failed intermittently on Windows while the file was being scanned)
    for attempt in range(5):
        try:
            code = Path(SCRIPT).read_text(encoding="ascii")
            break
        except OSError:
            time.sleep(0.2)
    tcl.call("eval", code)
    yield tcl, be, port
    be.stop()


def out(tcl) -> list[str]:
    return list(tcl.splitlist(tcl.eval("set ::OUT")))


def run_pub(tcl, cmd: str, args: str = "", hand: str = "*") -> str:
    tcl.eval("set ::OUT {}")
    tcl.eval(f'{{*}}$::BINDS(pub,!{cmd}) alice alice@host {hand} #test {{{args}}}')
    return tcl.eval("wait_out")


def test_script_loads_and_binds_all_commands(bridge):
    tcl, _be, _ = bridge
    for c in ("live", "skin", "market", "admin", "followmatch", "watch", "news"):
        assert tcl.eval(f"info exists ::BINDS(pub,!{c})") == "1"
    assert any("loaded" in ln for ln in tcl.splitlist(tcl.eval("set ::LOG")))


def test_channel_command_roundtrip(bridge):
    tcl, _be, _ = bridge
    assert run_pub(tcl, "version") == "1"
    lines = out(tcl)
    assert lines and lines[0].startswith("PRIVMSG #test :CS2 Intelligence Bot v")


def test_notice_reply_and_utf8(bridge):
    tcl, _be, _ = bridge
    run_pub(tcl, "help", "news")
    assert out(tcl)[0].startswith("NOTICE alice :NEWS !news")
    run_pub(tcl, "skin", "ñandú ×")  # accents and symbols arrive intact and the reply comes back in UTF-8
    assert "ñandú" in out(tcl)[0]


def test_admin_permission_from_eggdrop_flags(bridge):
    tcl, be, _ = bridge
    run_pub(tcl, "admin", "pause", hand="*")
    assert "Permission denied" in out(tcl)[0] and not be.app.publisher.paused
    run_pub(tcl, "admin", "pause", hand="owner")
    assert "PAUSED" in out(tcl)[0] and be.app.publisher.paused


def test_outbox_polling_delivers_automatic_posts(bridge):
    tcl, be, _ = bridge
    be.call(be.app.publisher.enqueue("testnet", "#test", ["[CS2 LIVE] Vitality 1-0 Spirit"], priority=8))
    be.call(be.app.publisher.enqueue("testnet", "someuser", ["private notice"], kind="NOTICE"))
    tcl.eval("set ::OUT {}")
    tcl.eval("::cs2intel::poll")
    tcl.eval("wait_out")
    lines = out(tcl)
    assert "PRIVMSG #test :[CS2 LIVE] Vitality 1-0 Spirit" in lines
    assert "NOTICE someuser :private notice" in lines


def test_backend_down_then_recovers(bridge):
    tcl, _be, port = bridge
    tcl.eval(f'set cs2intel(api_url) "http://127.0.0.1:{free_port()}"')  # port with nothing listening
    for _ in range(3):
        run_pub(tcl, "version")
    assert "not available" in out(tcl)[0] and out(tcl)[0].startswith("NOTICE alice")
    log = " ".join(tcl.splitlist(tcl.eval("set ::LOG")))
    assert "backend NOT available" in log
    tcl.eval(f'set cs2intel(api_url) "http://127.0.0.1:{port}"')  # the backend "comes back"
    run_pub(tcl, "version")
    assert out(tcl)[0].startswith("PRIVMSG #test :CS2 Intelligence")
    assert "backend recovered" in " ".join(tcl.splitlist(tcl.eval("set ::LOG")))


def test_no_poll_when_disconnected_from_irc(bridge):
    tcl, be, _ = bridge
    be.call(be.app.publisher.enqueue("testnet", "#test", ["must not be lost"]))
    tcl.eval('set ::server ""')
    tcl.eval("::cs2intel::poll")
    time.sleep(0.3)
    tcl.eval("update")
    rows = be.call(be.app.publisher.fetch("testnet"))  # still pending: not collected without an IRC connection
    assert [r.text for r in rows] == ["must not be lost"]
