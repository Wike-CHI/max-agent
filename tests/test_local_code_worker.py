from __future__ import annotations

from pathlib import Path

import pytest

from max_agent.kernel.scheduler import Scheduler
from max_agent.kernel.service import TaskService
from max_agent.models.enums import TaskStatus, WorkerKind
from max_agent.models.schemas import TaskCreate


def test_local_code_write_and_pytest(session, tmp_path: Path):
    # use /tmp which LocalCodeWorker allows
    demo = Path("/tmp/max_agent_mvp_demo.py")
    testf = Path("/tmp/test_max_agent_mvp_demo.py")
    for p in (demo, testf):
        if p.exists():
            p.unlink()

    svc = TaskService(session)
    t = svc.create(
        TaskCreate(
            goal="write adder and verify with pytest",
            acceptance_criteria=["worker_ok", "file_exists:/tmp/max_agent_mvp_demo.py"],
            worker_kind=WorkerKind.LOCAL_CODE,
            worker_payload={
                "action": "write_and_pytest",
                "path": str(demo),
                "test_path": str(testf),
            },
        )
    )
    out = Scheduler(session).run_one(t.id)
    assert out.status == TaskStatus.SUCCEEDED, out.last_error
    assert demo.is_file()


def test_local_code_rejects_existing_test_outside_allowlist(session) -> None:
    forbidden = Path.home() / "max_agent_forbidden_test.py"
    marker = Path("/tmp/max_agent_forbidden_marker")
    if marker.exists():
        marker.unlink()
    forbidden.write_text(
        "from pathlib import Path\n\n"
        "def test_should_not_run():\n"
        "    Path('/tmp/max_agent_forbidden_marker').write_text('ran')\n",
        encoding="utf-8",
    )
    try:
        svc = TaskService(session)
        task = svc.create(
            TaskCreate(
                goal="pytest an existing file outside the allowlist",
                acceptance_criteria=["worker_ok"],
                worker_kind=WorkerKind.LOCAL_CODE,
                worker_payload={"action": "run_pytest", "test_path": str(forbidden)},
            )
        )
        out = Scheduler(session).run_one(task.id)
        assert out.status == TaskStatus.FAILED
        assert out.last_error and "refusing" in out.last_error
        assert not marker.exists()
    finally:
        forbidden.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def test_local_code_rejects_symlink_that_resolves_outside_allowlist(session) -> None:
    secret = Path.home() / "max_agent_secret_test.py"
    link = Path("/tmp/max_agent_test_link.py")
    marker = Path("/tmp/max_agent_symlink_marker")
    for path in (link, marker):
        if path.exists() or path.is_symlink():
            path.unlink()
    secret.write_text(
        "from pathlib import Path\n\n"
        "def test_should_not_run():\n"
        "    Path('/tmp/max_agent_symlink_marker').write_text('ran')\n",
        encoding="utf-8",
    )
    link.symlink_to(secret)
    try:
        svc = TaskService(session)
        task = svc.create(
            TaskCreate(
                goal="pytest through a symlink",
                acceptance_criteria=["worker_ok"],
                worker_kind=WorkerKind.LOCAL_CODE,
                worker_payload={"action": "run_pytest", "test_path": str(link)},
            )
        )
        out = Scheduler(session).run_one(task.id)
        assert out.status == TaskStatus.FAILED
        assert out.last_error and "refusing" in out.last_error
        assert not marker.exists()
    finally:
        secret.unlink(missing_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        marker.unlink(missing_ok=True)


def test_local_code_pytest_does_not_see_credential_env(session, monkeypatch: pytest.MonkeyPatch) -> None:
    demo = Path("/tmp/max_agent_env_demo.py")
    testf = Path("/tmp/test_max_agent_env_demo.py")
    for path in (demo, testf):
        if path.exists():
            path.unlink()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_LOCALCODESECRET123")
    monkeypatch.setenv("MAX_AGENT_DATABASE_URL", "sqlite:////prod/secret.db")
    testf.write_text(
        "import os\n\n"
        "def test_credentials_absent():\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n"
        "    assert 'MAX_AGENT_DATABASE_URL' not in os.environ\n",
        encoding="utf-8",
    )
    try:
        svc = TaskService(session)
        task = svc.create(
            TaskCreate(
                goal="pytest without host credentials",
                acceptance_criteria=["worker_ok"],
                worker_kind=WorkerKind.LOCAL_CODE,
                worker_payload={
                    "action": "run_pytest",
                    "path": str(demo),
                    "test_path": str(testf),
                },
            )
        )
        out = Scheduler(session).run_one(task.id)
        assert out.status == TaskStatus.SUCCEEDED, out.last_error
    finally:
        for path in (demo, testf):
            if path.exists():
                path.unlink()
