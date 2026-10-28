"""Offline workflow contracts, not a local Xcode build or binary acceptance."""
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "macos/ConnectOnionStudio/ConnectOnionStudio.xcodeproj"
LOCK = PROJECT / "project.xcworkspace/xcshareddata/swiftpm/Package.resolved"


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/native-build.yml").read_text())


def test_native_smoke_trigger_permissions_and_concurrency_are_bounded():
    spec = workflow()
    assert set(spec["on"]) == {"pull_request", "workflow_dispatch"}
    assert spec["permissions"] == {"contents": "read"}
    assert spec["concurrency"]["cancel-in-progress"] is True
    assert "${{ github.ref }}" in spec["concurrency"]["group"]
    assert set(spec["jobs"]) == {"macos-compile"}
    job = spec["jobs"]["macos-compile"]
    assert job["runs-on"] == "macos-latest"
    assert job["timeout-minutes"] == 15
    assert "permissions" not in job and "environment" not in job
    assert job["env"] == {"GIT_TERMINAL_PROMPT": "0"}


def test_checkout_is_same_immutable_revision_as_existing_regression_and_no_credentials():
    spec = workflow()
    actions = [step for step in spec["jobs"]["macos-compile"]["steps"] if "uses" in step]
    assert len(actions) == 1
    checkout, = actions
    existing = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    expected = next(step["uses"] for step in existing["jobs"]["regression"]["steps"]
                    if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["uses"] == expected
    assert re.fullmatch(r"actions/checkout@[a-f0-9]{40}", checkout["uses"])
    assert checkout["with"] == {"persist-credentials": False}
    text = (ROOT / ".github/workflows/native-build.yml").read_text()
    assert "secrets." not in text and "id-token:" not in text
    assert "pull_request_target" not in text


def test_build_is_debug_unsigned_locked_and_does_not_package_publish_or_execute_app():
    steps = workflow()["jobs"]["macos-compile"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "-project macos/ConnectOnionStudio/ConnectOnionStudio.xcodeproj" in commands
    assert "-scheme ConnectOnionStudio" in commands and "-configuration Debug" in commands
    assert "-destination 'platform=macOS'" in commands
    assert "CODE_SIGNING_ALLOWED=NO" in commands and "CODE_SIGNING_REQUIRED=NO" in commands
    assert "-disableAutomaticPackageResolution" in commands
    assert "-onlyUsePackageVersionsFromResolvedFile" in commands
    assert '"$RUNNER_TEMP/studio-native-derived-data"' in commands
    assert '"$RUNNER_TEMP/studio-native-source-packages"' in commands
    assert any(step.get("run", "").rstrip().endswith("build") for step in steps)
    for forbidden in ("-allowProvisioningUpdates", "-exportArchive", " archive", "notarytool",
                      "codesign ", "hdiutil", "gh release", "git push", "python", "pip install",
                      "build-dmg", "bundle-python", "curl ", "wget ", "osascript", "open -a"):
        assert forbidden not in commands
    final = steps[-1]
    assert final["if"] == "always()"
    assert final["run"] == "git diff --exit-code -- " + str(LOCK.relative_to(ROOT))


def test_workflow_shell_fragments_have_valid_bash_syntax_only():
    # bash -n parses but does not execute commands, download packages or invoke Xcode.
    for step in workflow()["jobs"]["macos-compile"]["steps"]:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                                    capture_output=True, timeout=5)
            assert result.returncode == 0, result.stderr


def test_scheme_and_lock_exist_without_hidden_packaging_actions():
    scheme = ET.parse(PROJECT / "xcshareddata/xcschemes/ConnectOnionStudio.xcscheme").getroot()
    references = scheme.findall("./BuildAction/BuildActionEntries/BuildActionEntry/BuildableReference")
    assert len(references) == 1
    assert references[0].attrib["BlueprintName"] == "ConnectOnionStudio"
    assert references[0].attrib["BuildableName"] == "ConnectOnionStudio.app"
    assert not scheme.findall(".//ExecutionAction")
    project = (PROJECT / "project.pbxproj").read_text()
    assert "PBXShellScriptBuildPhase" not in project
    assert "shellScript =" not in project
    lock = json.loads(LOCK.read_text())
    assert lock["version"] == 3 and lock["pins"]
    for pin in lock["pins"]:
        assert pin["kind"] == "remoteSourceControl"
        assert re.fullmatch(r"[0-9a-f]{40}", pin["state"]["revision"])
        assert pin["state"].get("version") and "branch" not in pin["state"]


def test_readme_distinguishes_native_smoke_from_full_bundle_acceptance():
    readme = (ROOT / "README.md").read_text()
    assert ".github/workflows/native-build.yml" in readme
    assert "native shell compilation only" in readme
    assert "not full bundled-runtime acceptance" in readme
