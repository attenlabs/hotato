"""atlas job: runs the existing `atlas` CLI (hotato-atlas package) with Gemini
on Vertex AI as the classifier. The hotato-atlas repo is not modified: the engine
class below is swapped in at runtime under the name the CLI already accepts.
"""
import os
import tempfile

from common import main_guard
import vertex_models as models

RESOLVED = {"model": None}  # set by work() from gates/policy.json models.atlas


class GeminiEngine:
    """Implements atlas.distill.Engine: name + run(prompt, context) -> raw text."""
    name = "gemini-vertex"

    def __init__(self, client=None, model=None):
        self.model = model or RESOLVED["model"] or os.environ.get("ATLAS_GEMINI_MODEL", "gemini-2.5-flash")
        self._client = client

    def available(self):
        return True

    def _c(self):
        if self._client is None:
            from google import genai  # google-genai, Vertex mode (ADC of the job's service account)
            self._client = genai.Client(vertexai=True, project=os.environ["GCP_PROJECT"],
                                        location=models.vertex_region())
        return self._client

    def run(self, prompt, context):
        try:
            resp = self._c().models.generate_content(
                model=self.model, contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0})
        except Exception as exc:
            from atlas.distill import EngineUnavailable
            raise EngineUnavailable(f"gemini call failed: {type(exc).__name__}") from exc
        return resp.text or ""


def install_engine(cls=GeminiEngine):
    """Replace the CLI-backed engine registered as 'claude' (the CLI's argparse only
    accepts auto|stub|claude) so `--engine claude` means Gemini in this job."""
    from atlas import distill
    distill.ENGINES["claude"] = cls
    return distill


def work(store, rec):
    RESOLVED["model"] = rec["model"] = models.resolve("atlas")
    from atlas import cli
    install_engine()
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "atlas.db")
        blob = store.get_bytes("atlas/atlas.db")
        if blob:
            open(db, "wb").write(blob)
        out = os.path.join(tmp, "site")
        argv = ["--db", db, "run", "--engine", "claude", "--agents", "stub", "--out", out]
        code = cli.main(argv)
        if os.path.exists(db):
            store.put_bytes("atlas/atlas.db", open(db, "rb").read())
    if code:
        raise RuntimeError(f"atlas exited {code}")
    return {"argv": argv[2:], "exit": code}


if __name__ == "__main__":
    main_guard("atlas", work)
