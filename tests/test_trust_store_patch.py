import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "trust_store_patch", Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-trust-store.py"
)
trust = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trust)
EXPECTED = {
    "7DD78D5F4F13459A83DFF9ABBBA62EDBAF6F2D102BF257FD712F4D9F2746ED8D",
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B",
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4",
    "F2EDAAFF3D9B701FB4D14C9F285F8C0BAD90FC3BCC1FA07D47F4718EDABA34A5",
}


def pem(der, newline=b"\n"):
    return b"-----BEGIN CERTIFICATE-----" + newline + base64.b64encode(der) + newline + b"-----END CERTIFICATE-----"


class TrustStorePatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.hermes = self.root / "hermes"
        self.sources = self.root / "sources"
        self.system = self.root / "system.crt"
        self.sources.mkdir()
        self.ders = [f"synthetic-blocked-{n}".encode() for n in range(4)]
        mapped = dict(zip(self.ders, sorted(EXPECTED)))
        # Synthetic DER fixtures exercise each exact production fingerprint
        # without adding trusted certificates or relying on the host CA store.
        self.validate_patcher = patch.object(trust, "_validate_der")
        self.validate_patcher.start()
        patcher = patch.object(trust, "der_sha256", side_effect=lambda der: mapped.get(der, hashlib.sha256(der).hexdigest().upper()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.validate_patcher.stop)
        self.keep = b"# emSign Root TLS CA - G3 is only a comment\r\n" + pem(b"unrelated-root", b"\r\n") + b"\r\n"
        self.blocked = b"\n".join(pem(der) for der in self.ders)
        for n, der in enumerate(self.ders):
            (self.sources / f"root-{n}.crt").write_bytes(pem(der))
        (self.sources / "keep.crt").write_bytes(self.keep)
        self.system.write_bytes(self.keep + self.blocked)
        self.packages = []
        for version, package in (("3.12", "certifi"), ("3.14", "botocore")):
            path = self.hermes / ".venv" / "lib" / f"python{version}" / "site-packages" / package / "cacert.pem"
            path.parent.mkdir(parents=True)
            path.write_bytes(self.keep + self.blocked)
            self.packages.append(path)

    def invoke(self, **kwargs):
        return trust.patch_trust_store(hermes_root=self.hermes, source_root=self.sources,
                                       system_bundle=self.system, **kwargs)

    def test_exact_production_fingerprint_scope(self):
        self.assertEqual(trust.BLOCKED_ROOTS, EXPECTED)
        self.assertEqual(len(trust.BLOCKED_ROOTS), 4)

    def test_filter_preserves_every_other_byte_and_is_idempotent(self):
        data = self.keep + b"\n" + self.blocked + b"\r\n# trailing\n"
        filtered, summary = trust.filter_pem_bytes(data)
        expected = self.keep + b"\n" + b"\n" * 3 + b"\r\n# trailing\n"
        self.assertEqual(filtered, expected)
        self.assertEqual(set(summary["removed"]), EXPECTED)
        again, second = trust.filter_pem_bytes(filtered)
        self.assertEqual(again, filtered)
        self.assertEqual(second["removed"], [])

    def test_patch_removes_sources_then_regenerates_and_filters_all_bundles(self):
        def regenerate(command, **kwargs):
            self.assertEqual(command, ["update-ca-certificates", "--fresh"])
            self.assertFalse(any(self.sources.glob("root-*.crt")))
            # A regenerated system bundle may contain a blocked root from
            # another input; the final bundle still has to be filtered.
            self.system.write_bytes(self.keep + self.blocked)
        runner = Mock(side_effect=regenerate)
        report = self.invoke(runner=runner)
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_certificates_removed"], 4)
        runner.assert_called_once()
        self.assertEqual((self.sources / "keep.crt").read_bytes(), self.keep)
        first = {path: path.read_bytes() for path in [self.system, *self.packages]}
        for data in first.values():
            self.assertTrue(data.startswith(self.keep))
            self.assertEqual(trust.filter_pem_bytes(data)[1]["removed"], [])
        self.assertTrue(self.invoke(runner=runner)["ok"])
        self.assertEqual(first, {path: path.read_bytes() for path in first})

    def test_check_is_read_only_and_reports_residual_roots(self):
        runner = Mock()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ValueError, "blocked DER fingerprints"):
            self.invoke(check_only=True, runner=runner)
        runner.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.invoke(runner=Mock())
        with patch.object(Path, "write_bytes", side_effect=AssertionError("read-only check wrote")), patch.object(Path, "unlink", side_effect=AssertionError("read-only check deleted")):
            self.assertTrue(self.invoke(check_only=True, runner=runner)["ok"])
        runner.assert_not_called()

    def test_missing_each_required_bundle_fails_before_mutation(self):
        for absent in (self.system, *self.packages):
            original = absent.read_bytes()
            absent.unlink()
            runner = Mock()
            with self.assertRaisesRegex(ValueError, "CA bundle not found"):
                self.invoke(runner=runner)
            runner.assert_not_called()
            self.assertEqual(len(list(self.sources.glob("root-*.crt"))), 4)
            absent.write_bytes(original)

    def test_multiple_python_versions_are_all_checked(self):
        other = self.hermes / ".venv/lib/python3.15/site-packages/certifi/cacert.pem"
        other.parent.mkdir(parents=True)
        other.write_bytes(self.keep + self.blocked)
        self.invoke(runner=Mock())
        self.assertEqual(trust.filter_pem_bytes(other.read_bytes())[1]["removed"], [])

    def test_empty_or_malformed_bundles_fail(self):
        for data in (b"", b"not a certificate", b"-----BEGIN PRIVATE KEY-----\nAA==\n-----END PRIVATE KEY-----", self.blocked):
            with self.assertRaises(ValueError):
                trust.filter_pem_bytes(data)
        filtered, summary = trust.filter_pem_bytes(pem(self.ders[0]), allow_empty=True)
        self.assertEqual(filtered, b"")
        self.assertEqual(summary["certificates_after"], 0)

    def test_x509_validation_is_not_only_base64_validation(self):
        self.validate_patcher.stop()
        with self.assertRaises((ValueError, trust.ssl.SSLError)):
            trust.filter_pem_bytes(pem(b"not an X509 certificate"))

    def test_cli_check_and_nonzero_failure_without_accessing_host_store(self):
        with patch.object(trust.sys, "platform", "linux"), patch.object(trust, "patch_trust_store", return_value={"ok": True}) as check, patch.object(trust.subprocess, "check_output", return_value='{"version":"synthetic-node","roots":[]}'), redirect_stdout(io.StringIO()):
            self.assertEqual(trust.main(["--check"]), 0)
            check.assert_called_once_with(check_only=True)
        with patch.object(trust.sys, "platform", "linux"), patch.object(trust, "patch_trust_store", side_effect=ValueError("Hermes botocore CA bundle not found")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trust.main([]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "Hermes botocore CA bundle not found")

    def test_cli_rejects_blocked_node_defaults_without_accessing_host_store(self):
        with patch.object(trust.sys, "platform", "linux"), patch.object(trust, "patch_trust_store", return_value={"ok": True}), patch.object(trust.subprocess, "check_output", return_value=json.dumps({"version": "synthetic", "roots": list(EXPECTED)})), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trust.main(["--check"]), 1)
            self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
