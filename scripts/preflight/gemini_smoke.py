"""Milestone 0 live Gemini smoke test.

Checks that the configured models are reachable on the caller's API key and
that the exact SDK calls in SPEC.md sections 2.2-2.3 behave as specified.
Makes 5 requests: 2 model lookups, 2 embedding calls, 1 generation call.

Usage (Python 3.12):
    python3.12 -m venv .venv-preflight
    .venv-preflight/bin/pip install -r scripts/preflight/requirements.txt
    .venv-preflight/bin/python scripts/preflight/gemini_smoke.py [--thinking-level low]

Reads GEMINI_API_KEY (and optional model overrides) from the environment or
from ./.env. The key is never printed.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

import numpy as np
from google import genai
from google.genai import errors, types

DIMENSIONS = 768


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def check_vectors(label: str, embeddings, expected_count: int) -> bool:
    ok = check(f"{label}: embedding count", len(embeddings) == expected_count,
               f"got {len(embeddings)}, expected {expected_count}")
    for i, emb in enumerate(embeddings):
        values = np.asarray(emb.values, dtype=np.float64)
        finite = bool(np.all(np.isfinite(values)))
        raw_norm = float(np.linalg.norm(values))
        normalized = values / raw_norm if raw_norm else values
        ok &= check(f"{label}[{i}]: dimensions", values.shape == (DIMENSIONS,), f"got {values.shape[0]}")
        ok &= check(f"{label}[{i}]: finite values", finite)
        ok &= check(f"{label}[{i}]: normalizes to unit length",
                    math.isclose(float(np.linalg.norm(normalized)), 1.0, rel_tol=1e-9),
                    f"raw norm {raw_norm:.6f}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thinking-level", choices=["low", "medium", "high"],
                        help="Optional; the spec call does not set it (model default applies).")
    args = parser.parse_args()

    load_dotenv(Path.cwd() / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    chat_model = os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.8-flash")
    embedding_model = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    if not api_key:
        print("GEMINI_API_KEY is not set (environment or ./.env). Live check not run.")
        return 2

    print(f"google-genai {genai.__version__}; chat={chat_model}; embedding={embedding_model}")
    client = genai.Client(api_key=api_key)
    ok = True
    try:
        for name in (chat_model, embedding_model):
            model = client.models.get(model=name)
            ok &= check(f"model listed: {name}", True,
                        f"{model.display_name}; actions={model.supported_actions}")

        docs = client.models.embed_content(
            model=embedding_model,
            contents=["Laptops can be returned within 30 days.", "Standard shipping takes 3-5 days."],
            config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT", output_dimensionality=DIMENSIONS),
        )
        ok &= check_vectors("RETRIEVAL_DOCUMENT batch", docs.embeddings, 2)

        query = client.models.embed_content(
            model=embedding_model,
            contents=["Can I return a laptop after 20 days?"],
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=DIMENSIONS),
        )
        ok &= check_vectors("RETRIEVAL_QUERY", query.embeddings, 1)

        config = types.GenerateContentConfig(
            system_instruction="Answer in one short sentence.",
            max_output_tokens=1200,
        )
        if args.thinking_level:
            config.thinking_config = types.ThinkingConfig(thinking_level=args.thinking_level.upper())
        response = client.models.generate_content(
            model=chat_model, contents="Reply with the word: ready", config=config,
        )
        text = response.text or ""
        finish = response.candidates[0].finish_reason if response.candidates else None
        usage = response.usage_metadata
        ok &= check("generate_content returned text", bool(text.strip()), f"{len(text)} chars")
        print(f"       finish_reason={finish}; thinking_level={args.thinking_level or 'model default'}")
        if usage:
            print(f"       usage: prompt={usage.prompt_token_count} output={usage.candidates_token_count} "
                  f"thoughts={usage.thoughts_token_count} total={usage.total_token_count}")
    except errors.APIError as exc:
        print(f"[FAIL] Gemini API error: code={exc.code} status={exc.status} message={exc.message}")
        return 1

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
