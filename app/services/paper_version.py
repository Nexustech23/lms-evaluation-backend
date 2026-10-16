"""Paper snapshot guards, including unversioned records from older deployments."""
import hashlib
import json


def paper_key_version(paper: dict) -> str:
    if paper.get("version"):
        return paper["version"]
    # Old keys without this fingerprint are rebuilt once, never trusted blindly.
    snapshot = [paper.get("url"), paper.get("fileId"), paper.get("text")]
    return "legacy:" + hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()


def paper_snapshot_filter(exam_id, paper: dict) -> dict:
    query = {"_id": exam_id, "question_paper.version": paper.get("version")}
    if not paper.get("version"):
        for field in ("url", "fileId", "text"):
            query[f"question_paper.{field}"] = paper.get(field)
    return query
