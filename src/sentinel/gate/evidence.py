"""Evidence bundles: canonical JSON, its SHA-256, tamper check and a readable report.

The bundle text is canonical (sorted keys, fixed separators, no NaN), so the same content
always has the same hash. The hash is stored in the append-only `gate_decisions` table;
`verify_bundle` recomputes it, so any edit to a stored bundle is detected.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np

BUNDLE_SCHEMA_VERSION = 1


def _clean(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars -> Python, tuples -> lists, NaN/inf -> None."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def canonical_json(bundle: dict[str, Any]) -> str:
    return json.dumps(
        _clean(bundle), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def bundle_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_bundle(text: str, expected_sha256: str) -> bool:
    """True only if the text is exactly what was hashed when the decision was stored."""
    return bundle_sha256(text) == expected_sha256


def render_markdown(bundle: dict[str, Any], sha256: str) -> str:
    """A short human-readable decision report (the JSON bundle is the record)."""
    m = bundle["models"]
    ev = bundle["evaluation"]
    lines = [
        f"# Gate decision {bundle['decision_id']}",
        "",
        f"**Decision: {bundle['decision']}**",
        "",
        bundle["reason"],
        "",
        "| | Production | Candidate |",
        "|---|---|---|",
        f"| Model | `{m['production']['label']}` | `{m['candidate']['label']}` |",
        f"| Trained as of week | {m['production']['as_of_week']} | "
        f"{m['candidate']['as_of_week']} |",
        f"| Shipped threshold | {m['production']['threshold']:.7f} | "
        f"{m['candidate']['threshold']:.7f} |",
        "",
        f"- Policy `{bundle['policy']['policy_version']}` (configs/gate_policy.yaml), "
        f"file type {bundle['file_type']}",
        f"- Evaluated weeks {ev['weeks']}; labels known by {ev['labels_as_of']}",
        f"- Data fingerprint `{bundle['data']['data_fingerprint']}`; git `{bundle['git_commit']}`",
        f"- Evidence bundle SHA-256 `{sha256}` (stored in `gate_decisions`)",
        "",
        "## Checks",
        "",
        "| Check | Hard | Status | Summary |",
        "|---|---|---|---|",
    ]
    for c in bundle["checks"]:
        summary = str(c["summary"]).replace("|", "\\|")
        lines.append(
            f"| {c['name']} | {'yes' if c['hard'] else 'no'} | **{c['status']}** | {summary} |"
        )
    weekly = next((c for c in bundle["checks"] if c["name"] == "weekly_regression"), None)
    if weekly and weekly["numbers"].get("weeks"):
        lines += [
            "",
            "## Recall per week (at the operating false-positive rate)",
            "",
            "| Week | Benign | Malicious | Production | Candidate | Drop | Status |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in weekly["numbers"]["weeks"]:
            if "production_recall" in r:
                lines.append(
                    f"| {r['week']} | {r['benign']:,} | {r['malicious']:,} | "
                    f"{100 * r['production_recall']:.2f}% | {100 * r['candidate_recall']:.2f}% | "
                    f"{100 * r['recall_drop']:+.2f} | {r['status']} |"
                )
            else:
                lines.append(
                    f"| {r['week']} | {r['benign']:,} | {r['malicious']:,} | - | - | - | "
                    f"{r['status']} |"
                )
    dis = next((c for c in bundle["checks"] if c["name"] == "disagreement"), None)
    if dis:
        n = dis["numbers"]
        lines += [
            "",
            "## Disagreements (McNemar exact test, shipped thresholds)",
            "",
            "| Class | Only production right | Only candidate right | p-value |",
            "|---|---|---|---|",
        ]
        for cls in ("malicious", "benign"):
            if cls in n:
                lines.append(
                    f"| {cls} | {n[cls]['only_production_right']} | "
                    f"{n[cls]['only_candidate_right']} | {n[cls]['mcnemar_p']:.3g} |"
                )
        lines.append("")
        lines.append(
            f"{len(n.get('flipped_files', []))} flipped files are listed in the JSON bundle."
        )
    return "\n".join(lines) + "\n"
