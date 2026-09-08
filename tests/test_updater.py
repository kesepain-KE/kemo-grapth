from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from starlette.requests import Request

import start
from api.routes import _is_loopback_host, post_update_apply
from api.schemas import UpdateApplyRequest
from core.jobs import MaintenanceJobManager
from update import (
    ApplicationUpdater,
    SemanticVersion,
    UpdateError,
    UpdatePermissionError,
    UpdateSourceError,
    migrate_legacy_runtime,
)


def _load_update_entry():
    path = Path(__file__).resolve().parents[1] / "update.py"
    spec = importlib.util.spec_from_file_location("kemo_graph_update_entry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path: Path, version: str = "1.0.0") -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "version.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    return root


def _runner(stdout: str = ""):
    def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return run


def test_semver_precedence_and_validation() -> None:
    assert SemanticVersion.parse("1.2.0") > SemanticVersion.parse("1.1.9")
    assert SemanticVersion.parse("1.0.0") > SemanticVersion.parse("1.0.0-rc.1")
    assert SemanticVersion.parse("1.0.0-rc.2") > SemanticVersion.parse("1.0.0-rc.1")
    assert SemanticVersion.parse("1.0.0+build.2") == SemanticVersion.parse(
        "1.0.0+build.1"
    )
    with pytest.raises(ValueError):
        SemanticVersion.parse("v1")


def test_check_finds_new_version_and_persists_state(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".git").mkdir()
    updater = ApplicationUpdater(
        root,
        fetch_json=lambda _url, _timeout: {"version": "1.1.0"},
        command_runner=_runner(),
    )

    status = updater.check()

    assert status["current_version"] == "1.0.0"
    assert status["latest_version"] == "1.1.0"
    assert status["update_available"] is True
    assert status["can_apply"] is True
    assert json.loads(
        (root / "update" / "runtime" / "state.json").read_text(encoding="utf-8")
    )[
        "latest_version"
    ] == "1.1.0"


def test_same_version_check_offers_force_update(tmp_path: Path) -> None:
    root = _project(tmp_path, version="1.2.0")
    (root / ".git").mkdir()
    updater = ApplicationUpdater(
        root,
        fetch_json=lambda _url, _timeout: {"version": "1.2.0"},
        command_runner=_runner(),
    )

    status = updater.check()

    assert status["update_available"] is False
    assert status["force_update_available"] is True
    assert status["can_apply"] is False
    assert status["can_force_apply"] is True


def test_status_recomputes_same_version_flags_from_saved_latest_version(tmp_path: Path) -> None:
    root = _project(tmp_path, version="1.3.0")
    (root / ".git").mkdir()
    updater = ApplicationUpdater(root, command_runner=_runner())
    state_path = root / "update" / "runtime" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "latest_version": "1.2.0",
                "update_available": False,
                "force_update_available": True,
                "phase": "idle",
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    status = updater.status()

    assert status["update_available"] is False
    assert status["force_update_available"] is False
    assert status["can_force_apply"] is False


def test_same_version_force_apply_rebuilds_without_version_bump(tmp_path: Path) -> None:
    root = _project(tmp_path, version="1.2.0")
    (root / ".git").mkdir()

    def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"version": "1.2.0"}),
                stderr="",
            )
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="abc123", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    updater = ApplicationUpdater(
        root,
        fetch_json=lambda _url, _timeout: {"version": "1.2.0"},
        command_runner=run,
    )

    result = updater.apply(force=True)

    assert result["updated"] is True
    assert result["forced"] is True
    assert result["current_version"] == "1.2.0"


def test_update_failure_after_merge_attempts_safe_git_rollback(tmp_path: Path) -> None:
    root = _project(tmp_path, version="1.2.0")
    (root / ".git").mkdir()
    config_path = root / "config" / "config.json"
    config_path.parent.mkdir()
    original_config = b'{"user": true}\n'
    config_path.write_bytes(original_config)
    (root / "requirements.txt").write_text("example-package==0.0.0\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        del cwd
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="old-head", stderr="")
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"version": "1.2.0"}),
                stderr="",
            )
        if command[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "merge"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command and command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="pip failed")

    updater = ApplicationUpdater(
        root,
        fetch_json=lambda _url, _timeout: {"version": "1.2.0"},
        command_runner=run,
    )
    updater._git_bytes = lambda _revision_path: b'{"default": true}\n'  # type: ignore[method-assign]

    with pytest.raises(UpdateError, match="命令执行失败"):
        updater.apply(force=True)

    assert ["git", "reset", "--hard", "old-head"] in commands
    assert config_path.read_bytes() == original_config


def test_root_update_entry_prompts_for_same_version_force(monkeypatch, capsys) -> None:
    entry = _load_update_entry()
    calls: list[bool] = []

    class FakeUpdater:
        def check(self):
            return {
                "current_version": "1.2.0",
                "latest_version": "1.2.0",
                "update_available": False,
                "force_update_available": True,
                "can_force_apply": True,
            }

        def apply(self, *, force=False):
            calls.append(force)
            return {"updated": True, "forced": force}

    monkeypatch.setattr(entry, "ApplicationUpdater", FakeUpdater)
    monkeypatch.setattr("builtins.input", lambda: "y")

    assert entry.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"] == {"updated": True, "forced": True}
    assert calls == [True]


def test_legacy_runtime_is_migrated_without_overwriting(tmp_path: Path) -> None:
    root = _project(tmp_path)
    legacy = root / ".update"
    legacy.mkdir()
    (legacy / "state.json").write_text('{"phase":"idle"}', encoding="utf-8")

    runtime = migrate_legacy_runtime(root)

    assert runtime == root / "update" / "runtime"
    assert (runtime / "state.json").read_text(encoding="utf-8") == '{"phase":"idle"}'
    assert not legacy.exists()


