"""Offline distributional summaries. Synthetic dry runs never produce quality scores."""

import math
import statistics
from collections import defaultdict

from backend.snow_app.mvp_service import _compact

from .build_dialogue_profiles import STYLE_PARTICLES


def metrics(rows, lines, signatures, seed, threshold=14):
    import numpy as np
    from scipy.spatial.distance import jensenshannon
    from scipy.stats import wasserstein_distance
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    training = [
        r for r in lines if r["spoken"] and r["source_type"] != "character_voice" and r["split"] == "train"
    ]
    held = [
        r for r in lines if r["spoken"] and r["source_type"] != "character_voice" and r["split"] == "holdout"
    ]
    vectorizer = TfidfVectorizer(
        analyzer="char", ngram_range=(2, 4), min_df=2, sublinear_tf=True, max_features=65000
    )
    x = vectorizer.fit_transform([r["text"] for r in training])
    y = np.array([r["character_id"] for r in training])
    model = LogisticRegression(max_iter=300, random_state=seed, class_weight="balanced").fit(x, y)
    originals = "\0".join(_compact(r["text"]) for r in lines if r["spoken"])
    gens = vectorizer.transform([r["answer"] for r in rows])
    predictions = model.predict(gens)
    held_predictions = model.predict(vectorizer.transform([r["text"] for r in held])) if held else []
    fallbacks = defaultdict(set)
    for r in rows:
        if r["fallback"]:
            fallbacks[r["answer"]].add(r["character_id"])

    def distribution(texts):
        return np.array(
            [
                sum(s.count(c) for s in texts) + 0.5
                for c in (*STYLE_PARTICLES, "。", "？", "！", "，", "……", "~")
            ]
        )

    def copied(answer, min_chars):
        s = _compact(answer)
        k = min(min_chars, max(1, math.ceil(len(s) * 0.6)))
        return bool(s) and any(s[i : i + k] in originals for i in range(len(s) - k + 1))

    groups = sorted({r["character_id"] for r in rows})
    summaries, term_rows = [], []
    for cid in groups:
        indexes = [i for i, r in enumerate(rows) if r["character_id"] == cid]
        selected = [rows[i] for i in indexes]
        replies = [r["answer"] for r in selected]
        refs = [r["text"] for r in training if r["character_id"] == cid]
        lengths = [len(s) for s in replies]
        similarity = []
        for other in groups:
            if other != cid:
                # Dot of average unit vectors = mean pairwise cosine.
                other_indices = [i for i, r in enumerate(rows) if r["character_id"] == other]
                baseline = float(
                    np.inner(
                        np.asarray(x[y == cid].mean(axis=0)).ravel(),
                        np.asarray(x[y == other].mean(axis=0)).ravel(),
                    )
                )
                observed = float(
                    np.inner(
                        np.asarray(gens[indexes].mean(axis=0)).ravel(),
                        np.asarray(gens[other_indices].mean(axis=0)).ravel(),
                    )
                )
                similarity.append((observed, baseline))
        held_correct = [pred == cid for pred, r in zip(held_predictions, held) if r["character_id"] == cid]
        fallback_count = sum(r["fallback"] for r in selected)
        n = len(selected)
        row = dict(
            character_id=cid,
            n=n,
            length_median=statistics.median(lengths),
            length_iqr=float(np.percentile(lengths, 75) - np.percentile(lengths, 25)),
            length_wasserstein=wasserstein_distance(lengths, [len(s) for s in refs]),
            particle_punctuation_js=float(
                jensenshannon(distribution(replies), distribution(refs), base=2) ** 2
            ),
            recognition=float(np.mean(predictions[indexes] == cid)),
            held_recognition=float(np.mean(held_correct)) if held_correct else None,
            convergence=(
                sum(v for v, _ in similarity) / sum(v for _, v in similarity)
                if sum(v for _, v in similarity) > 0
                else None
            ),
            shared_fallback_rate=(
                sum(r["fallback"] and len(fallbacks[r["answer"]]) > 1 for r in selected) / fallback_count
                if fallback_count
                else None
            ),
        )
        for k in dict.fromkeys((12, threshold)):
            row[f"copy_{k}_or_60pct"] = sum(copied(s, k) for s in replies) / n
        for field in ("guard", "rewrite", "fallback", "register", "shared_denial"):
            row[field + "_rate"] = sum(r[field] for r in selected) / n
        for field in (
            "http_calls",
            "compat_retries",
            "input_tokens",
            "output_tokens",
            "estimated_prompt_tokens",
        ):
            row[field] = sum(r[field] for r in selected)
        for sig in signatures:
            if sig["character_id"] != cid or sig["category"] == "topic":
                continue
            counts = [s.count(sig["term"]) for s in replies]
            occurrence = sum(c > 0 for c in counts) / n
            term_rows.append(
                dict(
                    character_id=cid,
                    term=sig["term"],
                    review_status=sig["review_status"],
                    generated_rate=occurrence,
                    source_rate=sig["rate"],
                    max_per_reply=max(counts),
                    rate_ratio=occurrence / sig["rate"] if sig["rate"] else None,
                )
            )
        summaries.append(row)
    return summaries, term_rows
