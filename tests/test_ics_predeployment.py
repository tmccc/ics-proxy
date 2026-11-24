# Modified for the standalone iCS proxy repository.
"""Check when the normal release gate runs and reuses the additional iCS tests."""

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from tools import ics_predeployment as checks
from tools import validate
from tools.managed_reporting import software


# Commit a tiny source tree so the release gate must select real immutable candidate and predecessor bytes.
def commit(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return checks.git(repo, "rev-parse", "HEAD").decode().strip()


# Supply successive commits where only the final one changes the proxy.
@pytest.fixture
def releases(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for key, value in (("user.name", "Example"), ("user.email", "example@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    commit(repo, "requirements-trial.txt", "websockets==15.0.1\n")
    installed = commit(repo, "tools/managed_proxy.py", "# installed\n")
    client = commit(repo, "custom_components/ics_proxy/sensor.py", "# client change\n")
    candidate = commit(repo, "tools/managed_proxy.py", "# candidate\n")
    return repo, installed, client, candidate


# Intent: decide on the week from shipped code, with a real older Git commit as the source for the simulator.
# Why: an HA-only commit does not change the standalone build, while a new proxy must use its actual predecessor.
def test_proxy_change_detection_and_export(tmp_path, releases):
    repo, installed, client, candidate = releases
    assert checks.changed_proxy(repo, client, installed) is False
    assert checks.changed_proxy(repo, candidate, installed) is True
    with pytest.raises(ValueError, match="full installed proxy commit"):
        checks.changed_proxy(repo, candidate, installed[:12])
    exported = tmp_path / "installed"
    checks.export_installed(repo, installed, exported)
    assert (exported / "tools/managed_proxy.py").read_text() == "# installed\n"


# Intent: changing only the standalone installation's dependency pin triggers the installation week.
# Why: the service installs from a different requirements file than the offline test environment.
def test_proxy_change_detection_includes_standalone_dependency_pins(releases):
    repo, _, _, old_pin = releases
    new_pin = commit(repo, "requirements-trial.txt", "websockets==16.0\n")
    assert checks.changed_proxy(repo, new_pin, old_pin) is True


# Intent: a release subprocess imports fresh dependency source even if its normal cache contains older code.
# Why: -B disables writes but still reads previously installed bytecode, and -E ignores Python environment flags.
def test_release_check_reads_fresh_source(tmp_path):
    source = tmp_path / "source"
    tools = source / "tools"
    tools.mkdir(parents=True)
    module = tools / "example_dependency.py"
    module.write_text("value = 'stale'\n")
    py_compile.compile(
        str(module),
        cfile=str(tools / "__pycache__" / f"example_dependency.{sys.implementation.cache_tag}.pyc"),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )
    module.write_text("value = 'fresh'\n")
    (tools / checks.DISCOVERY.runner).write_text(
        "import os, sys, example_dependency\n"
        "assert sys.pycache_prefix == os.environ['PYTHONPYCACHEPREFIX']\n"
        "assert example_dependency.value == 'fresh'\n"
    )
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    checks.run_check(checks.DISCOVERY, sys.executable, source, attempt, None)
    assert not (attempt / "scratch/python-bytecode").exists()


# Intent: reused evidence follows dependency contents with stable versions and ignores generated import caches.
# Why: rebuilding or editing an installed dependency at the same version changes what the verifier runs.
def test_predeployment_runtime_identity_tracks_same_version_dependency_bytes(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    site.mkdir()
    dependency = site / "fixture_dependency.py"
    dependency.write_text("value = 1\n")
    dist = site / "fixture_dependency-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: fixture-dependency\nVersion: 1.0\n")
    (dist / "direct_url.json").write_text(json.dumps({"url": tmp_path.as_uri(), "dir_info": {}}))
    (site / "optional-2.0.dist-info").mkdir()
    cache = site / "__pycache__"
    cache.mkdir()

    # Report only the tiny fixture's import root, without starting or inspecting other installed runtimes.
    def metadata(command, **kwargs):
        assert command[1:5] == ["-E", "-P", "-B", "-X"]
        prefix = Path(command[5].removeprefix("pycache_prefix="))
        assert not list(prefix.iterdir())
        return json.dumps({"import_paths": [str(site)], "runtime_roots": [str(site)]})

    with monkeypatch.context() as runtime:
        runtime.setattr(checks.subprocess, "check_output", metadata)
        before = checks.runtime_identity(sys.executable, tmp_path)
        assert before["packages"] == [["fixture-dependency", "1.0"]]
        (cache / "fixture_dependency.pyc").write_bytes(b"cache only")
        assert checks.runtime_identity(sys.executable, tmp_path) == before
        dependency.write_text("value = 2\n")
        after = checks.runtime_identity(sys.executable, tmp_path)
    assert after["packages"] == before["packages"]
    assert after["runtime_sha256"] != before["runtime_sha256"]


# Intent: refuse editable metadata and startup markers even when the import roots remain unchanged.
# Why: editable finders can make external dependency source importable without listing it on sys.path.
@pytest.mark.parametrize("editable", ["metadata", "egg-link", "finder"])
def test_predeployment_runtime_rejects_editable_dependencies(tmp_path, monkeypatch, editable):
    site = tmp_path / "site-packages"
    site.mkdir()
    outside = tmp_path / "external-project"
    outside.mkdir()
    if editable == "metadata":
        dist = site / "fixture_dependency-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Name: fixture-dependency\nVersion: 1.0\n")
        (dist / "direct_url.json").write_text(json.dumps({"url": outside.as_uri(), "dir_info": {"editable": True}}))
    elif editable == "egg-link":
        (site / "fixture-dependency.egg-link").write_text(str(outside) + "\n.\n")
    else:
        (site / "__editable__.fixture_dependency-1.0.pth").write_text("import editable_fixture_finder\n")

    walk = Path.walk
    open_file = Path.open

    # Fail the test if release validation walks the editable source directory.
    def walk_installed(path, *args, **kwargs):
        assert path.is_relative_to(site)
        return walk(path, *args, **kwargs)

    # A single file read outside the installed runtime would also miss the intended boundary.
    def open_installed(path, *args, **kwargs):
        assert path.is_relative_to(site)
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as runtime:
        runtime.setattr(
            checks.subprocess,
            "check_output",
            lambda *args, **kwargs: json.dumps({"import_paths": [str(site)], "runtime_roots": [str(site)]}),
        )
        runtime.setattr(Path, "walk", walk_installed)
        runtime.setattr(Path, "open", open_installed)
        with pytest.raises(ValueError, match="editable Python installs are unsupported"):
            checks.runtime_identity(sys.executable, tmp_path)


# Intent: reject an unexpected import location before scanning any directory, including one added through a link.
# Why: a path added by site startup could otherwise cause release validation to walk unrelated data.
@pytest.mark.parametrize("linked", [False, True])
def test_predeployment_runtime_does_not_walk_outside_package_roots(tmp_path, monkeypatch, linked):
    site = tmp_path / "site-packages"
    extra = tmp_path / "unrelated"
    site.mkdir()
    extra.mkdir()
    if linked:
        link = site / "external"
        link.symlink_to(extra, target_is_directory=True)
        extra = link

    # Verify the bad import location is found before opening even a known package tree.
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected runtime directory traversal")

    with monkeypatch.context() as runtime:
        runtime.setattr(Path, "walk", forbidden)
        with pytest.raises(ValueError, match="unrecognized Python runtime import location"):
            checks.runtime_content_digest([str(site), str(extra)], [str(site)])


# Intent: hash within-runtime file links and refuse an escaping link before opening its target.
# Why: checking import directories alone cannot confine a linked dependency file.
def test_predeployment_runtime_file_links_cannot_escape_package_roots(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    stdlib = tmp_path / "stdlib"
    site.mkdir()
    stdlib.mkdir()
    target = stdlib / "fixture.py"
    target.write_text("value = 1\n")
    module = site / "fixture.py"
    module.symlink_to(target)
    paths, roots = [str(site)], [str(site), str(stdlib)]
    before = checks.runtime_content_digest(paths, roots)
    target.write_text("value = 2\n")
    assert checks.runtime_content_digest(paths, roots) != before

    external = tmp_path / "external.py"
    external.write_text("value = 3\n")
    module.unlink()
    module.symlink_to(external)

    # A refused link must leave its external file unread.
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected runtime file opened")

    with monkeypatch.context() as runtime:
        runtime.setattr(Path, "open", forbidden)
        with pytest.raises(ValueError, match="unrecognized Python runtime link"):
            checks.runtime_content_digest(paths, roots)


# Intent: bind the exact distribution startup and library link targets to the runtime fingerprint.
# Why: those installed files must affect evidence without making their directories import roots.
@pytest.mark.parametrize("layout", ["startup", "library"])
def test_predeployment_runtime_hashes_root_managed_distribution_links(tmp_path, monkeypatch, layout):
    lib = tmp_path / "usr/lib"
    config = tmp_path / "etc"
    stdlib = lib / "python3.14"
    stdlib.mkdir(parents=True)
    if layout == "startup":
        module = stdlib / "sitecustomize.py"
        target = config / "python3.14/sitecustomize.py"
        target.parent.mkdir(parents=True)
        module.symlink_to(target)
    else:
        module = stdlib / "config-3.14-x86_64-linux-gnu/libpython3.14.so"
        module.parent.mkdir()
        soname = lib / "x86_64-linux-gnu/libpython3.14.so.1"
        target = soname.with_name(soname.name + ".0")
        target.parent.mkdir()
        soname.symlink_to(target.name)
        module.symlink_to("../../x86_64-linux-gnu/libpython3.14.so.1")
    for directory in (lib, stdlib, target.parent, config if layout == "startup" else module.parent):
        directory.chmod(0o755)
    target.write_bytes(b"installed")
    target.chmod(0o644)
    lstat = Path.lstat
    walk = Path.walk
    untrusted = None

    # Emulate ownership of fixture system paths without requiring root to run the tests.
    def ownership(path):
        values = list(lstat(path))
        values[4] = 1000 if path == untrusted else 0
        return os.stat_result(values)

    # Only the approved import root may be walked, even though one target is read outside it.
    def walk_import_root(path, *args, **kwargs):
        assert path == stdlib
        return walk(path, *args, **kwargs)

    with monkeypatch.context() as runtime:
        runtime.setattr(checks, "SYSTEM_PYTHON_LIB", lib)
        runtime.setattr(checks, "SYSTEM_PYTHON_CONFIG", config)
        runtime.setattr(Path, "lstat", ownership)
        runtime.setattr(Path, "walk", walk_import_root)
        paths = [str(stdlib)]
        initial = checks.runtime_content_digest(paths, paths)
        target.write_bytes(b"changed")
        assert checks.runtime_content_digest(paths, paths) != initial

        target.chmod(0o664)
        with pytest.raises(ValueError, match="unrecognized Python runtime link"):
            checks.runtime_content_digest(paths, paths)
        target.chmod(0o644)
        untrusted = module
        with pytest.raises(ValueError, match="unrecognized Python runtime link"):
            checks.runtime_content_digest(paths, paths)
        untrusted = None
        other = target.with_suffix(".other")
        other.write_bytes(b"outside the recognized layout")
        module.unlink()
        module.symlink_to(other)
        with pytest.raises(ValueError, match="unrecognized Python runtime link"):
            checks.runtime_content_digest(paths, paths)


# Intent: the interpreter that runs the installation week has every candidate standalone pin before it can pass.
# Why: the old build's pins and a matching Home Assistant version cannot verify the candidate runtime by themselves.
@pytest.mark.parametrize(
    ("active_version", "websockets_version", "extra_version", "error"),
    [
        ("2026.10.4", "16.0", "2", None),
        ("2026.11.0", "16.0", "2", None),
        ("2026.11.0", "15.0.1", "2", "standalone runtime pins differ from the installation week runtime"),
        ("2026.11.0", "16.0", None, "standalone runtime pins differ from the installation week runtime"),
        ("2026.10.9", "16.0", "2", "installation week requires a pinned Home Assistant runtime"),
    ],
)
def test_installation_week_requires_candidate_standalone_pins(
    tmp_path, monkeypatch, active_version, websockets_version, extra_version, error
):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "requirements-trial.txt").write_text("websockets==16.0\nfixture_extra==2\n")
    (source / "requirements-ha-proxy.txt").write_text("homeassistant==2026.11.0\n")
    (source / "requirements-ha-proxy-2026.10.txt").write_text("homeassistant==2026.10.4\n")
    previous = tmp_path / "installed"
    previous.mkdir()
    (previous / "requirements-trial.txt").write_text("# same pins\nWebSockets==16.0\nfixture.extra==2\n")
    packages = [["homeassistant", active_version], ["WebSockets", websockets_version]]
    if active_version == "2026.10.9":
        packages.append(["homeassistant", "2026.11.0"])
    if extra_version is not None:
        packages.append(["fixture.extra", extra_version])

    # Keep the import selection independent of the order of installed package metadata.
    def identity(python, source, *, homeassistant):
        assert homeassistant
        return {"packages": sorted(packages), "homeassistant_version": active_version}

    monkeypatch.setattr(checks, "runtime_identity", identity)
    attempts = []

    # Retain one synthetic passing test report only when the candidate's pins admit the interpreter.
    def run(check, python, source, attempt, installed, *, ha_version, installed_python):
        assert installed == previous
        assert ha_version == active_version
        assert installed_python is None
        attempts.append(attempt)
        classname, name = check.case.rsplit(".", 1)
        (attempt / "pytest.xml").write_text(f'<testsuite><testcase classname="{classname}" name="{name}"/></testsuite>')

    monkeypatch.setattr(checks, "run_check", run)
    output = tmp_path / "evidence"
    args = (checks.WEEK, source, output, {"commit": "a", "source_digest": "b"}, sys.executable)
    if error is None:
        checks.ensure_check(*args, installed=("previous", previous))
        checks.ensure_check(*args, installed=("previous", previous))
        assert len(attempts) == 1
        receipt = json.loads((attempts[0].parent / "passed.json").read_text())
        assert receipt["inputs"]["runtime"]["homeassistant_version"] == active_version
        assert receipt["inputs"]["installed_runtime"] == receipt["inputs"]["runtime"]
    else:
        with pytest.raises(ValueError, match=error):
            checks.ensure_check(*args, installed=("previous", previous))
        assert not attempts and not output.exists()


# Intent: executable paths and contents from both ways of invoking OpenSSL identify a passing run.
# Why: updating the service executable or the fixture executable can change TLS without touching Python.
def test_release_identity_tracks_both_openssl_executables(tmp_path, monkeypatch):
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    fixture, service = tool_bin / "openssl", tmp_path / "system-openssl"
    for executable in (fixture, service):
        executable.write_text("fixture version one\n")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", "bin")
    monkeypatch.setattr(checks, "SYSTEM_OPENSSL", service)

    initial = checks.openssl_identity(tmp_path)
    assert initial["fixture"]["executable"] == str(fixture)
    assert initial["service"]["executable"] == str(service)
    fixture.write_text("fixture version two\n")
    changed = checks.openssl_identity(tmp_path)
    assert changed["fixture"] != initial["fixture"] and changed["service"] == initial["service"]
    service.write_text("system version two\n")
    assert checks.openssl_identity(tmp_path)["service"] != changed["service"]


# Intent: run real certificate generation and inspection under the same OpenSSL settings a cached week represents.
# Why: inherited config files can break synthetic certificates even when the executables stay the same.
def test_installation_week_reuses_results_with_pinned_openssl_config(tmp_path, monkeypatch):
    source, previous, output = (tmp_path / name for name in ("candidate", "installed", "evidence"))
    tools = source / "tools"
    tools.mkdir(parents=True)
    previous.mkdir()
    for build in (source, previous):
        (build / "requirements-trial.txt").write_text("websockets==16.0\n")
    for pin in ("requirements-ha-proxy.txt", "requirements-ha-proxy-2026.10.txt"):
        (source / pin).write_text("homeassistant==2026.11.0\n")

    certificate = tmp_path / "certificate.pem"
    generate = (
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=offline.test",
        "-addext",
        "subjectAltName=IP:127.0.0.1",
        "-keyout",
        str(tmp_path / "key.pem"),
        "-out",
        str(certificate),
    )
    inspect = (str(checks.SYSTEM_OPENSSL), "x509", "-in", str(certificate), "-noout", "-dates", "-checkip", "127.0.0.1")
    classname, name = checks.WEEK.case.rsplit(".", 1)
    report = f'<testsuite><testcase classname="{classname}" name="{name}"/></testsuite>'
    (tools / checks.WEEK.runner).write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"assert os.environ['OPENSSL_CONF'] == {os.devnull!r}\n"
        "assert 'OPENSSL_CONF_INCLUDE' not in os.environ and 'OPENSSL_MODULES' not in os.environ\n"
        f"subprocess.run({generate!r}, check=True)\n"
        f"inspection = subprocess.check_output({inspect!r}, text=True, env={{**os.environ, 'LC_ALL': 'C'}})\n"
        "assert 'IP 127.0.0.1 does match certificate' in inspection\n"
        "junit = next(arg.removeprefix('--junitxml=') for arg in sys.argv if arg.startswith('--junitxml='))\n"
        f"Path(junit).write_text({report!r})\n"
    )
    runtime = {"homeassistant_version": "2026.11.0", "packages": [["websockets", "16.0"]]}
    monkeypatch.setattr(checks, "runtime_identity", lambda *args, **kwargs: runtime)
    bad_settings = tmp_path / "broken.cnf"
    bad_settings.write_text("invalid OpenSSL configuration\n")
    monkeypatch.setenv("OPENSSL_CONF", str(bad_settings))
    monkeypatch.setenv("OPENSSL_CONF_INCLUDE", str(tmp_path))
    monkeypatch.setenv("OPENSSL_MODULES", str(tmp_path))
    assert subprocess.run(generate, cwd=source, capture_output=True, timeout=20).returncode != 0

    args = (checks.WEEK, source, output, {"commit": "a", "source_digest": "b"}, sys.executable)
    checks.ensure_check(*args, installed=("old", previous))
    (receipt,) = output.glob("*/passed.json")
    assert json.loads(receipt.read_text())["inputs"]["openssl"]["config"] == os.devnull
    assert os.environ["OPENSSL_CONF"] == str(bad_settings)
    bad_settings.write_text("a different error in inherited OpenSSL settings\n")
    monkeypatch.setenv("OPENSSL_CONF_INCLUDE", str(previous))
    checks.ensure_check(*args, installed=("old", previous))
    assert list(output.glob("*/passed.json")) == [receipt]
    assert len(list(output.glob("*/attempt-*"))) == 1


# Intent: prove each build has the right dependencies and reuse a pass only while both runtimes and TLS inputs match.
# Why: a dependency rollout can require different service environments even though the simulated HA runner is shared.
def test_installation_week_verifies_split_runtimes_and_openssl(tmp_path, monkeypatch):
    source, previous = (tmp_path / name for name in ("candidate", "installed"))
    source.mkdir()
    previous.mkdir()
    (source / "requirements-trial.txt").write_text("websockets==16.0\n")
    (source / "requirements-ha-proxy.txt").write_text("homeassistant==2026.11.0\n")
    (source / "requirements-ha-proxy-2026.10.txt").write_text("homeassistant==2026.10.4\n")
    (previous / "requirements-trial.txt").write_text("websockets==15.0.1\n")
    installed_python = str(tmp_path / "previous-python")
    candidate_runtime = {"homeassistant_version": "2026.11.0", "packages": [["websockets", "16.0"]]}
    old_runtime = {"runtime_sha256": "old-1", "packages": [["websockets", "15.0.1"]]}
    openssl = "first-ssl"
    change_during_run = None
    attempts = []

    # Read the assigned environment for each build so a changed runtime can invalidate the saved result.
    def identity(python, cwd, *, homeassistant=False):
        if cwd == source:
            assert python == sys.executable and homeassistant
            return candidate_runtime.copy()
        assert cwd == previous and python == installed_python and not homeassistant
        return old_runtime.copy()

    # Keep the receipt real while recording which interpreter the release command would give the subprocess harness.
    def run(check, python, cwd, attempt, old, *, ha_version, installed_python):
        nonlocal openssl
        assert (python, cwd, old, ha_version, installed_python) == (
            sys.executable,
            source,
            previous,
            "2026.11.0",
            str(tmp_path / "previous-python"),
        )
        attempts.append(attempt)
        classname, name = check.case.rsplit(".", 1)
        (attempt / "pytest.xml").write_text(f'<testsuite><testcase classname="{classname}" name="{name}"/></testsuite>')
        if change_during_run == "installed runtime":
            old_runtime["runtime_sha256"] += "-changed"
        elif change_during_run == "OpenSSL":
            openssl += "-changed"

    monkeypatch.setattr(checks, "runtime_identity", identity)
    monkeypatch.setattr(checks, "openssl_identity", lambda source: {"fixture": openssl})
    monkeypatch.setattr(checks, "run_check", run)
    args = (checks.WEEK, source, tmp_path / "evidence", {"commit": "a", "source_digest": "b"}, sys.executable)
    installed = ("old", previous)
    with pytest.raises(ValueError, match="provide --ics-installed-python"):
        checks.ensure_check(*args, installed=installed)
    old_runtime["packages"] = candidate_runtime["packages"]
    with pytest.raises(ValueError, match="pins differ from the installed standalone runtime"):
        checks.ensure_check(*args, installed=installed, installed_python=installed_python)
    assert not attempts
    old_runtime["packages"] = [["websockets", "15.0.1"]]
    checks.ensure_check(*args, installed=installed, installed_python=installed_python)
    checks.ensure_check(*args, installed=installed, installed_python=installed_python)
    assert len(attempts) == 1
    receipt = json.loads((attempts[0].parent / "passed.json").read_text())
    assert receipt["inputs"]["installed_runtime"] == old_runtime
    assert receipt["inputs"]["runtime"] == candidate_runtime
    assert receipt["inputs"]["openssl"] == {"fixture": openssl}

    old_runtime["runtime_sha256"] = "old-2"
    checks.ensure_check(*args, installed=installed, installed_python=installed_python)
    openssl = "new-ssl"
    checks.ensure_check(*args, installed=installed, installed_python=installed_python)
    assert len(attempts) == 3
    for change_during_run in ("installed runtime", "OpenSSL"):
        openssl += "-next"
        with pytest.raises(ValueError, match=change_during_run + " changed during verification"):
            checks.ensure_check(*args, installed=installed, installed_python=installed_python)
        assert not (attempts[-1].parent / "passed.json").exists()


# Intent: verify the imported Home Assistant version in the runner used for probing and rehearsal.
# Why: a second installed distribution may show a pin that the interpreter never imports.
@pytest.mark.parametrize("active_version", ["2026.10.4", "2026.11.0", "2026.10.9"])
def test_installation_week_runner_verifies_selected_home_assistant(tmp_path, active_version):
    runner = Path(__file__).resolve().parents[1] / "tools" / checks.WEEK.runner
    inactive_version = "2026.10.4" if active_version == "2026.11.0" else "2026.11.0"
    active, inactive = tmp_path / "active", tmp_path / "inactive"
    for root, version in ((active, active_version), (inactive, inactive_version)):
        package = root / "homeassistant"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "const.py").write_text(f"__version__ = {version!r}\n")
        metadata = root / f"homeassistant-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: homeassistant\nVersion: {version}\n")
    bootstrap = (
        "import runpy, sys; "
        "sys.path[:0] = sys.argv[1:3]; "
        "script, *args = sys.argv[3:]; "
        "sys.argv[:] = [script, *args]; "
        "runpy.run_path(script, run_name='__main__')"
    )
    command = [sys.executable, "-E", "-P", "-B", "-c", bootstrap, str(active), str(inactive), str(runner)]
    probe = subprocess.run([*command, "--print-ha-version"], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == active_version
    rehearsal = subprocess.run([*command, f"--expected-ha-version={inactive_version}"], capture_output=True, text=True)
    assert rehearsal.returncode != 0
    assert f"imported Home Assistant {active_version}. Expected {inactive_version}" in rehearsal.stderr

    (active / "homeassistant/const.py").write_text(f"__version__ = {inactive_version!r}\n")
    mismatched = subprocess.run([*command, "--print-ha-version"], capture_output=True, text=True)
    assert mismatched.returncode != 0
    assert f"imported Home Assistant {inactive_version}. Metadata reports {active_version}" in mismatched.stderr


# Intent: retain the logs but withhold release results if the runtime changes during the run.
# Why: the results would be incorrectly attributed to the interpreter and packages hashed at entry.
def test_predeployment_check_requires_stable_runtime_during_verification(tmp_path, monkeypatch):
    packages = [["homeassistant", "2026.11.0"], ["websockets", "15.0.1"]]
    identities = iter(
        {"packages": packages, "runtime_sha256": digest, "homeassistant_version": "2026.11.0"} for digest in ("a", "b")
    )
    (tmp_path / "requirements-trial.txt").write_text("websockets==15.0.1\n")
    (tmp_path / "requirements-ha-proxy.txt").write_text("homeassistant==2026.11.0\n")
    (tmp_path / "requirements-ha-proxy-2026.10.txt").write_text("homeassistant==2026.10.4\n")

    # Simulate a passing test so only the changed runtime can withhold the receipt.
    def run(check, python, source, attempt, previous, *, ha_version, installed_python):
        classname, name = check.case.rsplit(".", 1)
        (attempt / "pytest.xml").write_text(f'<testsuite><testcase classname="{classname}" name="{name}"/></testsuite>')

    monkeypatch.setattr(checks, "runtime_identity", lambda python, source, *, homeassistant: next(identities))
    monkeypatch.setattr(checks, "run_check", run)
    output = tmp_path / "evidence"
    with pytest.raises(ValueError, match="installation week runtime changed during verification"):
        checks.ensure_check(
            checks.WEEK,
            tmp_path,
            output,
            {"commit": "a", "source_digest": "b"},
            sys.executable,
            installed=("previous", tmp_path),
        )
    assert list(output.glob("*/attempt-*/pytest.xml"))
    assert not list(output.glob("*/passed.json"))


# Intent: the unit shipped to standalone operators counts as a proxy release input.
# Why: a unit-only change can affect how the daemon starts or shuts down in an installation.
def test_proxy_change_detection_includes_packaged_unit(releases):
    repo, _, _, installed = releases
    candidate = commit(repo, "packaging/ics-proxy.service.example", "[Service]\nTimeoutStopSec=45\n")
    assert checks.changed_proxy(repo, candidate, installed) is True


# Intent: include changes to the tools initializer and Stop incident reader in release rehearsal and fingerprinting.
# Why: both are imported by the installed service and its diagnostic command.
@pytest.mark.parametrize("runtime_input", ["__init__.py", "managed_stop_incident.py"])
def test_proxy_release_and_fingerprint_include_imported_tools(releases, runtime_input):
    repo, _, _, _ = releases
    tools = ("__init__.py", "managed_reporting.py", "managed_incident.py", "managed_stop_incident.py", "trial_proxy.py")
    for name in tools:
        if name != runtime_input:
            commit(repo, f"tools/{name}", "# runtime helper\n")
    installed = commit(repo, f"tools/{runtime_input}", "REVISION = 1\n")
    before = software(repo)["proxy_source_sha256"]

    candidate = commit(repo, f"tools/{runtime_input}", "REVISION = 2\n")
    after = software(repo)["proxy_source_sha256"]
    assert checks.changed_proxy(repo, candidate, installed)
    assert before is not None and after is not None and before != after


# Intent: every static command and the full audit retain the original candidate if HEAD moves during the run.
# Why: a published qualification receipt must describe the same source that passed its checks.
@pytest.mark.parametrize("candidate_passes", [True, False])
def test_release_checks_the_frozen_candidate_after_checkout_changes(tmp_path, monkeypatch, releases, candidate_passes):
    repo, installed, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    snapshot = tmp_path / "validated"
    snapshot.mkdir()
    checked_sources = []
    commands = []

    # Move the checkout on the first check and verify which source each subsequent check sees.
    def step(name, command, results, *, cwd=None, cache_dir=None):
        if not commands:
            subprocess.run(["git", "checkout", "--detach", "-q", installed], cwd=repo, check=True)
        commands.append(command)
        if command[0] == "git":
            assert command == ["git", "show", "--check", "--format=", candidate]
        else:
            assert cwd.name == "source" and cwd.parent.parent == snapshot
            assert not (snapshot / "source").exists()
            assert (cwd / "tools/managed_proxy.py").read_text() == "# candidate\n"
            assert cache_dir != cwd and cache_dir is not None
            checked_sources.append(cwd)
        results[name] = candidate_passes or name != "ruff check"

    # Confirm the audit still receives the candidate while the checkout contains the installed build.
    def audit(path, source, metadata, python, results, executors):
        assert source == checked_sources[0]
        assert metadata["commit"] == candidate
        assert (repo / "tools/managed_proxy.py").read_text() == "# installed\n"
        results["three suites"] = True
        return {}, {}

    monkeypatch.setattr(validate, "run_step", step)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    monkeypatch.setattr(checks, "ensure_check", lambda *args: None)
    results = {}
    validate.run_full_qualification(
        tmp_path / "report.json",
        sys.executable,
        results,
        validated_snapshot=snapshot,
        deployment=validate.Deployment("ics-client", None, None),
    )
    assert len(checked_sources) == 7
    assert results["three suites"] and all(results.values()) is candidate_passes
    receipt = snapshot / "snapshot.json"
    assert receipt.exists() is candidate_passes
    if candidate_passes:
        assert json.loads(receipt.read_text())["commit"] == candidate
        assert (snapshot / "source/tools/managed_proxy.py").read_text() == "# candidate\n"
    else:
        assert list(snapshot.iterdir()) == []


# Intent: a full tests-only audit can pass independently, and the CLI refuses a publication request in this mode.
# Why: an evidence audit can verify the recorded suites, while publication also requires static validation.
def test_tests_only_audits_frozen_source_without_publishing(tmp_path, monkeypatch, releases):
    repo, _, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    audits = []

    # Record which frozen source the tests-only path audits.
    def audit(path, source, metadata, python, results, executors):
        assert source != repo
        assert metadata["commit"] == candidate
        assert (source / "tools/managed_proxy.py").read_text() == "# candidate\n"
        audits.append(metadata["commit"])
        results["three suites"] = True
        return {}, {}

    # Detect any unexpected static check in the tests-only path.
    def unexpected_static(*args, **kwargs):
        pytest.fail("tests-only mode ran static checks")

    monkeypatch.setattr(validate, "run_static_checks", unexpected_static)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    arguments = [
        "validate",
        "--tests-only",
        "--qualification-report",
        str(tmp_path / "report.json"),
        "--deployment-target",
        "qualification-only",
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    assert validate.main() == 0
    assert audits == [candidate]

    snapshot = tmp_path / "validated"
    monkeypatch.setattr(sys, "argv", [*arguments, "--validated-snapshot", str(snapshot)])
    with pytest.raises(SystemExit) as error:
        validate.main()
    assert error.value.code == 2
    assert audits == [candidate] and not snapshot.exists()


# Intent: exact scoped release checks precede publication of an approved source snapshot.
# Why: a failed week must stop the actual packaging path, even when the normal three suites have already passed.
@pytest.mark.parametrize(
    ("target", "same_proxy", "week_fails", "pin_matches", "expected"),
    [
        ("qualification-only", False, False, True, ()),
        ("ics-client", False, False, True, (checks.DISCOVERY,)),
        ("ics-proxy", True, False, True, (checks.DISCOVERY,)),
        ("ics-proxy", False, False, True, (checks.DISCOVERY, checks.WEEK)),
        ("ics-proxy", False, True, True, (checks.DISCOVERY, checks.WEEK)),
        ("ics-proxy", False, False, False, ()),
    ],
)
def test_release_snapshot_waits_for_required_checks(
    tmp_path, monkeypatch, releases, target, same_proxy, week_fails, pin_matches, expected
):
    repo, installed, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    snapshot = tmp_path / "validated"
    snapshot.mkdir()
    calls = []

    # This test covers focused iCS checks after static checks and audits. It still freezes the real commit.
    def audit(path, source, metadata, python, results, executors):
        assert metadata["commit"] == candidate
        results["three suites"] = True
        return {"websockets": "15.0.1" if pin_matches else "16.0"}, {}

    # Capture the command boundary before the release process can mark a snapshot as approved.
    def invoke(check, source, output, metadata, python, *, installed=None, installed_python=None):
        assert not (snapshot / "snapshot.json").exists()
        assert not (snapshot / "source").exists()
        assert source.name == "source" and source.parent.parent == snapshot and metadata["commit"] == candidate
        assert (source / "tools/managed_proxy.py").read_text() == "# candidate\n"
        if installed is not None:
            assert installed[0] == releases[1]
            assert (installed[1] / "tools/managed_proxy.py").read_text() == "# installed\n"
            assert installed_python == str(Path(sys.executable).absolute())
        calls.append(check)
        if week_fails and check == checks.WEEK:
            raise ValueError("synthetic failed week")

    monkeypatch.setattr(validate, "run_static_checks", lambda *args, **kwargs: None)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    monkeypatch.setattr(checks, "ensure_check", invoke)
    results = {}
    validate.run_full_qualification(
        tmp_path / "report.json",
        sys.executable,
        results,
        validated_snapshot=snapshot,
        deployment=validate.Deployment(
            target, candidate if same_proxy else installed, Path(sys.executable), Path(sys.executable)
        ),
    )
    assert tuple(calls) == expected
    passed = not week_fails and pin_matches
    assert (snapshot / "snapshot.json").exists() is passed
    assert all(results.values()) is passed


# Intent: an unsuccessful release leaves the requested output available for retry with the same exact candidate.
# Why: a failed or repeated attempt must preserve any files the operator already kept in the destination.
def test_failed_validation_can_retry_the_same_snapshot_directory(tmp_path, monkeypatch, releases):
    repo, _, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    monkeypatch.setattr(validate, "run_static_checks", lambda *args, **kwargs: None)
    monkeypatch.setattr(checks, "ensure_check", lambda *args, **kwargs: None)
    fail = True

    # Refuse the first qualification and accept later attempts for the same frozen commit.
    def audit(path, source, metadata, python, results, executors):
        assert metadata["commit"] == candidate
        if fail:
            raise ValueError("synthetic failed audit")
        results["three suites"] = True
        return {}, {}

    monkeypatch.setattr(validate, "audit_qualification", audit)
    snapshot = tmp_path / "target"
    snapshot.mkdir()
    unrelated = snapshot / "notes.txt"
    unrelated.write_text("operator notes\n")

    # Run each attempt with fresh results and the same publication destination.
    def run():
        results = {}
        validate.run_full_qualification(
            tmp_path / "report.json",
            sys.executable,
            results,
            validated_snapshot=snapshot,
            deployment=validate.Deployment("ics-client", None, None),
        )
        return all(results.values())

    assert not run()
    assert list(snapshot.iterdir()) == [unrelated]
    fail = False
    assert run()
    approved = (snapshot / "snapshot.json").read_bytes()
    assert json.loads(approved)["commit"] == candidate
    assert not run()
    assert (snapshot / "snapshot.json").read_bytes() == approved
    assert (snapshot / "source/tools/managed_proxy.py").read_text() == "# candidate\n"
    assert unrelated.read_text() == "operator notes\n"


# Intent: publication can roll back its staged source when an older or concurrent receipt owns the output name.
# Why: a source snapshot and its receipt must remain paired when another publication already owns the receipt path.
def test_snapshot_publication_preserves_a_preexisting_receipt(tmp_path):
    source = tmp_path / "staging/source"
    source.mkdir(parents=True)
    (source / "candidate").write_text("candidate\n")
    destination = tmp_path / "validated"
    destination.mkdir()
    receipt = destination / "snapshot.json"
    receipt.write_text("original receipt\n")
    with pytest.raises(FileExistsError):
        validate.publish_snapshot(source, destination, {"commit": "candidate"})
    assert (source / "candidate").read_text() == "candidate\n"
    assert list(destination.iterdir()) == [receipt]
    assert receipt.read_text() == "original receipt\n"


# Produce synthetic boundary observations with the same public evidence fields as the discovery simulator.
def discovery_summary(orders):
    runs = []
    for order in orders:
        suffix = ["settle_lifecycle", "later_charge"]
        observations = []
        revision, pause, replied = 1, "original", False
        for event in ["prefix", *order, *suffix]:
            if event in ("pause", "newer_pause"):
                revision += 1
                pause = event
            replied |= event == "reply"
            later = event == "later_charge"
            receipts = {"original": "completed", "start": "running"}
            if pause != "original":
                receipts[pause] = "accepted"
            if event in suffix:
                receipts.update({"start": "completed", pause: "completed"})
            if later:
                receipts["final"] = "completed"
            observations.append(
                {
                    "event": event,
                    "intent": {"mode": "app" if later else "paused", "revision": revision + int(later)},
                    "expected_pause_revision": revision,
                    "old_start_stage": "accepted" if replied else "issued",
                    "old_reply_seen": replied,
                    "start_commands_seen": 1,
                    "receipts": receipts,
                }
            )
        runs.append({"purpose": "search", "status": "passed", "events": order, "observations": observations})
    return {
        "schema_version": 1,
        "status": "passed",
        "seed": 19,
        "planned": 20,
        "observed_events": sorted({row["event"] for run in runs for row in run["observations"]}),
        "runs": runs,
    }


# Intent: require evidence for each exact history even when a cache agrees with a changed artifact's hash.
# Why: a matching hash alone only identifies the cached bytes. The audit also checks the observations they contain.
@pytest.mark.parametrize("damage", ["placeholder", "duplicate", "missing_observation", "incomplete_return"])
def test_reusing_and_rechecking_discovery_results(tmp_path, monkeypatch, damage):
    attempts = []
    source = Path(__file__).resolve().parents[1]
    orders = checks.discovery_orders(sys.executable, source)

    # Write the exact raw artifacts in miniature so the cache treats them the same way as actual child output.
    def run(check, python, source, attempt, previous, *, ha_version, installed_python):
        attempts.append(attempt)
        path = attempt / "discovery"
        path.mkdir()
        (attempt / "pytest.xml").write_text(
            '<testsuite><testcase classname="tests_discovery.test_recovery_discovery" '
            'name="test_recovery_discovery"/></testsuite>'
        )
        (path / "summary.json").write_text(json.dumps(discovery_summary(orders)))

    monkeypatch.setattr(checks, "runtime_identity", lambda python, source, *, homeassistant: {"python": "fixture"})
    monkeypatch.setattr(checks, "run_check", run)
    args = (checks.DISCOVERY, source, tmp_path, {"commit": "a", "source_digest": "b"}, sys.executable)
    checks.ensure_check(*args)
    checks.ensure_check(*args)
    assert len(attempts) == 1
    path = attempts[0] / "discovery/summary.json"
    summary = json.loads(path.read_text())
    if damage == "placeholder":
        summary["runs"][0] = {"purpose": "search", "status": "passed"}
    elif damage == "duplicate":
        summary["runs"][0] = summary["runs"][1]
    elif damage == "missing_observation":
        summary["runs"][0]["observations"].pop(1)
    else:
        summary["runs"][0]["observations"][-1]["receipts"]["final"] = "accepted"
    path.write_text(json.dumps(summary))
    receipt_path = attempts[0].parent / "passed.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["evidence"]["discovery/summary.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    checks.ensure_check(*args)
    assert len(attempts) == 2
