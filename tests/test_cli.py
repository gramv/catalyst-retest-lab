import json
import stat

from catalyst_lab.cli import main
from tests.conftest import NOW


def test_local_bootstrap_creates_private_token(cluster, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MUSE_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["catalyst-lab", "dev-init", "--local-dir", str(cluster)])
    main()
    token = cluster / "muse-token"
    assert len(token.read_text()) >= 32
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_export_and_offline_verification(
    cluster, repo, raw, evidence, policy, monkeypatch, tmp_path, capsys
):
    repo.submit(raw, NOW, lambda c, now: evidence, policy)
    monkeypatch.setenv("DATABASE_URL", repo.database_url)
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr("sys.argv", ["catalyst-lab", "export", "--file", str(path)])
    main()
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    assert manifest["event_count"] > 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv",
        ["catalyst-lab", "verify", "--file", str(path), "--expected-head", manifest["head_hash"]],
    )
    main()
    assert json.loads(capsys.readouterr().out)["valid"]
