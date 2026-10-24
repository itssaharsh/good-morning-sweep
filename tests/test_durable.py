"""The durable scan against a real local Temporal server (the CLI's dev server), with a fake model."""
import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest

from sweep import durable, model, pull
from conftest import add, card

TEMPORAL = os.environ.get("TEMPORAL_CLI", str(Path.home() / ".local/opt/temporal"))


@pytest.mark.skipif(not Path(TEMPORAL).is_file(), reason="Temporal CLI not installed")
def test_workflow_classifies_everything_and_retries_while_the_model_is_down(con, tmp_path, monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    for i in range(4):
        add(con, card(tmp_path / f"c{i}.jpg", seed=30 + i), "wa_images")
    pull.index(con)
    calls = {"n": 0}

    def flaky(jpeg, url=None, wait_s=None, log=None, **k):
        calls["n"] += 1
        if calls["n"] == 2:                      # the server falls over once
            raise model.ModelDown("connection refused")
        return {"kind": "greeting", "words": "Good Morning", "people": False, "screen": "", "raw": "{}", "ms": 1,
                "error": None}
    monkeypatch.setattr(model, "classify", flaky)
    monkeypatch.setattr(durable, "MODEL_RETRY", durable.RetryPolicy(initial_interval=durable.timedelta(milliseconds=50),
                                                                    maximum_attempts=0))

    async def go():
        env = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL)
        try:
            return await durable.run(tmp_path / "sweep.db", "http://x", f"t-{uuid.uuid4()}", client=env.client,
                                     log=lambda *a: None)
        finally:
            await env.shutdown()
    done = asyncio.run(go())
    assert done == 4 and calls["n"] == 5
    assert con.execute("SELECT COUNT(*) FROM verdicts WHERE kind='greeting'").fetchone()[0] == 4
    assert con.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 4
