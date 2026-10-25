"""Bounded attendance-export preparation. Python standard library only.

The stable key is an attendance record ID, not a person's name or row position.
All input records receive an accepted/held decision. No live system is modified.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date
import hashlib
import html
import io
import json
from pathlib import Path
import re
import shutil
import tempfile

VERSION = "0.1.0"
EXPORT_FIELDS = ["record_id", "member_id", "member_name", "session_date", "attendance_status"]
NOTE_FIELDS = ["record_id", "staff_note"]
CLEAN_FIELDS = EXPORT_FIELDS + ["staff_note"]
IMPORT_FIELDS = ["AttendanceRecordID", "MemberID", "MemberName", "SessionDate", "Attendance", "StaffNote"]
STATUSES = {"booked", "present", "absent", "cancelled"}
ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
MAX_BYTES = 10_000_000
MAX_ROWS = 100_000
MAX_CELL = 100_000


class InputError(ValueError):
    """Input cannot be interpreted without losing or guessing information."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def formula_like(value: str) -> bool:
    """Conservative prefix rule, not a claim about all spreadsheet software."""
    probe = value.lstrip().lstrip("\ufeff").lstrip()
    return bool(probe and probe[0] in "=+-@") or value.startswith(("\t", "\r", "\n"))


def review_value(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if formula_like(text) else text


def write_csv(path: Path, fields: list[str], rows: list[dict], review: bool = True) -> None:
    """Quote every field; review exports also escape formula-like prefixes."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            values = {key: row.get(key, "") for key in fields}
            writer.writerow({key: review_value(value) if review else value for key, value in values.items()})


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_csv(path: Path, required: list[str]) -> list[dict]:
    if path.stat().st_size > MAX_BYTES:
        raise InputError(f"{path.name}: input exceeds {MAX_BYTES:,} bytes")
    csv.field_size_limit(MAX_CELL)
    try:
        text = path.read_bytes().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if header is None:
            raise InputError(f"{path.name}: empty file")
        if len(header) != len(set(header)):
            raise InputError(f"{path.name}: duplicate column header")
        if set(header) != set(required):
            missing = sorted(set(required) - set(header))
            unexpected = sorted(set(header) - set(required))
            raise InputError(f"{path.name}: schema mismatch; missing={missing}, unexpected={unexpected}")
        rows = []
        for number, values in enumerate(reader, start=2):
            if len(values) != len(header):
                raise InputError(f"{path.name}: CSV record {number} has {len(values)} fields; expected {len(header)}")
            if any("\x00" in value for value in values):
                raise InputError(f"{path.name}: NUL byte in CSV record {number}")
            rows.append(dict(zip(header, values), source_row=number))
            if len(rows) > MAX_ROWS:
                raise InputError(f"{path.name}: input exceeds {MAX_ROWS:,} records")
        return rows
    except (UnicodeDecodeError, csv.Error) as error:
        raise InputError(f"{path.name}: invalid UTF-8 or CSV: {error}") from error


def load_notes(path: Path) -> tuple[list[dict], dict[str, str]]:
    if path.suffix.lower() != ".json":
        return load_csv(path, NOTE_FIELDS), {}
    if path.stat().st_size > MAX_BYTES:
        raise InputError(f"{path.name}: input exceeds {MAX_BYTES:,} bytes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"{path.name}: invalid notes JSON") from error
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("records"), list):
        raise InputError(f"{path.name}: expected notes_state schema version 1")
    if set(data) != {"schema_version", "records", "record_bindings"}:
        raise InputError(f"{path.name}: unexpected notes-state fields")
    bindings = data["record_bindings"]
    if not isinstance(bindings, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or not ID_RE.fullmatch(key) or not ID_RE.fullmatch(value) for key, value in bindings.items()):
        raise InputError(f"{path.name}: invalid stable-record bindings")
    if len(data["records"]) > MAX_ROWS:
        raise InputError(f"{path.name}: too many notes")
    result = []
    for number, record in enumerate(data["records"], start=1):
        if not isinstance(record, dict) or set(record) != set(NOTE_FIELDS):
            raise InputError(f"{path.name}: unexpected fields in note item {number}")
        if not all(isinstance(record[field], str) for field in NOTE_FIELDS):
            raise InputError(f"{path.name}: note item {number} must contain strings")
        if any("\x00" in record[field] or len(record[field]) > MAX_CELL for field in NOTE_FIELDS):
            raise InputError(f"{path.name}: unsupported note content at item {number}")
        result.append(dict(record, source_row=number))
    return result, bindings


def table(fields: list[str], rows: list[dict]) -> str:
    heading = "".join(f"<th scope='col'>{html.escape(field.replace('_', ' '))}</th>" for field in fields)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in fields) + "</tr>" for row in rows)
    return f"<div class='table'><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>"


def render_report(evidence: dict, accepted: list[dict], exceptions: list[dict]) -> str:
    counts = evidence["counts"]
    metrics = [("Export records", counts["export_rows"]), ("Eligible for demo import", counts["accepted_export_rows"]),
               ("Export records held", counts["held_export_rows"]), ("Notes retained", counts["notes_rows"])]
    cards = "".join(f"<div><strong>{value}</strong><span>{label}</span></div>" for label, value in metrics)
    reasons = [{"reason": key, "held_rows": value} for key, value in sorted(evidence["exception_reason_counts"].items())]
    context = "Synthetic demonstration" if evidence["synthetic_demo"] else "Preparation run"
    explanation = "This sample uses fictional people and deliberately faulty records." if evidence["synthetic_demo"] else "Held records require review before they can be included."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Clearrow demonstration · {html.escape(evidence['run_label'])}</title>
<style>body{{font:16px/1.6 system-ui,Arial,sans-serif;color:#17233b;background:#f5f7fa;margin:0}}main{{max-width:1120px;margin:auto;padding:40px 24px 64px}}h1{{font-size:32px;line-height:1.2;margin:8px 0 18px}}h2{{font-size:22px;margin-top:36px}}a{{color:#154da1}}.eyebrow{{font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}}.intro{{max-width:780px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:26px 0}}.metrics div{{background:white;padding:18px;border:1px solid #d7dfeb;border-radius:8px}}.metrics strong{{font-size:30px;display:block}}.metrics span{{font-size:14px}}.notice{{background:#fff4d8;padding:16px 20px;border-left:4px solid #a56900}}.table{{overflow:auto;background:white}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid #d7dfeb}}th{{background:#e5ebf4}}td{{white-space:pre-wrap;overflow-wrap:anywhere}}code{{font-size:13px}}footer{{margin-top:40px;color:#4d5a6e;font-size:14px}}@media print{{body{{background:white}}main{{padding:12px}}.metrics{{break-inside:avoid}}}}</style></head>
<body><main><p class="eyebrow">{context} · {html.escape(evidence['run_label'])}</p>
<h1>Refresh attendance records without moving staff notes</h1>
<p class="intro">Records join by the exact attendance record ID. Names and row order never select a note. {explanation}</p>
<div class="metrics">{cards}</div>
<p class="notice"><strong>Review is still required.</strong> Only eligible rows are in <a href="import_ready.csv">import_ready.csv</a>. This file follows the demo schema; acceptance by a customer’s software has not been tested. Raw originals may contain formula-like text. Review CSVs prefix such text with an apostrophe; exact values remain in JSON evidence.</p>
<h2>What needs attention</h2>{table(['reason', 'held_rows'], reasons) if reasons else '<p>No rows were held in this run.</p>'}
<p>{counts['orphan_note_rows']} note rows refer to IDs absent from this export. All {counts['notes_rows']} note rows remain in <a href="notes_state.json">notes_state.json</a>, including conflicts and unresolved IDs. Staff must resolve these items before those IDs can be attached.</p>
<h2>Eligible records</h2>{table(CLEAN_FIELDS, accepted)}
<h2>Held records</h2>{table(['source', 'source_row', 'record_id', 'reasons'], exceptions) if exceptions else '<p>No held records.</p>'}
<h2>Files and checks</h2><ul><li><a href="cleaned.csv">Canonical eligible records</a> and <a href="import_ready.csv">mapped demo import</a></li>
<li><a href="exceptions.csv">Held-row review</a>, <a href="changes.csv">changes</a> and <a href="provenance.csv">row provenance</a></li>
<li><a href="staff_notes_review.csv">All notes for review</a> (review-only CSV; retain the JSON state for the next run)</li>
<li><a href="evidence.json">Exact JSON evidence and source/output SHA-256 hashes</a></li></ul>
<h2>Rules applied</h2><ul>{''.join('<li>'+html.escape(rule)+'</li>' for rule in evidence['rules'])}</ul>
<footer>Engine {VERSION}. CSV preparation only. This report does not demonstrate external import acceptance, screen-reader certification or a paid-client outcome. No fuzzy matching, missing-value invention or date guessing is performed.</footer></main></body></html>"""


def prepare(export_path: Path, notes_path: Path, output: Path, run_label: str = "run", synthetic_demo: bool = False) -> dict:
    export_path, notes_path, output = Path(export_path), Path(notes_path), Path(output)
    if output.exists():
        raise InputError(f"Output already exists: {output}. Choose a new directory.")
    source_export = load_csv(export_path, EXPORT_FIELDS)
    source_notes, bindings = load_notes(notes_path)
    export_hash, notes_hash = sha256(export_path), sha256(notes_path)
    changes, exceptions, decisions = [], [], []

    def change(source: str, number: int, key: str, field: str, old: str, new: str, action: str) -> None:
        changes.append(dict(source=source, source_row=number, record_id=key, field=field, before=old, after=new, action=action))

    export_rows, note_rows = [], []
    for source in source_export:
        row = dict(source)
        for field in EXPORT_FIELDS:
            new = row[field].strip()
            if field == "attendance_status":
                new = new.lower()
            if new != row[field]:
                change("export", row["source_row"], row["record_id"].strip(), field, row[field], new, "declared_normalization")
                row[field] = new
        export_rows.append(row)
    for source in source_notes:
        row = dict(source)
        key = row["record_id"].strip()
        if key != row["record_id"]:
            change("notes", row["source_row"], key, "record_id", row["record_id"], key, "declared_normalization")
            row["record_id"] = key
        note_rows.append(row)

    exports_by_id, notes_by_id = defaultdict(list), defaultdict(list)
    for row in export_rows:
        if row["record_id"]:
            exports_by_id[row["record_id"]].append(row)
    for row in note_rows:
        if row["record_id"]:
            notes_by_id[row["record_id"]].append(row)
    note_issues = {}
    for position, row in enumerate(note_rows):
        reasons = []
        key = row["record_id"]
        if not key:
            reasons.append("missing_record_id")
        elif not ID_RE.fullmatch(key):
            reasons.append("invalid_record_id")
        if key and len(notes_by_id[key]) > 1:
            distinct = {r["staff_note"] for r in notes_by_id[key]}
            reasons.append("conflicting_duplicate_note_id" if len(distinct) > 1 else "duplicate_note_id")
        if formula_like(row["staff_note"]):
            reasons.append("formula_like_staff_note")
        note_issues[row["source_row"]] = reasons
        if reasons:
            exceptions.append(dict(source="notes", source_row=row["source_row"], record_id=key,
                                   reasons="; ".join(reasons), original_record={f: source_notes[position][f] for f in NOTE_FIELDS},
                                   normalized_record={f: row[f] for f in NOTE_FIELDS}))
        decisions.append(dict(source="notes", source_row=row["source_row"], record_id=key,
                              outcome="held" if reasons else ("retained_absent_from_export" if key not in exports_by_id else "eligible_note"), reasons=reasons))

    accepted, provenance = [], []
    for position, row in enumerate(export_rows):
        reasons, key = [], row["record_id"]
        if not key:
            reasons.append("missing_record_id")
        elif not ID_RE.fullmatch(key):
            reasons.append("invalid_record_id")
        if not row["member_id"] or not ID_RE.fullmatch(row["member_id"]):
            reasons.append("missing_or_invalid_member_id")
        if key in bindings and bindings[key] != row["member_id"]:
            reasons.append("record_id_reassigned_member")
        if not row["member_name"]:
            reasons.append("missing_member_name")
        try:
            if not DATE_RE.fullmatch(row["session_date"]):
                raise ValueError("ISO date required")
            date.fromisoformat(row["session_date"])
        except ValueError:
            reasons.append("invalid_iso_session_date")
        if row["attendance_status"] not in STATUSES:
            reasons.append("unsupported_attendance_status")
        for field in EXPORT_FIELDS:
            if formula_like(row[field]):
                reasons.append("formula_like_" + field)
        group = exports_by_id.get(key, [])
        if len(group) > 1:
            distinct = {tuple(r[field] for field in EXPORT_FIELDS) for r in group}
            reasons.append("conflicting_duplicate_record_id" if len(distinct) > 1 else "duplicate_record_id")
        matching = notes_by_id.get(key, [])
        if any(note_issues[note["source_row"]] for note in matching):
            reasons.append("staff_note_requires_review")
        if reasons:
            exceptions.append(dict(source="export", source_row=row["source_row"], record_id=key,
                                   reasons="; ".join(reasons), original_record={f: source_export[position][f] for f in EXPORT_FIELDS},
                                   normalized_record={f: row[f] for f in EXPORT_FIELDS}))
            decisions.append(dict(source="export", source_row=row["source_row"], record_id=key, outcome="held", reasons=reasons))
            continue
        note = matching[0] if matching else None
        cleaned = {field: row[field] for field in EXPORT_FIELDS}
        cleaned["staff_note"] = note["staff_note"] if note else ""
        accepted.append(cleaned)
        if note:
            change("notes", note["source_row"], key, "staff_note", "", note["staff_note"], "attached_by_exact_record_id")
        provenance.append(dict(output_row=len(accepted) + 1, record_id=key, export_source_row=row["source_row"],
                               note_source_row=note["source_row"] if note else "", join_method="exact_record_id" if note else "no_note_for_id"))
        decisions.append(dict(source="export", source_row=row["source_row"], record_id=key, outcome="accepted", reasons=[]))

    next_bindings = dict(bindings)
    next_bindings.update({row["record_id"]: row["member_id"] for row in accepted})
    note_state = {"schema_version": 1, "records": [{field: row[field] for field in NOTE_FIELDS} for row in note_rows], "record_bindings": next_bindings}
    held_export = sum(1 for row in exceptions if row["source"] == "export")
    counts = dict(export_rows=len(export_rows), accepted_export_rows=len(accepted), held_export_rows=held_export,
                  notes_rows=len(note_rows), held_note_rows=sum(bool(value) for value in note_issues.values()),
                  notes_attached=sum(bool(row["note_source_row"]) for row in provenance),
                  orphan_note_rows=sum(bool(row["record_id"]) and row["record_id"] not in exports_by_id for row in note_rows))
    if counts["accepted_export_rows"] + counts["held_export_rows"] != counts["export_rows"]:
        raise AssertionError("Every export row must have exactly one disposition")
    if len({row["record_id"] for row in accepted}) != len(accepted):
        raise AssertionError("Duplicate output ID")
    if any(formula_like(value) for row in accepted for value in row.values()):
        raise AssertionError("Formula-like value reached import output")
    if len(note_state["records"]) != len(source_notes):
        raise AssertionError("Notes state lost a source record")

    reason_counts = Counter(reason for row in decisions if row["outcome"] == "held" for reason in row["reasons"])
    evidence = dict(schema_version=1, engine_version=VERSION, run_label=run_label, synthetic_demo=synthetic_demo,
                    counts=counts, exception_reason_counts=dict(reason_counts),
                    sources={"export": {"filename": export_path.name, "sha256": export_hash},
                             "notes": {"filename": notes_path.name, "sha256": notes_hash}},
                    rules=["UTF-8 CSV only, or a version 1 notes-state JSON. Exact column set is required. Structural errors stop the run.",
                           "Trim surrounding whitespace from export fields and note record IDs. Lowercase attendance status. Staff-note text is otherwise unchanged.",
                           "Preserve identifiers as text, including leading zeros. Match notes only by exact attendance record ID, never by name or row order.",
                           "Require valid YYYY-MM-DD dates and booked/present/absent/cancelled status. Do not guess or convert ambiguous dates.",
                           "Hold every row sharing a duplicate export ID. Hold conflicting or duplicate note IDs and their matching export rows.",
                           "Hold missing or invalid IDs and formula-like field prefixes. Keep exact source values in JSON evidence.",
                           "Retain every note, including notes absent from the latest export and unresolved note records, in notes_state.json.",
                           "After a successful preparation, retain record-to-member ID bindings. Hold a known record ID if a later export assigns it to a different member ID.",
                           "Quote all CSV fields. Prefix formula-like text in review CSVs with an apostrophe. Raw source copies remain unchanged.",
                           "Import output is a demonstration mapping, not proof of acceptance by any external system."],
                    target_schema=dict(zip(EXPORT_FIELDS + ["staff_note"], IMPORT_FIELDS)),
                    decisions=decisions, exceptions=exceptions, changes=changes, provenance=provenance)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".clearrow-", dir=output.parent))
    try:
        shutil.copyfile(export_path, stage / "original_export.csv")
        notes_copy = "original_notes.json" if notes_path.suffix.lower() == ".json" else "original_notes.csv"
        shutil.copyfile(notes_path, stage / notes_copy)
        write_csv(stage / "cleaned.csv", CLEAN_FIELDS, accepted, review=False)
        import_rows = [dict(zip(IMPORT_FIELDS, [row[field] for field in CLEAN_FIELDS])) for row in accepted]
        write_csv(stage / "import_ready.csv", IMPORT_FIELDS, import_rows, review=False)
        exception_review = [{**row, "original_record": json.dumps(row["original_record"], ensure_ascii=False),
                             "normalized_record": json.dumps(row["normalized_record"], ensure_ascii=False)} for row in exceptions]
        write_csv(stage / "exceptions.csv", ["source", "source_row", "record_id", "reasons", "original_record", "normalized_record"], exception_review)
        write_csv(stage / "changes.csv", ["source", "source_row", "record_id", "field", "before", "after", "action"], changes)
        write_csv(stage / "provenance.csv", ["output_row", "record_id", "export_source_row", "note_source_row", "join_method"], provenance)
        write_csv(stage / "staff_notes_review.csv", ["source_row"] + NOTE_FIELDS, note_rows)
        write_json(stage / "notes_state.json", note_state)
        (stage / "report.html").write_text(render_report(evidence, accepted, exceptions), encoding="utf-8")
        evidence["output_sha256"] = {path.name: sha256(path) for path in sorted(stage.iterdir()) if path.is_file()}
        write_json(stage / "evidence.json", evidence)
        if sha256(export_path) != export_hash or sha256(notes_path) != notes_hash:
            raise InputError("Input changed while processing; output not committed")
        stage.rename(output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New output directory; existing directory is refused")
    parser.add_argument("--label", default="demo")
    parser.add_argument("--synthetic-demo", action="store_true", help="Label outputs as synthetic demonstration data")
    args = parser.parse_args()
    try:
        result = prepare(args.export, args.notes, args.out, args.label, args.synthetic_demo)
    except (InputError, OSError) as error:
        parser.exit(2, f"Cannot prepare files: {error}\n")
    print(json.dumps(result["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
