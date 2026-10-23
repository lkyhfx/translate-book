#!/usr/bin/env python3
"""Independent source-grounded review gate for translated book chunks.

This module does NOT perform LLM review. A separate review agent produces
review_chunkNNNN.json; a revision agent edits output_chunkNNNN.md; a separate
verification pass produces verify_chunkNNNN.json for changed drafts.
Python tracks hashes and blocks stale/unreviewed final builds.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from manifest import file_hash, load_manifest, read_output_text
import run_state

VERSION = 1
STATE_FILE = "review_state.json"
CATEGORIES = {"omission", "addition", "mistranslation", "terminology",
              "numbers", "format", "style", "other"}
SEVERITIES = {"major", "minor"}


def _entries(temp_dir):
    manifest = load_manifest(temp_dir)
    if not manifest or not isinstance(manifest.get("chunks"), list) or not manifest["chunks"]:
        raise ValueError("Review requires a non-empty manifest.json")
    return {entry["id"]: entry for entry in manifest["chunks"]}


def _load_state(temp_dir):
    path = Path(temp_dir) / STATE_FILE
    if not path.exists():
        return {"version": VERSION, "chunks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != VERSION or not isinstance(data.get("chunks"), dict):
        raise ValueError("Invalid review_state.json schema")
    return data


def _save_state(temp_dir, state):
    directory = Path(temp_dir)
    fd, name = tempfile.mkstemp(prefix=".review-state-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(name, directory / STATE_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _paths(temp_dir, entry):
    base = Path(temp_dir)
    return base / entry["source_file"], base / entry["output_file"]


def _report_path(temp_dir, chunk_id, verification=False):
    prefix = "verify_" if verification else "review_"
    return Path(temp_dir) / f"{prefix}{chunk_id}.json"


def _validate_report(path, source, output):
    if not path.is_file():
        raise ValueError(f"Missing independent review report: {path.name}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("version") != VERSION:
        raise ValueError(f"Invalid review version: {path.name}")
    verdict = report.get("verdict")
    issues = report.get("issues")
    if verdict not in ("pass", "revise") or not isinstance(issues, list):
        raise ValueError(f"Expected verdict pass/revise and issues list: {path.name}")
    if verdict == "pass" and issues:
        raise ValueError("A pass report must have no unresolved issues")
    if verdict == "revise" and not issues:
        raise ValueError("A revise report must contain actionable issues")
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ValueError(f"Issue {index} is not an object")
        if issue.get("category") not in CATEGORIES or issue.get("severity") not in SEVERITIES:
            raise ValueError(f"Issue {index} has an invalid category/severity")
        quote = issue.get("source_quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in source:
            raise ValueError(f"Issue {index} must quote exact source text")
        target_quote = issue.get("translation_quote", "")
        if not isinstance(target_quote, str) or (target_quote and target_quote not in output):
            raise ValueError(f"Issue {index} target quote was not found in translated chunk")
        for key in ("explanation", "suggested_fix"):
            if not isinstance(issue.get(key), str) or not issue[key].strip():
                raise ValueError(f"Issue {index} missing actionable {key}")
    return report


def plan(temp_dir):
    entries = _entries(temp_dir)
    state = _load_state(temp_dir)["chunks"]
    translation_pending = set(run_state.plan(temp_dir)["translation_chunk_ids"])
    result = {"review_chunk_ids": [], "revision_chunk_ids": [],
              "verification_chunk_ids": [], "approved_chunk_ids": [],
              "blocked_chunk_ids": [], "waiting_for_translation_chunk_ids": [],
              "chunks": []}
    labels = {
        "review": "review_chunk_ids",
        "revise": "revision_chunk_ids",
        "verify": "verification_chunk_ids",
        "approved": "approved_chunk_ids",
        "blocked": "blocked_chunk_ids",
        "waiting_for_translation": "waiting_for_translation_chunk_ids",
    }
    for chunk_id, entry in entries.items():
        source, output = _paths(temp_dir, entry)
        reason = ""
        if chunk_id in translation_pending or not source.is_file() or not output.is_file():
            action, reason = "waiting_for_translation", "translation_not_current"
        elif not (read_output_text(output) or "").strip():
            action, reason = "waiting_for_translation", "blank_translation"
        else:
            old = state.get(chunk_id)
            source_sha, output_sha = file_hash(source), file_hash(output)
            initial = _report_path(temp_dir, chunk_id)
            verify = _report_path(temp_dir, chunk_id, True)
            if not isinstance(old, dict) or old.get("source_hash") != source_sha:
                action, reason = "review", "missing_or_stale_review"
            elif not initial.exists() or old.get("initial_report_hash") != file_hash(initial):
                action, reason = "review", "review_report_changed"
            elif old.get("status") == "approved_initial" and old.get("draft_hash") == output_sha:
                action = "approved"
            elif old.get("status") == "await_revision":
                action = "revise" if old.get("draft_hash") == output_sha else "verify"
            elif old.get("status") == "approved_revised" and old.get("final_hash") == output_sha:
                if verify.exists() and old.get("verification_report_hash") == file_hash(verify):
                    action = "approved"
                else:
                    action, reason = "review", "verification_report_changed"
            elif old.get("status") == "blocked" and old.get("final_hash") == output_sha:
                action, reason = "blocked", "verification_rejected"
            else:
                action, reason = "review", "translated_output_changed"
        result[labels[action]].append(chunk_id)
        result["chunks"].append({"chunk_id": chunk_id, "action": action, "reason": reason})
    result["ready"] = len(result["approved_chunk_ids"]) == len(entries)
    result["total_chunks"] = len(entries)
    approved = set(result["approved_chunk_ids"])
    result["first_pass_approved"] = sum(
        1 for cid in approved if state.get(cid, {}).get("status") == "approved_initial"
    )
    result["revised_approved"] = sum(
        1 for cid in approved if state.get(cid, {}).get("status") == "approved_revised"
    )
    return result


def record(temp_dir, chunk_id, verification=False):
    entries = _entries(temp_dir)
    if chunk_id not in entries:
        raise ValueError(f"Unknown manifest chunk: {chunk_id}")
    planned = next(c for c in plan(temp_dir)["chunks"] if c["chunk_id"] == chunk_id)
    expected = "verify" if verification else "review"
    if planned["action"] != expected:
        raise ValueError(f"{chunk_id}: expected {expected}, got {planned['action']}")
    source, output = _paths(temp_dir, entries[chunk_id])
    source_text, output_text = read_output_text(source), read_output_text(output)
    report_path = _report_path(temp_dir, chunk_id, verification)
    report = _validate_report(report_path, source_text, output_text)
    state = _load_state(temp_dir)
    old = state["chunks"].get(chunk_id, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if verification:
        state["chunks"][chunk_id] = {
            **old, "final_hash": file_hash(output),
            "verification_report_hash": file_hash(report_path),
            "status": "approved_revised" if report["verdict"] == "pass" else "blocked",
            "updated_at": now,
        }
    else:
        state["chunks"][chunk_id] = {
            "source_hash": file_hash(source), "draft_hash": file_hash(output),
            "initial_report_hash": file_hash(report_path),
            "status": "approved_initial" if report["verdict"] == "pass" else "await_revision",
            "issue_count": len(report["issues"]), "updated_at": now,
        }
    _save_state(temp_dir, state)
    return {"chunk_id": chunk_id, "status": state["chunks"][chunk_id]["status"],
            "issue_count": len(report["issues"])}


def assert_ready(temp_dir):
    result = plan(temp_dir)
    if not result["ready"]:
        pending = {key: value for key, value in result.items()
                   if key.endswith("_chunk_ids") and key != "approved_chunk_ids" and value}
        raise ValueError(f"Independent review gate not satisfied: {json.dumps(pending)}")
    return {"approved_chunk_ids": result["approved_chunk_ids"], "ready": True}


def main():
    parser = argparse.ArgumentParser(description="Source-grounded, resumable independent review gate")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "status", "assert-ready"):
        sub.add_parser(command).add_argument("temp_dir")
    for command in ("record-review", "record-verification"):
        p = sub.add_parser(command)
        p.add_argument("temp_dir")
        p.add_argument("chunk_id")
    args = parser.parse_args()
    try:
        if args.command in ("plan", "status"):
            result = plan(args.temp_dir)
            if args.command == "status":
                result = {k: len(v) if k.endswith("_chunk_ids") else v
                          for k, v in result.items() if k != "chunks"}
        elif args.command == "assert-ready":
            result = assert_ready(args.temp_dir)
        else:
            result = record(args.temp_dir, args.chunk_id, args.command == "record-verification")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Review gate error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
