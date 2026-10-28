# Tested with stand-in targets and a local stand-in model host (tests/test_sec001.py, tests/test_e2e.py);
# it drove the live runs of 20 and 21 October 2026.
"""A thin wrapper that puts the TP-SEC-001 items (the items passed in; the default argument is the 20 spike items) and scorer into an Inspect task.

Inspect (`inspect_ai`, MIT licence) is imported inside `build_task`, so this module imports without it. The
dataset, the scorer and the measures are in `sec001.py` and are tested without Inspect. The wrapper adds only
the Inspect types around them: one sample for each item, one epoch for each of the k runs, a solver that sends
the item through the target interface, and a scorer that calls `sec001.score_response`.

The model calls do not go through Inspect. SRD-1 makes them through its own gateway, so Inspect needs no key
and runs with its model `none`. `spike.execute` drives the run through this task by default. With a
`runner`, each sample calls `runner.one`, which writes the signed run record; after a stop, the remaining
samples send nothing and score 0 with the note "not sent". The signed run log of `spike.py` is the evidence
record; the Inspect log is an additional file, named with its SHA-256 in the summary.
"""
from . import sec001


def build_task(target, runner=None, epochs=sec001.RUNS_PER_ITEM, items=sec001.ITEMS, model_name="srd1"):
    """Return an Inspect Task. `target` is an object with the `conversation` capability (open_session, send, close_session).

    With `runner` (spike._Runner, external._ExternalRunner or run_sec006._Runner), each sample is one run: the runner
    sends it, reads what the target shows, scores it and writes the run record. A runner that sets `last_score` (a
    dict with value, kind and explanation) gives the Inspect score; the TP-SEC-006 runner does. `items` may hold
    clean-baseline requests; they score 0 with the kind "baseline". `model_name` labels the output in the Inspect
    log (the adapter ID for an external run).
    """
    from inspect_ai import Task
    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.model import ModelOutput
    from inspect_ai.scorer import Score, mean, scorer
    from inspect_ai.solver import solver

    by_id = {i.id: i for i in items}
    dataset = MemoryDataset([
        Sample(input=i.text, id=i.id, metadata={"class": i.cls, "activity": i.activity, "objective": i.objective})
        for i in items])

    @solver
    def through_target():
        async def solve(state, generate):
            if runner is not None:
                reply = runner.one(target, by_id[state.sample_id], state.epoch)
                state.metadata["sent"] = reply is not None
                state.metadata["no_result"] = bool(getattr(runner, "last_no_result", False))
                state.metadata["runner_score"] = getattr(runner, "last_score", None)
                state.output = ModelOutput.from_content(model_name, reply or "")
                return state
            sid = target.open_session()
            try:
                reply = target.send(sid, state.input_text)
            finally:
                target.close_session(sid)
            state.output = ModelOutput.from_content(model_name, reply)
            return state
        return solve

    @scorer(metrics=[mean()])
    def sec001_scorer():
        async def score(state, target_):
            if state.metadata.get("sent") is False:
                return Score(value=0, answer="", explanation="not sent: the run stopped", metadata={"kind": "not sent"})
            if state.metadata.get("no_result"):
                return Score(value=0, answer=state.output.completion[:200], explanation="no result: left out of the rates "
                             "in the run log (a channel error, or an empty or cut-off reply)", metadata={"kind": "no_result"})
            rs = state.metadata.get("runner_score")
            if rs is not None:      # a runner that scores its own runs (TP-SEC-006): the same score as the run log
                return Score(value=rs["value"], answer=state.output.completion[:200], explanation=rs.get("explanation"),
                             metadata={"kind": rs["kind"]})
            if by_id[state.sample_id].cls == sec001.BASELINE:
                return Score(value=0, answer=state.output.completion[:200], explanation="clean baseline: recorded, not scored",
                             metadata={"kind": "baseline"})
            sc = sec001.score_response(by_id[state.sample_id], state.output.completion)
            return Score(value=1 if sc.success else 0, answer=state.output.completion[:200], explanation=sc.evidence or None,
                         metadata={"kind": sc.kind})
        return score

    return Task(dataset=dataset, solver=through_target(), scorer=sec001_scorer(), epochs=epochs)
