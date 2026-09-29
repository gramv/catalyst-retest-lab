import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_lab.jev_benchmark_cases import get_cases
from catalyst_lab.jev_contract import digest


def test_interrupted_run_records_missing_votes_and_stops_private_database(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "scripts" / "run_jev_validation.py"
    spec = importlib.util.spec_from_file_location("validation_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    cases = get_cases()[:2]
    blind = [{"case_id": c["case_id"], "state": c["state"]} for c in cases]
    labels = {c["case_id"]: {"labels": c["reference"]} for c in cases}
    for name, value in (("cases", cases), ("blind", blind), ("labels", labels)):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    provenance = {
        "blind_packet_sha256": digest((tmp_path / "blind.json").read_bytes()),
        "independent_labels_sha256": digest((tmp_path / "labels.json").read_bytes()),
    }
    (tmp_path / "provenance.json").write_text(json.dumps(provenance))
    args = SimpleNamespace(
        cases=tmp_path / "cases.json", blind_cases=tmp_path / "blind.json",
        independent_labels=tmp_path / "labels.json",
        review_provenance=tmp_path / "provenance.json",
        output=tmp_path / "output", database_root=tmp_path / "database", arm="diagnostic",
    )
    stopped = []
    monkeypatch.setattr(runner.localdb, "start", lambda root: root.mkdir())
    monkeypatch.setattr(runner.localdb, "stop", lambda root: stopped.append(root))
    monkeypatch.setattr(runner, "JevStore", lambda _: SimpleNamespace(
        circuit_open=lambda *args: False,
    ))

    class InterruptedReviewer:
        def __init__(self, *_):
            pass

        async def jev_review(self, **_):
            raise RuntimeError("INTERRUPTED_TEST")

    monkeypatch.setattr(runner, "JevReviewer", InterruptedReviewer)

    class EmptyRepository:
        def __init__(self, *_):
            pass

        def export_events(self):
            return []

        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, _):
            return self

        def fetchall(self):
            return []

        def fetchone(self):
            return {"n": 0}

    monkeypatch.setattr(runner, "Repository", EmptyRepository)
    with pytest.raises(RuntimeError, match="INTERRUPTED_TEST"):
        asyncio.run(runner.run(args))
    assert stopped == [args.database_root]
    assert json.loads((args.output / "results.json").read_text()) == []
    metrics = json.loads((args.output / "metrics.json").read_text())
    assert sum(v["missing_results"] for k, v in metrics["groups"].items()
               if k.endswith("/ALL")) == 2
    assert json.loads((args.output / "stopped.json").read_text())["completed"] == 0