def test_remote_failure_is_not_reported_as_up_to_date(tmp_path: Path) -> None:
    root = _project(tmp_path)

    def fail(_url: str, _timeout: float):
        raise RuntimeError("offline")

    updater = ApplicationUpdater(root, fetch_json=fail)
    with pytest.raises(UpdateSourceError, match="检查 GitHub 更新失败"):
        updater.check()
    assert updater.status()["phase"] == "failed"
    assert updater.status()["error"]


def test_runtime_and_user_config_do_not_make_worktree_dirty(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".git").mkdir()
    porcelain = (
        " M config/config.json\0?? data/index.bin\0?? external/a.md\0"
        "?? log/a.tsv\0?? output/a.json\0?? tmp/a.tmp\0"
        "?? update/runtime/state.json\0"
    )
    updater = ApplicationUpdater(root, command_runner=_runner(porcelain))

    status = updater.status()

    assert status["dirty_files"] == []
    assert status["worktree_clean"] is True


def test_program_changes_block_update(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".git").mkdir()
    updater = ApplicationUpdater(
        root,
        command_runner=_runner(" M core/rag_engine.py\0?? tests/new_test.py\0"),
    )
    status = updater.status()
    assert status["dirty_files"] == ["core/rag_engine.py", "tests/new_test.py"]
    assert status["blocking_reasons"] == ["工作区包含未提交的程序文件修改"]


def test_user_config_wins_while_new_defaults_are_added(tmp_path: Path) -> None:
    root = _project(tmp_path)
    config = root / "config" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"rag": {"chunk_size": 1000, "new_option": True}, "theme": "white"}),
        encoding="utf-8",
    )
    updater = ApplicationUpdater(root)
    old_user = json.dumps(
        {"rag": {"chunk_size": 600}, "api_key": "secret"}
    ).encode()

    updater._merge_user_config(config, old_user)

    merged = json.loads(config.read_text(encoding="utf-8"))
    assert merged == {
        "rag": {"chunk_size": 600, "new_option": True},
        "theme": "white",
        "api_key": "secret",
    }


def test_cli_version_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert start.main(["version"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["version"] == "1.3.1"


def test_update_apply_loopback_guard() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("192.168.1.10") is False


def _request_for_update(host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/update/apply",
            "raw_path": b"/api/v1/update/apply",
            "query_string": b"",
            "headers": [],
            "client": (host, 8000),
            "server": ("testserver", 8000),
            "scheme": "http",
            "http_version": "1.1",
            "root_path": "",
        }
    )


class _UpdateStatusStub:
    def __init__(self, status: dict[str, object]) -> None:
        self._status = status

    def status(self) -> dict[str, object]:
        return self._status


class _UpdateJobsStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def submit(self, kind: str, **options: object) -> dict[str, object]:
        self.calls.append((kind, options))
        return {"job_id": "update-test", "kind": kind, **options}


def test_update_apply_accepts_legacy_empty_request_for_new_version() -> None:
    updater = _UpdateStatusStub(
        {
            "update_available": True,
            "force_update_available": False,
            "can_apply": True,
            "can_force_apply": False,
            "blocking_reasons": [],
        }
    )
    jobs = _UpdateJobsStub()

    result = post_update_apply(_request_for_update(), updater, jobs)

    assert result["ok"] is True
    assert jobs.calls == [("update", {})]


def test_update_apply_accepts_same_version_force_body_and_query() -> None:
    updater = _UpdateStatusStub(
        {
            "update_available": False,
            "force_update_available": True,
            "can_apply": False,
            "can_force_apply": True,
            "blocking_reasons": [],
        }
    )
    jobs = _UpdateJobsStub()

    body_result = post_update_apply(
        _request_for_update(),
        updater,
        jobs,
        UpdateApplyRequest(force=True),
    )
    query_result = post_update_apply(
        _request_for_update(),
        updater,
        jobs,
        force=True,
    )

    assert body_result["ok"] is True
    assert query_result["ok"] is True
    assert jobs.calls == [
        ("update", {"force": True}),
        ("update", {"force": True}),
    ]


def test_update_apply_rejects_non_loopback_even_when_force_requested() -> None:
    updater = _UpdateStatusStub(
        {
            "update_available": False,
            "force_update_available": True,
            "can_apply": False,
            "can_force_apply": True,
            "blocking_reasons": [],
        }
    )
    jobs = _UpdateJobsStub()

    with pytest.raises(UpdatePermissionError, match="只允许从本机访问"):
        post_update_apply(
            _request_for_update("192.168.1.5"),
            updater,
            jobs,
            force=True,
        )
    assert jobs.calls == []


def test_update_job_forwards_force_flag_to_updater(tmp_path: Path) -> None:
    class _Updater:
        def __init__(self) -> None:
            self.forced: list[bool] = []

        def apply(self, *, progress, force: bool = False):
            self.forced.append(force)
            progress(0.5, "forced test")
            return {"forced": force}

    updater = _Updater()
    manager = MaintenanceJobManager(
        lambda: None,
        data_dir=tmp_path / "data",
        updater_factory=lambda: updater,
    )
    completed: list[dict[str, object]] = []
    failures: list[str] = []
    manager._set_running = lambda _job_id: None  # type: ignore[method-assign]
    manager._set_progress = lambda _job_id, _value, _detail: None  # type: ignore[method-assign]
    manager._complete = lambda _job_id, result: completed.append(result)  # type: ignore[method-assign]
    manager._fail = lambda _job_id, error: failures.append(error)  # type: ignore[method-assign]

    manager._run("job-force", "update", {"force": True})

    assert failures == []
    assert updater.forced == [True]
    assert completed == [{"forced": True}]
