"""Meaningful failure and recurrence tests for the bounded delivery engine."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from clearrow import EXPORT_FIELDS, NOTE_FIELDS, InputError, formula_like, prepare, sha256, write_csv


def record(key="000101", member="0007", name="Alex Example", session="2026-10-20", status="present"):
    return dict(zip(EXPORT_FIELDS, [key, member, name, session, status]))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clearrow-test-")
        self.root = Path(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, records=None, notes=None, note_path=None):
        self.counter += 1
        base = self.root / str(self.counter)
        base.mkdir()
        export = base / "export.csv"
        note_file = base / "notes.csv"
        write_csv(export, EXPORT_FIELDS, [record()] if records is None else records, review=False)
        if note_path is None:
            write_csv(note_file, NOTE_FIELDS, [{"record_id": "000101", "staff_note": "Preserve this note."}] if notes is None else notes, review=False)
        else:
            note_file = note_path
        out = base / "out"
        evidence = prepare(export, note_file, out, "test", True)
        return evidence, out

    @staticmethod
    def read(out, name="cleaned.csv"):
        with (out / name).open(encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))

    def test_repeat_refresh_preserves_notes_after_sort_name_change_and_new_rows(self):
        _, first = self.run_case([record(), record("000102", "0008", "Bea Example")],
                                 [{"record_id": "000101", "staff_note": "Alex's note"}, {"record_id": "000102", "staff_note": "Bea's note"}])
        _, second = self.run_case([record("000103", "0009", "New Example"), record("000102", "0008", "B. Example"), record(name="A. Example")],
                                  note_path=first / "notes_state.json")
        rows = {row["record_id"]: row for row in self.read(second)}
        self.assertEqual(rows["000101"]["staff_note"], "Alex's note")
        self.assertEqual(rows["000102"]["staff_note"], "Bea's note")
        self.assertEqual(rows["000103"]["staff_note"], "")

    def test_reused_record_id_for_other_member_is_held(self):
        _, first = self.run_case()
        evidence, second = self.run_case([record(member="9999")], note_path=first / "notes_state.json")
        self.assertEqual(self.read(second), [])
        self.assertIn("record_id_reassigned_member", evidence["decisions"][-1]["reasons"])
        state = json.loads((second / "notes_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["record_bindings"]["000101"], "0007")

    def test_conflicting_export_duplicates_all_held(self):
        evidence, out = self.run_case([record(), record(status="absent")])
        self.assertEqual(evidence["counts"]["held_export_rows"], 2)
        self.assertEqual(self.read(out), [])
        self.assertEqual(evidence["exception_reason_counts"]["conflicting_duplicate_record_id"], 2)

    def test_identical_export_duplicates_also_all_held(self):
        evidence, _ = self.run_case([record(), record()])
        self.assertEqual(evidence["exception_reason_counts"]["duplicate_record_id"], 2)
        self.assertEqual(evidence["counts"]["accepted_export_rows"], 0)

    def test_missing_record_id_never_attaches_missing_note(self):
        evidence, out = self.run_case([record(key="")], [{"record_id": "", "staff_note": "Do not attach"}])
        self.assertEqual(self.read(out), [])
        self.assertEqual(evidence["counts"]["held_note_rows"], 1)
        state = json.loads((out / "notes_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["records"][0]["staff_note"], "Do not attach")

    def test_leading_zero_ids_survive_csv_json_and_import_mapping(self):
        _, out = self.run_case()
        row = self.read(out)[0]
        mapped = self.read(out, "import_ready.csv")[0]
        self.assertEqual((row["record_id"], row["member_id"]), ("000101", "0007"))
        self.assertEqual((mapped["AttendanceRecordID"], mapped["MemberID"]), ("000101", "0007"))

    def test_ambiguous_and_impossible_dates_are_held_without_guessing(self):
        evidence, out = self.run_case([record(session="10/11/2026"), record(key="000102", session="2026-02-30")], [])
        self.assertEqual(evidence["counts"]["held_export_rows"], 2)
        self.assertEqual(self.read(out), [])
        self.assertEqual(evidence["exceptions"][0]["original_record"]["session_date"], "10/11/2026")

    def test_status_and_whitespace_normalization_are_logged(self):
        evidence, out = self.run_case([record(key=" 000101 ", name=" Alex Example ", status=" PRESENT ")])
        row = self.read(out)[0]
        self.assertEqual((row["record_id"], row["member_name"], row["attendance_status"]), ("000101", "Alex Example", "present"))
        self.assertEqual(sum(change["action"] == "declared_normalization" for change in evidence["changes"]), 3)

    def test_formula_like_export_is_held_and_exact_evidence_retained(self):
        attack = '=HYPERLINK("https://invalid.example","Example")'
        evidence, out = self.run_case([record(name=attack)])
        self.assertEqual(self.read(out), [])
        self.assertEqual(evidence["exceptions"][-1]["original_record"]["member_name"], attack)
        self.assertIn("formula_like_member_name", evidence["exceptions"][-1]["reasons"])

    def test_formula_like_note_held_state_exact_and_review_escaped(self):
        payload = "  =SUM(1,1)"
        evidence, out = self.run_case(notes=[{"record_id": "000101", "staff_note": payload}])
        self.assertEqual(self.read(out), [])
        self.assertEqual(self.read(out, "staff_notes_review.csv")[0]["staff_note"], "'" + payload)
        state = json.loads((out / "notes_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["records"][0]["staff_note"], payload)
        self.assertEqual(evidence["exceptions"][0]["original_record"]["staff_note"], payload)

    def test_review_export_covers_formula_prefixes_and_controls(self):
        values = ["=1+1", "+1", "-1", "@SUM(A1)", "\ttext", "\rtext", "\ntext", " \t=1"]
        path = self.root / "review.csv"
        write_csv(path, ["text"], [{"text": value} for value in values])
        with path.open(encoding="utf-8", newline="") as file:
            actual = [row["text"] for row in csv.DictReader(file)]
        self.assertEqual(actual, ["'" + value for value in values])
        self.assertFalse(formula_like("Normal note + normal punctuation"))

    def test_duplicate_notes_hold_export_and_preserve_both(self):
        evidence, out = self.run_case(notes=[{"record_id": "000101", "staff_note": "First"}, {"record_id": "000101", "staff_note": "Second"}])
        self.assertEqual(self.read(out), [])
        self.assertEqual(evidence["counts"]["held_note_rows"], 2)
        state = json.loads((out / "notes_state.json").read_text(encoding="utf-8"))
        self.assertEqual([row["staff_note"] for row in state["records"]], ["First", "Second"])
        self.assertEqual([row["original_record"]["staff_note"] for row in evidence["exceptions"] if row["source"] == "notes"], ["First", "Second"])

    def test_orphan_note_survives_and_attaches_when_record_returns(self):
        _, first = self.run_case([], [{"record_id": "000101", "staff_note": "Keep while absent"}])
        evidence, second = self.run_case(note_path=first / "notes_state.json")
        self.assertEqual(self.read(second)[0]["staff_note"], "Keep while absent")
        self.assertEqual(evidence["counts"]["orphan_note_rows"], 0)

    def test_multiline_quotes_and_commas_survive(self):
        note = 'Prefers reminders, "large type".\nSecond line.'
        _, out = self.run_case(notes=[{"record_id": "000101", "staff_note": note}])
        self.assertEqual(self.read(out)[0]["staff_note"], note)

    def test_structural_schema_errors_stop_without_output(self):
        variants = ["record_id,record_id\n1,2\n", "record_id,extra\n1,2\n", ",".join(EXPORT_FIELDS) + "\n1,2\n"]
        notes = self.root / "notes.csv"
        write_csv(notes, NOTE_FIELDS, [], review=False)
        for index, text in enumerate(variants):
            export, out = self.root / f"broken-{index}.csv", self.root / f"bad-{index}"
            export.write_text(text, encoding="utf-8")
            with self.assertRaises(InputError):
                prepare(export, notes, out)
            self.assertFalse(out.exists())

    def test_extra_fields_are_not_silently_discarded(self):
        export, notes, out = self.root / "extra.csv", self.root / "notes.csv", self.root / "extra-out"
        write_csv(export, EXPORT_FIELDS + ["unknown"], [{**record(), "unknown": "Do not lose me"}], review=False)
        write_csv(notes, NOTE_FIELDS, [], review=False)
        with self.assertRaises(InputError):
            prepare(export, notes, out)
        self.assertFalse(out.exists())

    def test_sources_unchanged_and_hashes_match_delivered_originals(self):
        evidence, out = self.run_case()
        self.assertEqual(sha256(out / "original_export.csv"), evidence["sources"]["export"]["sha256"])
        self.assertEqual(sha256(out / "original_notes.csv"), evidence["sources"]["notes"]["sha256"])
        for name, digest in evidence["output_sha256"].items():
            self.assertEqual(sha256(out / name), digest)

    def test_same_input_produces_deterministic_outputs(self):
        _, first = self.run_case()
        second = first.parent / "second"
        prepare(first.parent / "export.csv", first.parent / "notes.csv", second, "test", True)
        self.assertEqual({p.name: sha256(p) for p in first.iterdir()}, {p.name: sha256(p) for p in second.iterdir()})

    def test_existing_output_is_refused(self):
        _, first = self.run_case()
        before = sha256(first / "evidence.json")
        with self.assertRaises(InputError):
            prepare(first.parent / "export.csv", first.parent / "notes.csv", first)
        self.assertEqual(sha256(first / "evidence.json"), before)

    def test_report_escapes_html_from_data(self):
        _, out = self.run_case([record(name="<script>alert(1)</script>")])
        report = (out / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("default-src 'none'", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
