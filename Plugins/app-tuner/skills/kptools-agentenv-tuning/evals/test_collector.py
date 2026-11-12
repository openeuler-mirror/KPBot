"""Exercise collector safety and evidence quality without elevated privileges."""

import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path


COLLECTOR = Path(
    os.environ.get(
        "AGENTENV_COLLECTOR",
        str(Path(__file__).resolve().parents[1] / "scripts/collect_environment.sh"),
    )
)
ARM_LINUX = platform.system() == "Linux" and platform.machine() in (
    "aarch64",
    "arm64",
)


def run_collector(*arguments: str) -> subprocess.CompletedProcess:
    """Run a selected collector without shell expansion or inherited stdin."""
    return subprocess.run(
        ["bash", str(COLLECTOR), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class CollectorSafetyTests(unittest.TestCase):
    """Check that malformed requests cannot overwrite existing evidence."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agentenv-collector-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence"

    def test_help_does_not_create_output(self) -> None:
        result = run_collector("--output", str(self.output), "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.output.exists())

    def test_output_is_required(self) -> None:
        self.assertEqual(run_collector().returncode, 1)

    def test_invalid_role_is_rejected(self) -> None:
        result = run_collector("--output", str(self.output), "--role", "root")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.output.exists())

    def test_invalid_pid_is_rejected(self) -> None:
        for pid in ("0", "-1", "../self", "1;true", "1/../../self"):
            with self.subTest(pid=pid):
                result = run_collector("--output", str(self.output), "--pid", pid)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "sentinel"
        sentinel.write_text("preserve\n", encoding="utf-8")
        result = run_collector("--output", str(self.output))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_dangling_symlink_is_preserved(self) -> None:
        self.output.symlink_to(self.root / "missing")
        result = run_collector("--output", str(self.output))
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.root / "missing").exists())

    def test_missing_parent_is_not_created(self) -> None:
        output = self.root / "missing" / "evidence"
        self.assertEqual(run_collector("--output", str(output)).returncode, 1)
        self.assertFalse(output.parent.exists())

    def test_native_platform_result(self) -> None:
        result = run_collector("--output", str(self.output))
        self.assertEqual(result.returncode, 0 if ARM_LINUX else 2, result.stderr)
        status = (self.output / "collection-status.txt").read_text(encoding="utf-8")
        self.assertEqual(status.strip(), "collected" if ARM_LINUX else "unsupported_platform")


@unittest.skipUnless(ARM_LINUX, "live evidence assertions require ARM Linux")
class CollectorArmEvidenceTests(unittest.TestCase):
    """Validate actual live-node artifacts, not fabricated platform fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="agentenv-arm-evidence-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name) / "evidence"
        cls.result = run_collector(
            "--output", str(cls.output), "--role", "unknown", "--pid", str(os.getpid())
        )
        if cls.result.returncode != 0:
            raise RuntimeError(cls.result.stderr or cls.result.stdout)

    def evidence(self, name: str) -> str:
        """Read a required artifact and report a missing file as a test failure."""
        path = self.output / name
        self.assertTrue(path.is_file(), f"Missing artifact: {name}")
        return path.read_text(encoding="utf-8")

    def test_node_identity(self) -> None:
        environment = self.evidence("environment.txt")
        self.assertIn(f"hostname={platform.node()}\n", environment)
        self.assertIn(f"collector_uid={os.getuid()}\n", environment)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.assertIn(f"boot_id={boot_id}\n", environment)
        self.assertIn("role_verified=false", environment)

    def test_ublk_module_provenance_is_separate(self) -> None:
        metadata = self.evidence("ublk-module.txt")
        self.assertTrue(metadata.strip())
        modules = Path("/proc/modules").read_text(encoding="utf-8")
        if any(line.startswith("ublk_drv ") for line in modules.splitlines()):
            self.assertIn("ublk_drv ", self.evidence("ublk.txt"))
            srcversion = Path("/sys/module/ublk_drv/srcversion")
            if srcversion.is_file():
                self.assertIn(srcversion.read_text().strip(), self.evidence("ublk.txt"))

    def test_ublk_access_is_not_functional_validation(self) -> None:
        evidence = self.evidence("ublk.txt")
        self.assertIn("control_device_access=", evidence)
        self.assertIn("not an ioctl or I/O test", evidence)

    def test_target_capabilities_are_recorded(self) -> None:
        evidence = self.evidence("target-process.txt")
        for field in ("Name:", "CapPrm:", "CapEff:", "Cpus_allowed_list:"):
            self.assertIn(field, evidence)

    def test_target_identity_is_separate(self) -> None:
        evidence = self.evidence("target-identity.txt")
        self.assertIn(f"pid={os.getpid()}", evidence)
        self.assertIn("exe=", evidence)
        self.assertIn("stat=", evidence)

    def test_missing_topology_fields_are_explicit(self) -> None:
        fields = ("physical_package_id", "die_id", "cluster_id", "core_id")
        missing = [
            Path("/sys/devices/system/cpu/cpu0/topology") / field
            for field in fields
            if not (Path("/sys/devices/system/cpu/cpu0/topology") / field).exists()
        ]
        if not missing:
            self.skipTest("cpu0 exposes every tested topology field")
        topology = self.evidence("topology.txt")
        for path in missing:
            self.assertTrue(f"{path}=unknown" in topology, f"Missing explicit unknown: {path}")

    def test_cpu_pressure_evidence_is_recorded(self) -> None:
        evidence = self.evidence("cpu-pressure.txt")
        if Path("/proc/pressure/cpu").is_file():
            self.assertIn("some ", evidence)
        else:
            self.assertIn("unknown", evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
