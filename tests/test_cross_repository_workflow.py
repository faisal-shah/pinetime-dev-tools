from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/cross-repository.yml"


def test_ref_resolution_uses_the_workflow_token_without_an_embedded_authorization_header() -> None:
    workflow = WORKFLOW.read_text()

    assert "GH_TOKEN: ${{ github.token }}" in workflow
    assert "gh api" in workflow
    assert "Authorization:" not in workflow
    assert "******" not in workflow
