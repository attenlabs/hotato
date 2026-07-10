"""hotato start --stereo: the guided own-call first-run flow."""
import json
import os

from hotato import cli
from tests import _trial_audio as ta


def test_stereo_review_flow_writes_page_and_is_honest(tmp_path, capsys):
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    code = cli.main(["start", "--stereo", wav, "--dir", str(tmp_path), "--format", "json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ran"] and out["scorable"]
    assert out["review_page"] == "hotato-review.html"
    assert os.path.isfile(tmp_path / "hotato-review.html")
    assert out["total_candidates"] >= 1
    # honesty: it states what it does NOT prove, and offers no contract yet
    assert "does not prove" in out["does_not_prove"].lower()
    assert out["contract"] is None


def test_stereo_label_creates_contract_capped_at_measured(tmp_path, capsys):
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    code = cli.main(["start", "--stereo", wav, "--dir", str(tmp_path),
                     "--label", "yield", "--format", "json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["contract"] is not None
    # a single recording is MEASURED at most -- never a paired/attested proof
    assert out["contract"]["evidence_tier"] <= 2
    assert "PAIRED" not in out["contract"]["evidence_headline"]
    assert "ATTESTED" not in out["contract"]["evidence_headline"]
    # the contract bundle really exists
    found = list(tmp_path.rglob("contract.json"))
    assert found, "no contract.json created"


def test_stereo_not_scorable_exits_2(tmp_path, capsys):
    # silent caller channel -> not scorable
    wav = str(tmp_path / "silent.wav")
    ta.write_stereo(wav, caller_windows=[], agent_windows=[(0.2, 5.0)], total_sec=6.0)
    code = cli.main(["start", "--stereo", wav, "--dir", str(tmp_path), "--format", "json"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["scorable"] is False
