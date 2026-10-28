"""Public source must not depend on an individual developer's checkout."""

from pathlib import Path
import tomllib
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


def test_project_urls_match_repository_without_changing_author_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    base = "https://github.com/catRiceY/connectonion-studio"
    assert project["urls"] == {"Homepage": base, "Repository": base, "Issues": base + "/issues"}
    readme = (ROOT / "README.md").read_text()
    assert f"git clone {base}" in readme
    assert "https://github.com/EvanYifanYang/connectonion-studio" not in readme


def test_shared_macos_scheme_does_not_override_developer_interpreter():
    scheme = ROOT / "macos/ConnectOnionStudio/ConnectOnionStudio.xcodeproj/xcshareddata/xcschemes/ConnectOnionStudio.xcscheme"
    document = ElementTree.fromstring(scheme.read_text())
    assert not document.findall(".//EnvironmentVariable[@key='CO_STUDIO_PYTHON']")
    assert not document.findall(".//EnvironmentVariable[@key='CO_STUDIO_BIN']")
    assert "/Users/" not in scheme.read_text()


def test_brand_asset_default_uses_sibling_checkout_and_keeps_explicit_override():
    script = (ROOT / "scripts/sync_brand_assets.sh").read_text()
    assert 'DEFAULT_XCASSETS="$REPO_ROOT/../connectonion-ios/ConnectOnion iOS/Assets.xcassets"' in script
    assert '${1:-${IOS_XCASSETS:-$DEFAULT_XCASSETS}}' in script
    assert "/Users/" not in script
