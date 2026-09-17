# Excel Extractor

Turns a messy `.xlsx` workbook into clean, nested JSON by combining a
deterministic cell-extraction script with an LLM-based
generate → validate → refine loop (run against a local/remote Ollama
model).

## How it works

```
workbook.xlsx
     │  consolidated_data_from_sheets.py
     ▼
raw_sheets/<Sheet>_raw.json          (every non-empty cell: coord, value, formula, merge_range)
     │  pipeline_setup.py  --instructions structuring_prompt.txt
     ▼
structured/<Sheet>_structured.json   (LLM's first attempt at clean, nested JSON)
     │  pipeline_setup.py  --instructions validate_output_prompt.txt
     ▼
structured/<Sheet>_validation.json   (QA report: score /100 + itemized issues)
     │  pipeline_setup.py  --instructions refine_output_prompt.txt
     ▼
structured/<Sheet>_structured_v2.json  (corrected final JSON)
```

### Step 1 — Extract raw cells

`consolidated_data_from_sheets.py` reads the workbook with `openpyxl`
(once with `data_only=True` for cached values, once with
`data_only=False` for formula text) and writes one JSON file per sheet
to `raw_sheets/`. Only non-empty cells are kept; merged regions emit a
single anchor cell tagged with `merge_range`.

```bash
python consolidated_data_from_sheets.py path/to/workbook.xlsx --outdir raw_sheets
```

### Step 2 — Generate structured JSON

`pipeline_setup.py` is a generic runner: it takes an instructions file
plus one or more labeled JSON inputs, sends them as a single prompt to
the Ollama model, and writes the parsed JSON response to `--out`.

```bash
python pipeline_setup.py \
    --instructions structuring_prompt.txt \
    --input sheet=raw_sheets/Sheet1_raw.json \
    --out structured/Sheet1_structured.json
```

`structuring_prompt.txt` tells the model how to turn a flat list of
cells into minimal nested JSON — plain tables become record lists,
label/value sheets become flat objects, multi-block sheets get one key
per block, merged header groups become nested objects, etc. It is
full of worked examples (Rules 1–10) covering multi-tier merged
headers, row-group labels, and edge cases like a locally-collapsed
header tier.

### Step 3 — Validate against the raw data

```bash
python pipeline_setup.py \
    --instructions validate_output_prompt.txt \
    --input raw_data=raw_sheets/Sheet1_raw.json \
    --input structured_output=structured/Sheet1_structured.json \
    --out structured/Sheet1_validation.json \
    --max-tokens 12000
```

`validate_output_prompt.txt` runs the model as a strict, literal QA
checker (not a re-structurer) across six checks — full cell coverage,
hallucinated/misattributed values, null correctness, merge/structural
fidelity, value fidelity, and unwarranted cleanup — and returns a
scored JSON report (`score` out of 100, deducting points per issue
found) that a refinement step can act on directly.

### Step 4 — Refine using the QA report

```bash
python pipeline_setup.py \
    --instructions refine_output_prompt.txt \
    --input raw_data=raw_sheets/Sheet1_raw.json \
    --input previous_structured_output=structured/Sheet1_structured.json \
    --input qa_report=structured/Sheet1_validation.json \
    --out structured/Sheet1_structured_v2.json
```

`refine_output_prompt.txt` fixes only what the QA report flagged,
while requiring everything else to stay byte-for-byte identical to the
previous structured output (no incidental flattening/renaming), and
independently re-checks for two recurring bug patterns even if the QA
report missed them: a label used as its own value, and a key that
doesn't match its row's actual label.

Re-run steps 3–4 (validate → refine) again on `Sheet1_structured_v2.json`
if the score isn't high enough yet.

## Setup

```bash
pip install openpyxl ollama httpx
```

`pipeline_setup.py` calls an Ollama-compatible server — the base URL
and model name are set at the top of the file:

```python
OLLAMA_BASE_URL = "http://213.173.99.7:34643"
OLLAMA_MODEL = "qwen-custom"
```

Update these (or add CLI overrides) if you're pointing at a different
server or model.

## Useful `pipeline_setup.py` flags

- `--num-ctx` — context window requested from the model. Raise this
  (e.g. `32768`) if you see empty responses or connection resets on
  large validate/refine prompts.
- `--max-tokens` — max tokens the model may generate. Raise this
  (e.g. `12000`) if a response gets cut off mid-JSON, which the
  validate step is especially prone to given how long its reports can
  get.
- `--no-json-format` — disable Ollama's `format="json"` grammar
  constraint (still parsed as JSON afterward). Try this if you get
  empty responses with a deeply nested schema.

## Directory layout

```
raw_sheets/     <Sheet>_raw.json            — output of step 1
structured/     <Sheet>_structured.json     — output of step 2 (first pass)
                <Sheet>_validation.json     — output of step 3 (QA report)
                <Sheet>_structured_v2.json  — output of step 4 (corrected)
```
