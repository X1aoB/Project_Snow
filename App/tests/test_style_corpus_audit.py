"""Synthetic source and prompt fixtures for the 1.4.0 audit tools."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from backend.snow_app.mvp_policy import canonical_mvp_character
from pipelines import style_audit as audit
from pipelines import style_corpus as corpus
from pipelines.style_compare import compare
from pipelines.style_mining import mine


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8")


def test_adjacent_turns_exact_speakers_and_boundaries():
    char = canonical_mvp_character("芙提雅")
    owners = {"芙提雅": char, "芙提雅·合成装甲": char}
    rec = {"page_id": "page"}
    raw = (
        "{{剧情格式文本|角色=分析员|对话=[[链接|今晚好呀]]}}"
        "{{剧情格式文本|角色=芙提雅·合成装甲|对话=<b>晚上好。</b>}}"
        "{{剧情格式文本|角色=分析员|对话=你听见了吗？}}"
        "{{剧情旁白文本|对话=灯光暗了。}}"
        "{{剧情格式文本|角色=芙提雅|对话=听见了。}}"
        "{{剧情格式文本|角色=分析员|对话=再见啦。}}"
        "{{剧情格式文本|角色=芙提雅假扮者|对话=不是本人的台词。}}"
    )
    lines, pairs, _, _ = corpus.page_lines(rec, "main_story", raw, owners)
    assert [r["text"] for r in lines] == ["晚上好。", "听见了。"]
    assert [(p["analyst"], p["reply"]) for p in pairs] == [("今晚好呀", "晚上好。")]
    assert all(r["page_id"] == "page" and r["document_id"] for r in lines)


def make_data(root):
    manifests = root / "Manifest"
    jsonl(manifests / "character_armors_index.jsonl", [])
    jsonl(manifests / "page_manifest.jsonl", [])
    for source, kind in corpus.INDEX_SOURCE_TYPES.items():
        if kind in corpus.STORY | {"character_voice"}:
            jsonl(manifests / source, [])
    records = []
    for target in ("train", "holdout"):
        page = next(str(i) for i in range(300) if corpus.split(str(i)) == target)
        rel = Path("Source/main_story") / (page + ".wikitext")
        file = root / rel
        file.parent.mkdir(exist_ok=True, parents=True)
        file.write_text(
            "{{剧情格式文本|角色=分析员|对话=今晚吃什么？}}"
            "{{剧情格式文本|角色=芬妮|对话=" + ("晚饭吃面。" if target == "train" else "晚饭吃粥。") + "}}",
            encoding="utf8",
        )
        records.append(dict(local_path=str(Path("Data") / rel), page_id=page))
    jsonl(manifests / "main_story_index.jsonl", records)


def test_index_excludes_held_pages_and_rebuild_retains_decisions(tmp_path):
    data, root = tmp_path / "Data", tmp_path / "runtime" / "style"
    make_data(data)
    manifest = corpus.build(data, root)
    path = root / "1.4.0"
    assert manifest["counts"]["character_lines"] == 2
    with closing(sqlite3.connect(path / "examples.sqlite3")) as connection:
        assert [r[0] for r in connection.execute("select reply from examples")] == ["晚饭吃面。"]
    review = path / "signature_terms.jsonl"
    review.write_text('{"review_status":"rejected"}\n', encoding="utf8")
    assert corpus.build(data, root) == manifest
    assert review.read_text(encoding="utf8") == '{"review_status":"rejected"}\n'
    with pytest.raises(ValueError):
        corpus.build(data, data / "Source")


def test_mining_incidence_deterministic_topic_exclusion():
    def line(cid, text, index):
        return dict(
            character_id=cid,
            text=text,
            spoken=True,
            source_type="affinity_story",
            split="train",
            high_stakes=False,
            page_id=f"p{index}",
            position=1,
            document_id=f"d{index}",
        )

    rows = [line("alpha", "妾身呀妾身，蓝城", i) for i in range(12)]
    rows.extend(line("beta", "我们喝茶吧", i) for i in range(25))
    s, reg, topics = mine(rows, [], {"蓝城"})
    assert (s, reg, topics) == mine(rows, [], {"蓝城"})
    assert all(r["term"] != "蓝城" and r["review_status"] == "pending" for r in s)
    assert all(r["count"] <= r["n"] and 0 < r["rate"] <= 1 for r in s)


def test_presets_price_and_feature_boundaries(tmp_path):
    assert audit.preset("candidate-style")[audit.PREFIX + "SHARED_FACTS"] == "futiya_legacy"
    assert audit.preset("candidate-all")[audit.PREFIX + "SHARED_FACTS"] == "data_driven"
    config = tmp_path / "config.json"
    config.write_text('{"OPENAI_API_KEY":"synthetic"}', encoding="utf8")
    with pytest.raises(ValueError):
        audit.preset(str(config))


def test_comparison_retains_adverse_outcomes_and_refuses_mismatched_seed(tmp_path):
    directories = []
    for name, rate in (("current", 0.2), ("candidate-style", 0.7)):
        path = tmp_path / name
        path.mkdir()
        manifest = dict.fromkeys(
            (
                "model",
                "seed",
                "characters",
                "probes",
                "sessions",
                "turns",
                "corpus_sha256",
                "runtime_inputs",
                "style_files",
                "probe_sha256",
                "commit",
            ),
            "fixture",
        )
        manifest.update(
            config=name,
            dry_run=False,
            status="complete",
            http_calls=1,
            estimated_prompt_tokens=100,
            estimate_calls=1,
            input_tokens=101,
            output_tokens=8,
        )
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
        audit.table(path / "metrics.csv", [dict(character_id="c", fallback_rate=rate)])
        directories.append(path)
    compare(directories, tmp_path / "report")
    assert "复核" in (tmp_path / "report/comparison.md").read_text(encoding="utf8")
    manifest["seed"] = 99
    (directories[1] / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ValueError):
        compare(directories, tmp_path / "other")


def test_metrics_recognition_copy_and_shared_fallback():
    pytest.importorskip("sklearn")
    from pipelines.style_metrics import metrics

    lines = []
    rows = []
    for cid, text in (("alpha", "合成的猫爱听故事喵"), ("beta", "今天的风景真好呀")):
        for i in range(6):
            lines.append(
                dict(
                    character_id=cid,
                    text=text + str(i),
                    source_type="main_story",
                    spoken=True,
                    split="holdout" if i == 5 else "train",
                )
            )
        rows.append(
            dict(
                character_id=cid,
                answer="今天的风景真好呀",
                fallback=True,
                rewrite=True,
                guard=True,
                shared_denial=False,
                register=False,
                http_calls=2,
                compat_retries=0,
                input_tokens=20,
                output_tokens=10,
                estimated_prompt_tokens=20,
            )
        )
    summaries, terms = metrics(rows, lines, [], 42)
    assert len(summaries) == 2
    assert all(r["shared_fallback_rate"] == 1 and r["copy_14_or_60pct"] == 1 for r in summaries)
    assert not terms


def test_public_review_requires_exact_hash_no_exception_and_denylist(tmp_path, monkeypatch):
    data = tmp_path / "Data"
    make_data(data)
    fake = tmp_path / "config/public_knowledge"
    fake.mkdir(parents=True)
    monkeypatch.setattr(corpus, "APP_ROOT", tmp_path)
    index = data / "Manifest/main_story_index.jsonl"
    records = list(corpus.read_jsonl(index))
    source = []
    for i, r in enumerate(records):
        r["canonical_url"] = f"https://example.invalid/{i}"
        path = corpus.source_path(data, r["local_path"])
        source.append(
            dict(
                canonical_url=r["canonical_url"],
                page_id=f"reviewed-{i}",
                source_revision_id=i + 1,
                fixed_revision_url=r["canonical_url"] + "?oldid=1",
                exception_status="no_page_exception_detected",
                local_sources=[
                    dict(local_path=r["local_path"], sha256=corpus.sha256(path.read_bytes()).hexdigest())
                ],
            )
        )
    jsonl(index, records)
    review = fake / "data_license_review.json"
    deny = tmp_path / "config/content_denylist.json"
    cfg = dict(schema_version="project-snow-content-denylist-1", urls=[], sha256=[], page_ids=[])

    def build(name):
        review.write_text(
            json.dumps(dict(sources=source, review_status="verified_against_bwiki_source_declaration")),
            encoding="utf8",
        )
        deny.write_text(json.dumps(cfg), encoding="utf8")
        return corpus.build(data, tmp_path / name, public_reviewed=True)

    result = build("allow")
    assert (
        result["pages"] == 2
        and result["public_review_sha256"] == corpus.sha256(review.read_bytes()).hexdigest()
    )
    source[0]["exception_status"] = "requires_review"
    source[1]["local_sources"][0]["sha256"] = "wrong"
    assert build("exclude")["pages"] == 0
    source[0]["exception_status"] = "no_page_exception_detected"
    cfg["page_ids"] = [records[0]["page_id"]]
    assert build("deny_local_id")["pages"] == 0
    cfg["page_ids"] = [source[0]["page_id"]]
    assert build("deny_review_id")["pages"] == 0
    cfg["page_ids"] = []
    cfg["urls"] = [source[0]["fixed_revision_url"]]
    assert build("deny_url")["pages"] == 0
    cfg["urls"] = []
    cfg["sha256"] = [source[0]["local_sources"][0]["sha256"]]
    assert build("deny_hash")["pages"] == 0
