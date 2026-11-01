# ABOUTME: `sdlc doctor`'s `sandbox-userns` finding (Story 35.2-016): Ubuntu's AppArmor userns
# ABOUTME: restriction vs a podman outside every profile — sysctl, PATH and profiles all injected.

from __future__ import annotations

from pathlib import Path

from sdlc import doctor

NIX_PODMAN = "/nix/store/abc123-podman-5.2.0/bin/podman"
SYSCTL = "kernel.apparmor_restrict_unprivileged_userns"
# Captured at import: the autouse conftest fixture replaces doctor._read_userns_sysctl per test.
REAL_READ_USERNS_SYSCTL = doctor._read_userns_sysctl


def _profiles(tmp_path: Path, **files: str) -> Path:
    profile_dir = tmp_path / "apparmor.d"
    (profile_dir / "abstractions").mkdir(parents=True, exist_ok=True)  # a subdirectory is not a profile
    for name, body in files.items():
        (profile_dir / name).write_text(body)
    return profile_dir


# The profile Ubuntu 24.04 ships for its own podman.
UBUNTU_PODMAN = """abi <abi/4.0>,
include <tunables/global>

profile podman /usr/bin/podman flags=(unconfined) {
  userns,
  include if exists <local/podman>
}
"""


def _check(tmp_path: Path, *, sysctl: str | None = "1", podman: str | None = NIX_PODMAN,
           system: str = "Linux", profile_dir: Path | None = None):
    return doctor.check_sandbox_userns(
        system=system,
        read_sysctl=lambda: sysctl,
        which=lambda name: podman if name == "podman" else None,
        profile_dir=profile_dir or _profiles(tmp_path, podman=UBUNTU_PODMAN),
    )


def test_restricted_userns_and_a_nix_podman_warns_naming_both_remedies(tmp_path) -> None:
    finding = _check(tmp_path)

    assert (finding.check, finding.status) == ("sandbox-userns", "WARN")
    assert SYSCTL in finding.detail
    assert NIX_PODMAN in finding.detail
    assert "AppArmor profile" in finding.remedy
    assert NIX_PODMAN in finding.remedy
    assert "--skip-sandbox-image" in finding.remedy


def test_a_podman_a_profile_attaches_to_is_clean(tmp_path) -> None:
    finding = _check(tmp_path, podman="/usr/bin/podman")

    assert (finding.check, finding.status) == ("sandbox-userns", "CLEAN")
    assert "/usr/bin/podman" in finding.detail


def test_the_resolved_path_is_what_a_profile_attaches_to(tmp_path) -> None:
    # A Nix profile link resolves into /nix/store; AppArmor judges the resolved binary.
    real = tmp_path / "store" / "podman"
    real.parent.mkdir()
    real.write_text("")
    link = tmp_path / "profile-bin" / "podman"
    link.parent.mkdir()
    link.symlink_to(real)
    profiles = _profiles(tmp_path, nix=f"profile nixpodman {real} flags=(unconfined) {{\n  userns,\n}}\n")

    finding = _check(tmp_path, podman=str(link), profile_dir=profiles)

    assert finding.status == "CLEAN"


def test_a_globbed_or_path_named_profile_counts(tmp_path) -> None:
    profiles = _profiles(
        tmp_path,
        globbed="profile nixpodman /nix/store/*/bin/podman {\n  userns,\n}\n",
        named="/opt/podman/bin/podman flags=(unconfined) {\n}\n",
    )
    assert _check(tmp_path, profile_dir=profiles).status == "CLEAN"
    assert _check(tmp_path, podman="/opt/podman/bin/podman", profile_dir=profiles).status == "CLEAN"


def test_a_profile_with_several_flags_counts(tmp_path) -> None:
    # The commas inside `flags=(…)` separate flags; they do not end a rule.
    profiles = _profiles(
        tmp_path,
        nix="profile nixpodman /nix/store/*/bin/podman flags=(unconfined,attach_disconnected) {\n  userns,\n}\n",
        spaced="/opt/podman/bin/podman flags=(complain, attach_disconnected) {\n}\n",
    )
    assert _check(tmp_path, profile_dir=profiles).status == "CLEAN"
    assert _check(tmp_path, podman="/opt/podman/bin/podman", profile_dir=profiles).status == "CLEAN"


def test_a_rule_inside_a_profile_is_not_an_attachment(tmp_path) -> None:
    profiles = _profiles(tmp_path, other="profile foo /usr/bin/foo {\n  /nix/store/** rix,\n}\n")

    assert _check(tmp_path, profile_dir=profiles).status == "WARN"


def test_no_profile_directory_reads_as_no_profiles(tmp_path) -> None:
    assert _check(tmp_path, profile_dir=tmp_path / "absent").status == "WARN"


def test_unrestricted_userns_is_clean(tmp_path) -> None:
    finding = _check(tmp_path, sysctl="0")

    assert (finding.check, finding.status) == ("sandbox-userns", "CLEAN")
    assert finding.remedy == ""


def test_not_applicable_on_a_mac_without_the_sysctl_or_without_podman(tmp_path) -> None:
    assert _check(tmp_path, system="Darwin") is None
    assert _check(tmp_path, sysctl=None) is None  # a kernel without the restriction
    assert _check(tmp_path, podman=None) is None


def test_a_mac_never_reads_the_sysctl() -> None:
    def read() -> str:
        raise AssertionError("read /proc on a Mac")

    assert doctor.check_sandbox_userns(system="Darwin", read_sysctl=read) is None


def test_run_doctor_reports_it_only_where_the_check_applies(tmp_path, monkeypatch) -> None:
    warn = doctor.Finding("sandbox-userns", "Sandbox user namespaces", "WARN", "restricted", "fix")
    monkeypatch.setattr(doctor, "check_sandbox_userns", lambda **_: warn)
    assert warn in doctor.run_doctor(repo_root=tmp_path).findings

    monkeypatch.setattr(doctor, "check_sandbox_userns", lambda **_: None)
    report = doctor.run_doctor(repo_root=tmp_path)
    assert all(f.check != "sandbox-userns" for f in report.findings)


def test_the_sysctl_reader_strips_the_value_and_reads_a_missing_file_as_none(tmp_path, monkeypatch) -> None:
    sysctl = tmp_path / "restrict"
    sysctl.write_text("1\n")
    monkeypatch.setattr(doctor, "_USERNS_SYSCTL_PATH", sysctl)
    assert REAL_READ_USERNS_SYSCTL() == "1"
    monkeypatch.setattr(doctor, "_USERNS_SYSCTL_PATH", tmp_path / "absent")
    assert REAL_READ_USERNS_SYSCTL() is None


def test_an_unreadable_profile_is_skipped_not_fatal(tmp_path, monkeypatch) -> None:
    profile_dir = _profiles(tmp_path, podman=UBUNTU_PODMAN, broken="profile x /y {\n")
    real_read_text = Path.read_text

    def flaky(self: Path, *args, **kwargs):
        if self.name == "broken":
            raise PermissionError("unreadable")  # injected: chmod is a no-op as root in CI
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert doctor._apparmor_attachments(profile_dir) == ["/usr/bin/podman"]
