import json
import os
import sys
import time
from pathlib import Path

from app.services.organize_ownership import OwnershipBusyError, plan_lock

from app.services.organize_executor import ExecOp, run_execution

root = Path("/locks")
plan_id = "46d368dd-24a7-4794-b7ee-5807c869c3b5"
mode = sys.argv[1]
if mode == "hold":
    with plan_lock(root, plan_id):
        (root / "source.mkv").write_bytes(b"c" * 1048577)
        (root / "ready").touch()
        time.sleep(120)
elif mode == "check":
    assert (root / "ready").exists()
    try:
        with plan_lock(root, plan_id):
            raise AssertionError("another container acquired the live owner's lock")
    except OwnershipBusyError:
        pass
    assert not (root / "target.mkv").exists()
    print(json.dumps({"cross_container_live_owner_rejected": True}))
elif mode == "recover":
    with plan_lock(root, plan_id):
        result = run_execution([ExecOp(
            op_type="move", src=str(root / "source.mkv"), dst=str(root / "target.mkv"), size=1048577,
        )], file_op="hardlink")
        assert result.ok, result.error
        assert os.path.samefile(root / "source.mkv", root / "target.mkv")
    print(json.dumps({"cross_container_recovery_after_sigkill": True, "real_hardlink": True}))
else:
    raise ValueError(mode)
