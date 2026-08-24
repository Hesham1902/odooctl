from odooctl import compose


class FakeProc:
    returncode = 0
    stdout = b""
    stderr = b""


def _proj(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}")
    return d


def test_exec_service_forwards_stdin_and_stdout_files(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, stdout=None, stderr=None, stdin=None, timeout=None):
        seen.update(cmd=cmd, stdout=stdout, stderr=stderr, stdin=stdin)
        return FakeProc()

    monkeypatch.setattr(compose.subprocess, "run", fake_run)
    proj = _proj(tmp_path)

    marker_out = object()
    marker_in = object()
    compose.exec_service(proj, "db", "psql", "-d", "x",
                         stdout_file=marker_out, stdin_file=marker_in, check=True)

    assert seen["cmd"][:2] == ["docker", "compose"]
    assert "exec" in seen["cmd"] and "-T" in seen["cmd"] and "db" in seen["cmd"]
    assert seen["stdout"] is marker_out
    assert seen["stdin"] is marker_in


def test_run_uses_stdin_file_without_capture(tmp_path, monkeypatch):
    seen = {}
    fh = object()

    def fake_run(cmd, stdout=None, stderr=None, stdin=None, timeout=None):
        seen.update(cmd=cmd, stdin=stdin)
        return FakeProc()

    monkeypatch.setattr(compose.subprocess, "run", fake_run)
    proj = _proj(tmp_path)
    compose.run(proj, "exec", "-T", "db", "psql", capture=False, stdin_file=fh)
    assert seen["stdin"] is fh


def test_run_with_stdin_stream_pumps_chunks_and_raises_on_error(tmp_path, monkeypatch):
    proj = _proj(tmp_path)
    written = []
    stderr_bytes = b"ERROR: bad sql\nDETAIL: boom"

    class FakeStdin:
        def write(self, data):
            written.append(data)

        def close(self):
            pass

    class FakeProc:
        def __init__(self, rc):
            self.stdin = FakeStdin()
            self._rc = rc

        def wait(self):
            return self._rc

        def kill(self):
            pass

    procs = [FakeProc(0), FakeProc(1)]

    class FakeTempFile:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def seek(self, pos):
            pass

        def read(self):
            return stderr_bytes

    monkeypatch.setattr(compose.subprocess, "Popen", lambda *a, **kw: procs.pop(0))
    monkeypatch.setattr(compose.tempfile, "TemporaryFile", FakeTempFile)

    rc = compose.run_with_stdin_stream(proj, ["exec", "-T", "db", "psql"],
                                       [b"SELECT 1;\n", b"SELECT 2;\n"])
    assert rc == 0
    assert written == [b"SELECT 1;\n", b"SELECT 2;\n"]

    import pytest

    with pytest.raises(compose.DockerError) as exc:
        compose.run_with_stdin_stream(proj, ["exec", "-T", "db", "psql"], [b"junk\n"])
    assert "bad sql" in str(exc.value)
