"""1.0.147 (F3.17): abstract-claude-serve-run provisions the serve venv BEFORE it bakes the UI.

The bake derives the console UI from the abstract-serve-core that $AC resolves to;
baking first shipped the previous package's UI after every pin bump.
"""
from pathlib import Path

RUN = Path(__file__).resolve().parents[1] / "resources" / "bin" / "abstract-claude-serve-run"


def test_provision_runs_before_bake():
    src = RUN.read_text()
    prov = src.index('"$_prov" "$_acvenv"')
    bake = src.index('"$_bake" "$AC_UI_DIST"')
    assert prov < bake, "station-provision-venv must run before station-bake-webui"
    assert "non-fatal" in src[prov:prov + 200] and "non-fatal" in src[bake:bake + 200]
