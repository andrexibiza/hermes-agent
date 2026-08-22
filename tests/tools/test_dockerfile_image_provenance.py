from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"


def test_official_image_bakes_unshadowable_provenance_marker():
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "/etc/hermes/image-provenance.json" in text
    assert '"deployment_kind": "image"' in text
    assert '"manager": "docker"' in text
    assert '"image": "nousresearch/hermes-agent"' in text
    assert "tomllib" in text
    assert 'os.environ.get("HERMES_GIT_SHA") or None' in text
    assert "chmod 0444 /etc/hermes/image-provenance.json" in text

    # The marker is deliberately outside both mutable state and the checkout:
    # a HERMES_HOME or /opt/hermes bind mount cannot erase the build fact.
    marker_write = text.index("/etc/hermes/image-provenance.json")
    home_env = text.index("ENV HERMES_HOME=/opt/data")
    assert marker_write < home_env
    assert "/opt/data/image-provenance.json" not in text
    assert "/opt/hermes/.hermes_image_provenance.json" not in text
