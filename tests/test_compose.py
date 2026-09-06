import os
import pathlib

import pytest

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
    compose.exec_service(
        proj, "db", "psql", "-d", "x", stdout_file=marker_out, stdin_file=marker_in, check=True
    )

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

    rc = compose.run_with_stdin_stream(proj, ["exec", "-T", "db", "psql"], [b"SELECT 1;\n", b"SELECT 2;\n"])
    assert rc == 0
    assert written == [b"SELECT 1;\n", b"SELECT 2;\n"]

    import pytest

    with pytest.raises(compose.DockerError) as exc:
        compose.run_with_stdin_stream(proj, ["exec", "-T", "db", "psql"], [b"junk\n"])
    assert "bad sql" in str(exc.value)


def test_find_compose_file_uses_docker_precedence(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    assert compose.find_compose_file(d) is None
    (d / "docker-compose.yml").write_text("services: {}")
    assert compose.find_compose_file(d).name == "docker-compose.yml"
    (d / "compose.yaml").write_text("services: {}")
    assert compose.find_compose_file(d).name == "compose.yaml"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_find_compose_file_returns_none_for_unreadable_dir(tmp_path):
    # Bind-mount data folders owned by root: on Python <=3.12, stat() on the
    # candidates raises EACCES instead of returning False.
    d = tmp_path / "locked"
    d.mkdir()
    (d / "compose.yaml").write_text("services: {}")
    d.chmod(0)
    try:
        assert compose.find_compose_file(d) is None
    finally:
        d.chmod(0o700)


def test_find_compose_file_skips_inaccessible_candidates(tmp_path, monkeypatch):
    # Same bug without depending on Python's pathlib version: an EACCES on one
    # candidate must not abort the lookup of the remaining names.
    d = tmp_path / "proj"
    d.mkdir()
    (d / "compose.yaml").write_text("services: {}")
    (d / "docker-compose.yml").write_text("services: {}")
    real_is_file = pathlib.Path.is_file

    def raising_is_file(self):
        if self.name == "compose.yaml":
            raise PermissionError(13, "Permission denied")
        return real_is_file(self)

    monkeypatch.setattr(pathlib.Path, "is_file", raising_is_file)
    assert compose.find_compose_file(d).name == "docker-compose.yml"


def test_base_accepts_compose_yaml_and_explains_when_missing(tmp_path):
    import pytest

    d = tmp_path / "proj"
    d.mkdir()
    with pytest.raises(compose.DockerError) as exc:
        compose._base(d)
    assert "compose.yaml" in str(exc.value) and "docker-compose.yml" in str(exc.value)

    (d / "compose.yaml").write_text("services: {}")
    cmd = compose._base(d)
    assert cmd[cmd.index("-f") + 1] == str(d / "compose.yaml")
