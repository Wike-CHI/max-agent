from __future__ import annotations

from pathlib import Path

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
