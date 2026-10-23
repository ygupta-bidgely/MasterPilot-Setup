"""Enrollment-file integrity (provisioner side): the manifest records every launchpad file the run produced and uploaded
(name, sha256 of the exact bytes uploaded, size, S3 bucket/key, the upload's write-journal entry), and a later phase never
uses or re-uploads a stored original whose bytes no longer match that record.

Most tests drive the REAL provisioning CLI as a child process with the recording stubs of test_provisioner_boundary
(local stub API, fake boto3 that records the sha256 of every body it receives, every other socket blocked).
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from test_provisioner_boundary import A, FAST, PHASES, World  # noqa: E402  (harness only, no TestCase imported)


# a resumed phase must never wait long in these tests (the stub never completes disagg/aggregation)
LATER = [*FAST, "--settle", "0", "--disagg-wait", "3", "--agg-wait", "3", "--disagg-resend-after", "1", "--attempts", "1"]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def manifest_path(w: World) -> Path:
    found = sorted(w.out.rglob("manifest.json"))
    assert len(found) == 1, found
    return found[0]


def created_world(script: str = "create_mds_single_fuel_user.py") -> tuple[World, Path]:
    w = World(A)
    p = w.run("--phases", PHASES, *FAST, script=script)
    if p.returncode != 0:
        w.close()
        raise AssertionError(w.tail(p))
    return w, manifest_path(w)


class ManifestRecordsUploadedBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w, cls.man = created_world()
        cls.m = json.loads(cls.man.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_every_upload_is_recorded_with_the_sha256_s3_received(self):
        recs = self.m.get("enrollmentFiles")
        self.assertIsInstance(recs, dict, "manifest has no enrollmentFiles integrity records")
        puts = self.w.puts()
        self.assertEqual({p["Key"].split("_")[0] for p in puts}, {"BC", "USERENROLL", "METERENROLL", "RAW", "INVOICE"})
        for p in puts:
            with self.subTest(key=p["Key"]):
                rec = recs.get(p["Key"])
                self.assertIsNotNone(rec, f"{p['Key']} uploaded but not recorded")
                self.assertEqual(rec["name"], p["Key"])
                self.assertEqual(rec["sha256"], p["sha256"])          # the bytes the S3 stub received
                self.assertEqual(rec["size"], p["size"])
                ups = [u for u in rec["uploads"] if u["bucket"] == p["Bucket"] and u["key"] == p["Key"]]
                self.assertEqual(len(ups), 1)
                self.assertEqual(ups[0]["sha256"], p["sha256"])
                self.assertEqual(ups[0]["size"], p["size"])

    def test_every_produced_file_is_recorded_and_its_stored_copy_matches(self):
        recs = self.m["enrollmentFiles"]
        for role, path in self.m["files"].items():
            with self.subTest(role=role):
                rec = recs[Path(path).name]
                stored = (self.man.parent / rec["relPath"]).read_bytes()
                self.assertEqual(sha(stored), rec["sha256"])
                self.assertEqual(len(stored), rec["size"])
                self.assertFalse(Path(rec["relPath"]).is_absolute())

    def test_upload_journal_reference_resolves_to_the_allowed_put(self):
        n = 0
        for rec in self.m["enrollmentFiles"].values():
            for u in rec["uploads"]:
                ref = u["journal"]
                self.assertEqual(Path(ref["path"]).resolve(), self.w.journal.resolve())
                line = Path(ref["path"]).read_text().splitlines()[ref["line"] - 1]
                self.assertEqual(sha(line.encode()), ref["sha256"])
                j = json.loads(line)
                self.assertEqual((j["kind"], j["allowed"], j["bucket"], j["key"], j["sha256"]),
                                 ("s3_put", True, u["bucket"], u["key"], u["sha256"]))
                n += 1
        self.assertEqual(n, len(self.w.puts()))


class LaterPhaseVerifiesTheOriginal(unittest.TestCase):
    """A resumed phase (--manifest) verifies every stored original first; a changed or missing one -> refused, nothing
    written (no S3 put, no API mutation, no SQS send, no journal entry; not even an API read)."""

    def setUp(self):
        self.w, self.man = created_world()
        self.addCleanup(self.w.close)
        self.m = json.loads(self.man.read_text())

    def original(self, role: str) -> Path:
        return self.man.parent / self.m["enrollmentFiles"][Path(self.m["files"][role]).name]["relPath"]

    def snapshot(self) -> dict:
        w = self.w
        return {"puts": len(w.puts()), "api": len(w.api), "sqs": len([r for r in w.aws if r.get("op") == "send_message"]),
                "journal": len(w.writes)}

    def refused(self, p, before: dict, needle: str):
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 2, self.w.tail(p))
        self.assertIn("ENROLLMENT_INTEGRITY", out)
        self.assertIn(needle, out)
        self.assertEqual(self.snapshot(), before)

    def test_control_intact_originals_resume(self):
        before = self.snapshot()
        p = self.w.run("--manifest", str(self.man), "--phases", "configure", *FAST)
        self.assertEqual(p.returncode, 0, self.w.tail(p))
        self.assertGreater(self.snapshot()["api"], before["api"])

    def test_tampered_userenroll_refused_before_aggregate(self):
        f = self.original("USERENROLL")
        f.write_bytes(f.read_bytes().replace(b"|RES|", b"|COM|", 1))
        before = self.snapshot()
        self.refused(self.w.run("--manifest", str(self.man), "--phases", "aggregate", *LATER), before, f.name)

    def test_tampered_meterenroll_refused_before_reupload(self):
        f = self.original("METERENROLL")
        f.write_bytes(f.read_bytes() + b"\n")
        before = self.snapshot()
        self.refused(self.w.run("--manifest", str(self.man), "--phases", "upload", *LATER), before, f.name)

    def test_deleted_userenroll_refused(self):
        f = self.original("USERENROLL")
        f.unlink()
        before = self.snapshot()
        self.refused(self.w.run("--manifest", str(self.man), "--phases", "aggregate", *LATER), before, "missing")

    def test_transition_to_tou_refuses_a_tampered_userenroll(self):
        f = self.original("USERENROLL")
        f.write_bytes(f.read_bytes().replace(b"|RES|", b"|COM|", 1))
        before = self.snapshot()
        p = self.w.run("--manifest", str(self.man), script="transition_mds_user_to_tou.py")
        self.refused(p, before, f.name)
        self.assertFalse(list(f.parent.glob("*_T2*.csv")), "a transition file was built from a changed original")

    def test_transition_to_tou_refuses_a_deleted_userenroll(self):
        self.original("USERENROLL").unlink()
        before = self.snapshot()
        self.refused(self.w.run("--manifest", str(self.man), script="transition_mds_user_to_tou.py"), before, "missing")


class UploadVerifiesInProcess(unittest.TestCase):
    """mds_common.upload(): the bytes sent are the recorded original's; a changed original is never (re-)uploaded and a
    recorded file is never replaced by different bytes."""

    def setUp(self):
        import target_fixture as TF
        TF.use(pilot=A["pilot"])
        import mds_common as mc
        self.mc = mc
        self.tmp = Path(tempfile.mkdtemp(prefix="enroll-int-"))
        (self.tmp / "input_files").mkdir()
        self.f = self.tmp / "input_files" / "USERENROLL_D_20260101_77001_MDSX.csv"
        self.f.write_bytes(b"c|a|p|RES|qa@example.invalid\n")
        self.run_ = mc.Run(self.tmp, {"files": {"USERENROLL": str(self.f)}})
        mc.record_files(self.run_)

        class S3:
            def __init__(s):
                s.puts = []

            def put_object(s, **k):
                s.puts.append(k)

            def head_object(s, Bucket, Key):
                return {"Metadata": {"utility_file_name": Key}}
        self.s3 = S3()

    def test_upload_sends_the_recorded_bytes(self):
        self.mc.upload(self.run_, self.s3, "b1", self.f)
        self.assertEqual(len(self.s3.puts), 1)
        rec = self.run_.m["enrollmentFiles"][self.f.name]
        self.assertEqual(sha(self.s3.puts[0]["Body"]), rec["sha256"])
        self.assertEqual(rec["uploads"][0]["sha256"], rec["sha256"])

    def test_changed_original_is_not_reuploaded(self):
        self.mc.upload(self.run_, self.s3, "b1", self.f)
        self.f.write_bytes(b"c|a|p|COM|qa@example.invalid\n")
        with self.assertRaises(self.mc.SetupError) as cm:
            self.mc.upload(self.run_, self.s3, "b1", self.f)
        self.assertIn("ENROLLMENT_INTEGRITY", str(cm.exception))
        self.assertEqual(len(self.s3.puts), 1)

    def test_missing_original_is_not_uploaded(self):
        self.f.unlink()
        with self.assertRaises(self.mc.SetupError):
            self.mc.upload(self.run_, self.s3, "b1", self.f)
        self.assertEqual(self.s3.puts, [])

    def test_a_recorded_file_is_never_rerecorded_with_other_bytes(self):
        self.f.write_bytes(b"different\n")
        with self.assertRaises(self.mc.SetupError):
            self.mc.record_files(self.run_)
        self.assertNotEqual(self.run_.m["enrollmentFiles"][self.f.name]["sha256"], sha(b"different\n"))


if __name__ == "__main__":
    unittest.main()
