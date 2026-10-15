from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Persist a complete, versioned prediction snapshot without copying source texts."""

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


LABELS = ("vacancy", "resume")
DECISION_POLICY = "strict_target_threshold_and_low_other_else_null"


def decide_label(prob_vac, prob_res, vacancy_threshold=0.8, resume_threshold=0.8,
                 max_other_probability=0.3):
    """Require a high target score and a strictly low competing score."""
    values = (prob_vac, prob_res, vacancy_threshold, resume_threshold, max_other_probability)
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in values):
        raise ValueError("Probabilities and thresholds must be finite and in [0, 1]")
    if max_other_probability > min(vacancy_threshold, resume_threshold):
        raise ValueError("Competing-score ceiling must not exceed either target threshold")
    if prob_vac > vacancy_threshold and prob_res < max_other_probability:
        return "vacancy"
    if prob_res > resume_threshold and prob_vac < max_other_probability:
        return "resume"
    return None


def save_snapshot(db_path, predictions, model_version, metadata):
    """All-or-nothing insertion; reject incomplete corpora and reused versions.

    predictions: iterable of (text_hash, prob_vac, prob_res).
    Human labels are not overwritten or substituted for model predictions.
    """
    db_path = local_path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    if not model_version or not model_version.strip():
        raise ValueError("model_version is required")
    vac = metadata["vacancy_threshold"]
    res = metadata["resume_threshold"]
    other = metadata.get("max_other_probability", 0.3)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    seen = set()
    for text_hash, pv, pr in predictions:
        if text_hash in seen:
            raise ValueError(f"Duplicate prediction: {text_hash}")
        seen.add(text_hash)
        pv, pr = float(pv), float(pr)
        rows.append((text_hash, model_version, pv, pr, decide_label(pv, pr, vac, res, other), now))
    metadata = {**metadata, "decision_policy": DECISION_POLICY, "max_other_probability": other}
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
    con = sqlite_connect(db_path.as_uri() + "?mode=rw", uri=True)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("BEGIN IMMEDIATE")
        hashes = {r[0] for r in con.execute("SELECT text_hash FROM contents")}
        if not hashes or hashes != seen:
            raise ValueError(f"Corpus mismatch: missing={len(hashes-seen)}, extra={len(seen-hashes)}")
        con.execute("""CREATE TABLE IF NOT EXISTS classification_runs (
            model_version TEXT PRIMARY KEY, labeled_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL, content_count INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS labeled_contents (
            text_hash TEXT NOT NULL REFERENCES contents(text_hash),
            model_version TEXT NOT NULL REFERENCES classification_runs(model_version),
            prob_vac REAL NOT NULL CHECK(prob_vac BETWEEN 0 AND 1),
            prob_res REAL NOT NULL CHECK(prob_res BETWEEN 0 AND 1),
            final_label TEXT CHECK(final_label IN ('vacancy','resume')),
            labeled_at TEXT NOT NULL, PRIMARY KEY(text_hash, model_version))""")
        con.execute("INSERT INTO classification_runs VALUES (?, ?, ?, ?)",
                    (model_version, now, metadata_json, len(rows)))
        con.executemany("INSERT INTO labeled_contents VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return len(rows)


def _write_query(con, path, query, params):
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = con.execute(query, params)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([column[0] for column in cur.description])
        writer.writerows(cur)


def export_snapshot(db_path, output_dir, model_version):
    """Export one explicit version, with distinct content/publication counts."""
    output_dir = local_path(output_dir)
    con = sqlite_connect(local_path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM classification_runs WHERE model_version=?", (model_version,)).fetchone():
            raise ValueError(f"Unknown model version: {model_version}")
        for label, filename in zip((*LABELS, None), ("vacancies_final.csv", "resumes_final.csv", "unlabeled_contents.csv")):
            _write_query(con, output_dir / filename, """
                SELECT l.text_hash,c.text,l.prob_vac,l.prob_res,l.final_label,l.model_version,l.labeled_at
                FROM labeled_contents l JOIN contents c USING(text_hash)
                WHERE l.model_version=? AND l.final_label IS ? ORDER BY l.text_hash
            """, (model_version, label))
        # Overall unique texts include texts without any publication.
        queries = ["""SELECT 'corpus' dimension,'all' dimension_value,l.final_label,
                     COUNT(DISTINCT l.text_hash) unique_contents,COUNT(p.id) publications,
                     l.model_version
                     FROM labeled_contents l JOIN contents c USING(text_hash)
                     LEFT JOIN publications p ON p.content_id=c.id
                     WHERE l.model_version=? GROUP BY l.final_label,l.model_version""",
                   """SELECT 'source',CAST(s.id AS TEXT)||':'||s.name,l.final_label,
                     COUNT(DISTINCT l.text_hash),COUNT(p.id),l.model_version
                     FROM labeled_contents l JOIN contents c USING(text_hash)
                     JOIN publications p ON p.content_id=c.id JOIN sources s ON s.id=p.source_id
                     WHERE l.model_version=? GROUP BY s.id,s.name,l.final_label,l.model_version""",
                   """SELECT 'month',COALESCE(strftime('%Y-%m',p.published_at),'unknown'),l.final_label,
                     COUNT(DISTINCT l.text_hash),COUNT(p.id),l.model_version
                     FROM labeled_contents l JOIN contents c USING(text_hash)
                     JOIN publications p ON p.content_id=c.id
                     WHERE l.model_version=? GROUP BY 2,l.final_label,l.model_version"""]
        _write_query(con, output_dir / "tables" / "corpus_summary.csv",
                     " UNION ALL ".join(queries), (model_version,) * 3)
    finally:
        con.close()
