"""The overnight scan as a Temporal workflow, for a run that has to survive the night on a laptop.

The plain `sweep scan` already stores every answer as it arrives. This adds what a long unattended run needs: when the
model server crashes or runs out of memory, each picture's call is retried with back-off until it answers (it never
guesses), and when the laptop sleeps or the process is killed, running `sweep scan --durable` again re-attaches to the
same workflow, which carries on from the next picture. Temporal's web UI shows where the run is."""
import asyncio
import concurrent.futures
import dataclasses
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import db, guard, media, model, scan

TASK_QUEUE = "good-morning-sweep"


@dataclasses.dataclass
class ScanInput:
    db_path: str
    model_url: str
    batch: int = 20
    done: int = 0


@dataclasses.dataclass
class Item:
    sha1: str
    local_path: str
    also: list


@activity.defn
def next_batch(db_path: str, n: int) -> list:
    con = db.connect(db_path)
    try:
        todo = scan.pending(con)
        same = scan.lookalikes(con, todo)
        skip = {s for v in same.values() for s in v}
        todo = [r for r in todo if r["sha1"] not in skip][:n]
        return [Item(r["sha1"], r["local_path"], list(same.get(r["sha1"], ()))) for r in todo]
    finally:
        con.close()


@activity.defn
def classify_one(db_path: str, item: Item, model_url: str) -> str:
    """One picture. Raises while the model server is down, so Temporal retries with back-off."""
    im = media.open_picture(item.local_path, max_side=768)
    if im is None:
        v = {"kind": None, "words": "", "people": None, "screen": "", "raw": "", "ms": 0,
             "error": "could not open the picture"}
    else:
        v = model.classify(media.jpeg_bytes(im), url=model_url, wait_s=0, log=lambda *a: None)
    con = db.connect(db_path)
    try:
        scan.store(con, item.sha1, v, also=item.also)
    finally:
        con.close()
    return v["kind"] or "unsure"


@activity.defn
def build_suggestions(db_path: str) -> int:
    con = db.connect(db_path)
    try:
        return len(guard.build(con))
    finally:
        con.close()


MODEL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), backoff_coefficient=2.0,
                          maximum_interval=timedelta(minutes=5), maximum_attempts=0)


@workflow.defn
class SweepScan:
    def __init__(self):
        self.done = 0
        self.last = ""

    @workflow.query
    def progress(self) -> dict:
        return {"done": self.done, "last": self.last}

    @workflow.run
    async def run(self, inp: ScanInput) -> int:
        self.done = inp.done
        while True:
            items = await workflow.execute_activity(next_batch, args=[inp.db_path, inp.batch],
                                                    start_to_close_timeout=timedelta(minutes=5),
                                                    retry_policy=RetryPolicy(maximum_attempts=0))
            if not items:
                break
            for it in items:
                self.last = await workflow.execute_activity(classify_one, args=[inp.db_path, it, inp.model_url],
                                                            start_to_close_timeout=timedelta(minutes=6),
                                                            retry_policy=MODEL_RETRY)
                self.done += 1
            if workflow.info().get_current_history_length() > 3000:
                workflow.continue_as_new(dataclasses.replace(inp, done=self.done))
        await workflow.execute_activity(build_suggestions, inp.db_path, start_to_close_timeout=timedelta(minutes=10))
        return self.done


async def run(db_path, model_url, workflow_id, address="localhost:7233", log=print, client=None):
    from temporalio.client import Client, WorkflowFailureError
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker
    from temporalio.service import RPCError

    client = client or await Client.connect(address)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        async with Worker(client, task_queue=TASK_QUEUE, workflows=[SweepScan],
                          activities=[next_batch, classify_one, build_suggestions], activity_executor=pool):
            try:
                handle = await client.start_workflow(SweepScan.run, ScanInput(str(db_path), model_url),
                                                     id=workflow_id, task_queue=TASK_QUEUE)
                log(f"Started workflow {workflow_id}. Stop any time; run the same command to carry on.")
            except (WorkflowAlreadyStartedError, RPCError):
                handle = client.get_workflow_handle(workflow_id)
                log(f"Re-attached to workflow {workflow_id}: carrying on where it stopped.")
            return await handle.result()


def run_sync(db_path, model_url, workflow_id, address="localhost:7233", log=print):
    return asyncio.run(run(db_path, model_url, workflow_id, address, log))
