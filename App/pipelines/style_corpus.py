"""Build a versioned, reviewable dialogue corpus from manifest-listed wiki sources."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import tempfile
from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path

from backend.snow_app.dialogue_persona import character_speaker_names
from backend.snow_app.mvp_policy import MVP_CHARACTERS, canonical_mvp_character
from backend.snow_app.mvp_service import (
    _FENNY_HIGH_STAKES_TERMS,
    _FUTIYA_BODY_TEASING_TERMS,
    _is_character_voice_line,
)

from .build_lexical_index import search_terms
from .common import (
    APP_ROOT,
    DATA_ROOT,
    INDEX_SOURCE_TYPES,
    RUNTIME_ROOT,
    chunk_text,
    clean_wikitext,
    normalize_character_references,
    normalize_text,
    read_jsonl,
    record_preamble,
    stable_id,
    write_json,
    write_jsonl,
)
from .style_mining import mine

STORY = {"affinity_story", "character_story", "main_story", "random_event"}
BLOCK = re.compile(r"\{\{\s*(剧情格式文本|剧情旁白文本|剧情选项)\s*(?=[|}])")
FIELD = re.compile(r"\|\s*([\w]+)\s*=")


def clean(value):
    value = re.sub(r"\[\[(?:[^]|]*\|)?([^]|]+)\]\]", r"\1", value)
    value = re.sub(r"<ref\b[^>]*>.*?</ref>|<!--.*?-->", "", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>|'{2,}", "", value)
    return " ".join(html.unescape(value).split()).strip()


def blocks(raw):
    # Match nested parser templates/choices independently. Any narrative or
    # option boundary interrupts adjacency even when it has no spoken line.
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    for match in BLOCK.finditer(raw):
        depth = 1
        for delimiter in re.finditer(r"\{\{|\}\}", raw[match.end() :]):
            depth += 1 if delimiter[0] == "{{" else -1
            if depth == 0:
                end = match.end() + delimiter.start()
                break
        else:
            continue
        body = raw[match.end() : end]
        fields = list(FIELD.finditer(body))
        values = {
            f[1]: clean(body[f.end() : fields[i + 1].start() if i + 1 < len(fields) else None])
            for i, f in enumerate(fields)
        }
        yield match[1], values.get("角色", ""), values.get("对话", "")


def speaker_registry(manifests):
    documents = []
    for armor in read_jsonl(manifests / "character_armors_index.jsonl"):
        char = canonical_mvp_character(armor.get("character_name"))
        if char:
            documents.append({"metadata": {**armor, "character_id": char.character_id}})
    owners = {}
    for char in MVP_CHARACTERS:
        names = character_speaker_names(char.character_id, char.display_name, documents)
        owners.update({name: char for name in {*names, char.source_name, *char.aliases}})
    return owners


def split(page):
    return "holdout" if int(sha256(page.encode()).hexdigest()[:8], 16) % 5 == 0 else "train"


def source_path(data, relative):
    path = Path(relative)
    if path.parts[:2] != ("Data", "Source"):
        raise ValueError("Unexpected source path")
    target = (data / Path(*path.parts[1:])).resolve()
    target.relative_to((data / "Source").resolve())
    return target


def story_documents(record, source, raw):
    # Use the serving lakehouse's chunk IDs whenever the complete line occurs
    # in a chunk. Keep a separate style ID for a line spanning two chunks.
    preamble = record_preamble(source, normalize_character_references(record))
    chunks = chunk_text(f"{preamble}\n\n{clean_wikitext(raw)}")
    return [
        (
            stable_id(
                source, record["page_id"], n, sha256(normalize_text(c).encode()).hexdigest(), prefix="doc_"
            ),
            re.sub(r"\s", "", c),
        )
        for n, c in enumerate(chunks)
    ]


def page_lines(record, source, raw, owners):
    page = record["page_id"]
    char_name = record.get("character_name", "")
    sequence = (
        [
            ("剧情格式文本", char_name, clean(v["text"]))
            for v in record.get("voice_lines", [])
            if v.get("text")
        ]
        if source == "character_voice"
        else list(blocks(raw))
    )
    docs = story_documents(record, source, raw) if source in STORY else []
    previous = None
    context = []
    lines, pairs, shared, cues = [], [], [], []
    for pos, (kind, speaker, line) in enumerate(sequence):
        char = owners.get(speaker)
        if kind == "剧情格式文本" and char and line:
            compact = re.sub(r"\s", "", line)
            document = next(
                (key for key, c in docs if compact in c), stable_id(page, pos, prefix="style_line_")
            )
            ref = dict(
                character_id=char.character_id,
                page_id=page,
                position=pos,
                document_id=document,
                split=split(page),
                source_type=source,
            )
            window = sequence[max(0, pos - 3) : pos + 4]
            nearby = " ".join(o[2] for o in window)
            row = dict(
                **ref,
                text=line,
                speaker=speaker,
                spoken=_is_character_voice_line(line, char.display_name) and not line.startswith(("（", "(")),
                high_stakes=source == "main_story" and any(t in nearby for t in _FENNY_HIGH_STAKES_TERMS),
            )
            lines.append(row)
            if previous:
                pairs.append(
                    dict(
                        **ref,
                        analyst=previous,
                        reply=line,
                        spoken=row["spoken"],
                        preceding=list(context[:-1])[-2:],
                    )
                )
            if source == "main_story":
                for _, name, _ in window:
                    other = owners.get(name)
                    if (
                        other
                        and other != char
                        and any(name in c and speaker in c and compact in c for k, c in docs if k == document)
                    ):
                        shared.append(dict(**ref, other_character_id=other.character_id, text=line))
                if (
                    char.display_name == "芙提雅"
                    and line == "妮塔你……干什么！"
                    and any(name == "妮塔" for _, name, _ in window)
                    and document.startswith("doc_")
                ):
                    cues.append(
                        dict(
                            **ref,
                            triggers=list(_FUTIYA_BODY_TEASING_TERMS),
                            co_speaker="妮塔",
                            strength="required",
                            required_opening="干什么！",
                            review_status="legacy",
                            hint="先用简短的‘干什么！’承接这个有原作依据的玩笑，再回应当下的话。",
                        )
                    )
        previous = line if kind == "剧情格式文本" and speaker == "分析员" else None
        if kind == "剧情选项":
            context.clear()
        elif line:
            context = (context + [f"{speaker or '旁白'}：{line}"])[-3:]
    return lines, pairs, shared, cues


def deduplicate(rows, fields):
    # Give holdout canonical ownership of duplicate replies/pairs to avoid
    # training on the test copy. Keep every occurrence for audit provenance.
    unique = {}
    for row in sorted(
        rows,
        key=lambda r: (
            r["source_type"] == "character_voice",
            r["split"] != "holdout",
            r["page_id"],
            r["position"],
        ),
    ):
        key = tuple(row[k] for k in fields)
        if key not in unique:
            unique[key] = {**row, "occurrences": []}
        unique[key]["occurrences"].append(
            {k: row[k] for k in ("page_id", "document_id", "position", "source_type", "split")}
        )
    return list(unique.values())


def build_index(path, pairs):
    from contextlib import closing

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE VIRTUAL TABLE examples USING fts5(character_id UNINDEXED,page_id UNINDEXED,"
            "document_id UNINDEXED,source_type UNINDEXED,analyst UNINDEXED,reply UNINDEXED,terms)"
        )
        conn.executemany(
            "INSERT INTO examples VALUES(?,?,?,?,?,?,?)",
            [
                (
                    p["character_id"],
                    p["page_id"],
                    p["document_id"],
                    p["source_type"],
                    p["analyst"],
                    p["reply"],
                    search_terms(p["analyst"]),
                )
                for p in pairs
                if p["split"] == "train" and p["spoken"] and not p.get("held_duplicate")
            ],
        )


class VoicePageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = False

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def public_sources(review_path, denylist_path):
    review_bytes = review_path.read_bytes()
    deny_bytes = denylist_path.read_bytes()
    review = json.loads(review_bytes)
    deny = json.loads(deny_bytes)
    if (
        review["review_status"] != "verified_against_bwiki_source_declaration"
        or deny["schema_version"] != "project-snow-content-denylist-1"
    ):
        raise ValueError("Unrecognized public source review")
    denied_ids = set(map(str, deny["page_ids"]))
    denied_urls = set(deny["urls"])
    denied_hashes = set(deny["sha256"])
    approved = set()
    for source in review["sources"]:
        if (
            source["exception_status"] != "no_page_exception_detected"
            or not source["source_revision_id"]
            or not source["fixed_revision_url"]
            or str(source["page_id"]) in denied_ids
            or source["canonical_url"] in denied_urls
            or source["fixed_revision_url"] in denied_urls
        ):
            continue
        for local in source["local_sources"]:
            if local["sha256"] not in denied_hashes:
                approved.add((source["canonical_url"], local["local_path"], local["sha256"]))
    return (
        approved,
        denied_ids,
        dict(
            public_review_sha256=sha256(review_bytes).hexdigest(),
            public_denylist_sha256=sha256(deny_bytes).hexdigest(),
        ),
    )


def build(data, root, version="1.4.0", graph=None, public_reviewed=False):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", version):
        raise ValueError("Invalid corpus version")
    if root.resolve().is_relative_to(data.resolve()):
        raise ValueError("Build output must be outside Data")
    manifests = data / "Manifest"
    status = {
        r["canonical_url"]: r.get("status", "active") for r in read_jsonl(manifests / "page_manifest.jsonl")
    }
    owners = speaker_registry(manifests)
    excluded = set(owners)
    if graph:
        excluded.update(
            r["name"]
            for r in read_jsonl(graph)
            if r.get("node_type") in {"character", "location", "place", "organization", "faction", "npc"}
        )
    lines, pairs, shared, cues = [], [], [], []
    inputs = {}
    approved, denied_ids, public_hashes = None, set(), {}
    if public_reviewed:
        approved, denied_ids, public_hashes = public_sources(
            APP_ROOT / "config/public_knowledge/data_license_review.json",
            APP_ROOT / "config/content_denylist.json",
        )
    for filename in ("page_manifest.jsonl", "character_armors_index.jsonl"):
        inputs["Manifest/" + filename] = sha256((manifests / filename).read_bytes()).hexdigest()
    if graph:
        inputs["entity_graph"] = sha256(graph.read_bytes()).hexdigest()
    for filename, source in INDEX_SOURCE_TYPES.items():
        if source not in STORY | {"character_voice"}:
            continue
        path = manifests / filename
        inputs["Manifest/" + filename] = sha256(path.read_bytes()).hexdigest()
        for rec in read_jsonl(path):
            if status.get(rec.get("canonical_url"), "active") != "active":
                continue
            file = source_path(data, rec["local_path"])
            if "index" in file.relative_to(data.resolve() / "Source").parts:
                continue
            contents = file.read_bytes()
            digest = sha256(contents).hexdigest()
            if approved is not None and (
                (rec["canonical_url"], rec["local_path"], digest) not in approved
                or str(rec["page_id"]) in denied_ids
            ):
                continue
            # Voice manifest transcripts are usable in the public build only
            # when their text is also in the reviewed local HTML snapshot.
            if approved is not None and source == "character_voice":
                parser = VoicePageText()
                parser.feed(contents.decode("utf-8-sig"))
                visible = re.sub(r"\s", "", "".join(parser.parts))
                rec = {
                    **rec,
                    "voice_lines": [
                        v
                        for v in rec.get("voice_lines", [])
                        if re.sub(r"\s", "", clean(v.get("text", ""))) in visible
                    ],
                }
            inputs[Path(rec["local_path"]).as_posix()] = digest
            excluded.update(rec.get("speaker_names", []))
            ll, pp, ss, cc = page_lines(rec, source, contents.decode("utf-8-sig"), owners)
            lines.extend(ll)
            pairs.extend(pp)
            shared.extend(ss)
            cues.extend(cc)
    lines = deduplicate(lines, ("character_id", "text"))
    pairs = deduplicate(pairs, ("character_id", "analyst", "reply"))
    held_replies = {(p["character_id"], p["reply"]) for p in pairs if p["split"] == "holdout"}
    for p in pairs:
        p["held_duplicate"] = p["split"] == "train" and (p["character_id"], p["reply"]) in held_replies
    sig, reg, topics = mine(lines, shared, excluded)
    root.mkdir(parents=True, exist_ok=True)
    output = root / version
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        stage = Path(tmp) / "style"
        stage.mkdir()
        datasets = dict(
            character_lines=lines,
            dialogue_pairs=pairs,
            signature_terms=sig,
            register_terms=reg,
            shared_topics=topics,
            situational_cues=cues,
        )
        for name, rows in datasets.items():
            write_jsonl(stage / f"{name}.jsonl", rows)
        build_index(stage / "examples.sqlite3", pairs)
        manifest = dict(
            version=version,
            **public_hashes,
            source_sha256=sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
            pages=sum(key.startswith("Data/Source") for key in inputs),
            counts={k: len(v) for k, v in datasets.items()},
            characters=dict(sorted(Counter(r["character_id"] for r in lines).items())),
            holdout_pairs=sum(p["split"] == "holdout" for p in pairs),
            train_pairs=sum(p["split"] == "train" for p in pairs),
            indexed_pairs=sum(
                p["split"] == "train" and p["spoken"] and not p["held_duplicate"] for p in pairs
            ),
            files={
                p.name: sha256(p.read_bytes()).hexdigest()
                for p in sorted(stage.iterdir())
                if not p.name.startswith(".")
            },
        )
        write_json(stage / "manifest.json", manifest)
        if output.exists():
            if json.loads((output / "manifest.json").read_text(encoding="utf8")) != manifest:
                raise ValueError(
                    "Version already exists with different inputs or output; choose a new version"
                )
            return manifest  # Preserve review decisions and downstream artifacts.
        stage.rename(output)
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=DATA_ROOT)
    p.add_argument("--runtime", type=Path, default=RUNTIME_ROOT)
    p.add_argument("--version", default="1.4.0")
    p.add_argument("--graph", type=Path)
    p.add_argument(
        "--public-reviewed", action="store_true", help="Restrict output to hash-reviewed public sources"
    )
    a = p.parse_args()
    print(
        json.dumps(
            build(a.data_root, a.runtime / "style", a.version, a.graph, a.public_reviewed),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
