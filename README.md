# ClearRow: inspect a fictional CSV preparation

This free evaluation sample prepares attendance exports while keeping staff notes attached by exact, stable record ID. Every record is fictional. An AI agent produced and checked the work with deterministic scripts. No human review, customer adoption, savings or destination-system compatibility is claimed.

**[Open the complete evaluation package](clearrow-evaluation.zip)** and use GitHub's **Download raw file** button. Extract the ZIP, then open `outputs/clearrow-demo/report.html`. The package includes both runs, unchanged originals, prepared CSVs, exceptions, change records, source-row evidence, operating rules, a SHA-256 manifest, Python source and tests. No account, API key or paid dependency is needed to run it locally.

| Run | Export rows | Eligible | Held export rows |
| --- | ---: | ---: | ---: |
| First refresh | 11 | 4 | 7 |
| Second refresh | 6 | 6 | 0 |

All eight note rows remain in state. Four note rows remain held after the second run. Changing a name or row order never chooses a note.

**Raw CSV warning:** original files intentionally contain formula-like test text. Inspect originals as plain text; do not open them by double-clicking in a spreadsheet. Import identifier columns as Text to retain leading zeros. Output escaping is a limited prefix rule, not a universal spreadsheet-safety guarantee.

## Inspect and reproduce

The [engine source](clearrow.py) and [20-test suite](test_clearrow.py) are also readable here. They use Python's standard library without network requests. After downloading this repository, Python 3.10 or later can run the tests from its root:

```text
python -m unittest -v test_clearrow
```

On Windows, `py` may replace `python`. The inner evaluation ZIP contains full instructions to reproduce both preparations into fresh output folders. The engine refuses to overwrite an existing output folder. It supports one documented attendance schema; it is not a universal cleaner or a tested CRM integration. The engine's 100,000-row technical ceiling does not expand the paid pilot's 1,000-total-row limit.

You may inspect, download and run the fictional example for evaluation. CSV cleaning and audit tools already exist, including [OpenRefine](https://openrefine.org/) and [CSV Handoff Kit](https://github.com/pbishay545/csv-handoff-kit). No unprecedented originality is claimed.

## A separately scoped file commission

[Current ClearRow offer and booking terms](https://kentorgersen.gumroad.com/l/clearrow-csv-preparation): one agreed file outcome from at most two CSV inputs and 1,000 total rows, US$160 base split US$80 upfront and US$80 on completion, plus applicable tax/card conversion. We agree the schema, rules and three acceptance examples before booking. A review copy is due within two UK business days after prerequisites. Final files go through Gumroad; one correction for missed agreed requirements may be requested within 30 days after final delivery.

This sample is not an order, paid-client result or proof of willingness to pay. Use the offer page's contact for an inquiry and share only fictional or appropriately anonymised examples. The AI-enabled handling environment must be agreed before supplying real files. Personal/confidential records, live-account access, remote installation, financial/payroll/tax work and ongoing support are outside this pilot.
