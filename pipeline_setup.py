#!/usr/bin/env python3
"""
pipeline_step.py

Generic single-call runner used for all three runs of the
generate -> validate -> refine loop. Each run is just:
    instructions (a prompt) + one or more labeled JSON inputs -> JSON output

Run 1 (generate):
    python pipeline_step.py \
        --instructions structuring_prompt.txt \
        --input sheet=Sheet1_raw.json \
        --out Sheet1_structured.json

Run 2 (validate):
    python pipeline_step.py \
        --instructions validate_output_prompt.txt \
        --input raw_data=Sheet1_raw.json --input structured_output=Sheet1_structured.json \
        --out Sheet1_validation.json

Run 3 (refine):
    python pipeline_step.py \
        --instructions refine_output_prompt.txt \
        --input raw_data=Sheet1_raw.json \
        --input previous_structured_output=Sheet1_structured.json \
        --input qa_report=Sheet1_validation.json \
        --out Sheet1_structured_v2.json

Requires:
    pip install ollama
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

import httpx
import ollama

OLLAMA_BASE_URL = "http://213.173.109.6:39836"
OLLAMA_MODEL = "qwen-custom"

# connect: fail fast (5s) if the server is genuinely unreachable.
# read: generous (900s) since long validate/refine prompts can take
# a while for the model to generate — this is the one that was
# getting cut short before.
# write/pool: default-ish, generous enough not to matter.
REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=900.0, write=30.0, pool=30.0)


def build_prompt(instructions, labeled_inputs):
    parts = [instructions.strip()]
    for label, data in labeled_inputs:
        parts.append(f"\n--- {label} ---\n" + json.dumps(data, indent=2, ensure_ascii=False))
    parts.append("\nRespond with the output described above.")
    return "\n".join(parts)


def strip_code_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_ollama(prompt, model=OLLAMA_MODEL, max_tokens=6000, num_ctx=16384, retries=3, use_json_format=True):
    # timeout=None on the underlying httpx client would wait forever; an
    # explicit generous read timeout plus a couple of retries protects
    # against a request getting silently dropped mid-flight (connection
    # reset, server restart, etc.) on longer validate/refine calls.
    client = ollama.Client(host=OLLAMA_BASE_URL, timeout=REQUEST_TIMEOUT)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            chat_kwargs = dict(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"num_predict": max_tokens, "num_ctx": num_ctx},
            )
            if use_json_format:
                chat_kwargs["format"] = "json"
            resp = client.chat(**chat_kwargs)
            content = resp.message.content
            if not content or not content.strip():
                # HTTP 200 but nothing generated - usually means the
                # model ran out of context room, or (as seen in practice)
                # grammar-constrained JSON decoding (format="json") choking
                # on a deeply nested/long schema and the server giving up
                # silently. Treat as a retryable failure rather than
                # returning empty text.
                raise RuntimeError(
                    "Model returned an empty response (context exhaustion, "
                    "or the server failed generating under format=\"json\" "
                    "constraints - try --no-json-format to test)."
                )
            return content
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 5 * attempt
                print(f"  Request failed ({e!r}); retrying in {wait}s ({attempt}/{retries})...")
                time.sleep(wait)
    raise last_err


def main():
    parser = argparse.ArgumentParser(description="Run one step of the generate/validate/refine loop.")
    parser.add_argument("--instructions", required=True, help="Path to the prompt/instructions text file for this step")
    parser.add_argument(
        "--input", action="append", required=True,
        help="label=path.json - repeatable. The label is shown verbatim above that JSON block in the prompt.",
    )
    parser.add_argument("--out", required=True, help="Output path for this step's JSON result")
    parser.add_argument("--model", default=OLLAMA_MODEL)
    parser.add_argument(
        "--num-ctx", type=int, default=16384,
        help="Context window to request from the model (tokens). Raise this "
             "(e.g. 32768) if you see empty responses / connection resets on "
             "large validate or refine prompts - that usually means the "
             "combined prompt didn't fit.",
    )
    parser.add_argument(
        "--no-json-format", action="store_true",
        help="Don't pass format=\"json\" to Ollama (plain text generation, "
             "still parsed as JSON afterward). Try this if you see empty "
             "responses / connection resets with format=\"json\" enabled - "
             "grammar-constrained decoding on a deeply nested schema can "
             "choke some model/Ollama version combinations.",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=6000,
        help="Max tokens the model is allowed to generate (num_predict). "
             "If a response gets cut off mid-JSON (JSON parse error near "
             "the end of a long raw response), raise this - e.g. 12000 for "
             "the validate step, which tends to produce long reports.",
    )
    args = parser.parse_args()

    with open(args.instructions, "r", encoding="utf-8") as f:
        instructions = f.read()

    labeled_inputs = []
    for item in args.input:
        if "=" not in item:
            sys.exit(f"--input must be label=path.json, got: {item}")
        label, path = item.split("=", 1)
        if not os.path.isfile(path):
            sys.exit(f"File not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            labeled_inputs.append((label, json.load(f)))

    prompt = build_prompt(instructions, labeled_inputs)

    approx_tokens = len(prompt) // 4  # rough rule of thumb, not exact
    print(f"Prompt size: {len(prompt)} chars (~{approx_tokens} tokens est.), num_ctx={args.num_ctx}")
    if approx_tokens > args.num_ctx * 0.7:
        print(
            f"  Warning: prompt is already using a large share of the "
            f"{args.num_ctx}-token context window before the model has "
            f"generated anything back. Consider --num-ctx {args.num_ctx * 2} "
            f"if you see empty responses or connection drops."
        )

    print(f"Calling Ollama model '{args.model}' ...")
    raw_response = call_ollama(
        prompt, model=args.model, num_ctx=args.num_ctx, max_tokens=args.max_tokens,
        use_json_format=not args.no_json_format,
    )
    cleaned = strip_code_fences(raw_response)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        sys.exit(f"Model did not return valid JSON: {e}\n\nRaw response:\n{raw_response}")

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Stamp the timestamp directly into the saved filename, e.g.
    # "Sheet1_structured.json" -> "Sheet1_structured_16092026_112345.json"
    # (DDMMYYYY_HHMMSS - no colons, since ':' isn't a legal filename
    # character on Windows).
    now = datetime.datetime.now()
    timestamp_for_name = now.strftime("%d%m%Y_%H%M%S")
    base, ext = os.path.splitext(args.out)
    stamped_out = f"{base}_{timestamp_for_name}{ext}"

    with open(stamped_out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Wrote {stamped_out}")

    # Sidecar metadata file (kept separate from the model's own output so it
    # never changes the shape of the JSON a downstream pipeline step reads).
    generated_at = now.astimezone().isoformat(timespec="seconds")
    meta = {
        "generated_at": generated_at,
        "instructions": args.instructions,
        "model": args.model,
        "inputs": {label: path for item in args.input for label, path in [item.split("=", 1)]},
    }
    meta_path = os.path.splitext(stamped_out)[0] + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"Wrote {meta_path} (generated_at={generated_at})")

    if "score" in result:
        print(f"Validation score: {result['score']}/100")


if __name__ == "__main__":
    main()