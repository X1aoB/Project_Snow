"""Line-incidence log odds and evidence for human review."""

import re
from collections import Counter, defaultdict
from math import log, sqrt

SELF = {"妾身", "本天师", "本人", "本小姐", "本大爷", "小女子", "在下", "人家"}
ADDRESS = {"达令", "亲爱的", "老师", "长官", "主人", "指挥官", "队长", "殿下", "大人"}
PARTICLE = re.compile(r"^[哈嘿嘻哼喵呵啦哦嗯呜哇呐哎呦嗷咦呀诶欸嘛]+$")
CATCH = {"算卦", "卦象", "运势", "本天师", "没问题", "没错", "没关系", "包在我身上"}
COMMAND = re.compile(r"别|退|滚|闭嘴|警告|撤|不许|马上|立刻|命令|必须|攻击|消灭|站住|住手|放下|掩护|小心")


def grams(text):
    return {
        s[i : i + k]
        for s in re.findall(r"[\u3400-\u9fff]+", text)
        for k in (2, 3, 4)
        for i in range(len(s) - k + 1)
    }


def log_odds(a, n, b, m):
    # Weighted log odds for a binary feature (presence/absence in a line)
    # with a pooled empirical Dirichlet prior of 100 pseudo-lines.
    prior = (a + b + 0.5) / (n + m + 1)
    pos, neg = prior * 100, (1 - prior) * 100
    delta = log((a + pos) / (n - a + neg)) - log((b + pos) / (m - b + neg))
    return delta / sqrt(1 / (a + pos) + 1 / (b + pos))


def interval(count, total):
    p = count / total
    denominator = 1 + 3.8416 / total
    midpoint = p + 3.8416 / (2 * total)
    spread = 1.96 * sqrt(p * (1 - p) / total + 3.8416 / (4 * total**2))
    return [(midpoint - spread) / denominator, (midpoint + spread) / denominator]


def shortlist(scored, excluded, cap=45):
    selected = []
    for score, term in sorted(scored, key=lambda r: (-r[0], -len(r[1]), r[1])):
        if (
            score <= 0
            or any(term in name for name in excluded)
            or any(term in t or t in term for t in selected)
        ):
            continue
        yield score, term
        selected.append(term)
        if len(selected) == cap:
            break


def reference(row):
    return {k: row[k] for k in ("page_id", "document_id", "position")}


def mine(lines, shared, excluded):
    stories = defaultdict(list)
    all_sources = defaultdict(list)
    counts = defaultdict(Counter)
    global_counts = Counter()
    for row in lines:
        if not row["spoken"]:
            continue
        cid = row["character_id"]
        all_sources[cid].append(row)
        if row["source_type"] != "character_voice" and row["split"] == "train":
            stories[cid].append(row)
            features = grams(row["text"])
            counts[cid].update(features)
            global_counts.update(features)
    total = sum(map(len, stories.values()))
    # The candidate grams have length at most four. Index every forbidden
    # substring once instead of scanning the entire entity catalogue per row.
    forbidden = {g for name in excluded for g in grams(str(name))}
    signatures, register, topics = [], [], []
    for cid, rows in sorted(stories.items()):
        n = len(rows)
        ranked = [
            (log_odds(a, n, global_counts[t] - a, total - n), t)
            for t, a in counts[cid].items()
            if a >= 8 and t not in forbidden
        ]
        for score, term in shortlist(ranked, set()):
            matches = [r for r in rows if term in r["text"]]
            a = len(matches)
            category = (
                "self_reference"
                if term in SELF
                else "address"
                if term in ADDRESS
                else "particle"
                if PARTICLE.fullmatch(term)
                else "catchphrase"
                if term in CATCH
                else "topic"
            )
            augmented = [r for r in all_sources[cid] if r["split"] == "train"]
            signatures.append(
                dict(
                    character_id=cid,
                    term=term,
                    category=category,
                    count=a,
                    n=n,
                    rate=a / n,
                    interval=interval(a, n),
                    z=score,
                    review_status="pending",
                    with_voice_rate=sum(term in r["text"] for r in augmented) / len(augmented),
                    repeat_rate=sum(r["text"].count(term) > 1 for r in matches) / a,
                    evidence=[reference(r) for r in matches[:4]],
                )
            )
        high = [r for r in rows if r["high_stakes"]]
        daily = [r for r in rows if r["source_type"] in {"random_event", "affinity_story"}]
        if high and daily:
            h = Counter(g for r in high for g in grams(r["text"]))
            d = Counter(g for r in daily for g in grams(r["text"]))
            ranked = [
                (log_odds(a, len(high), d[t], len(daily)), t)
                for t, a in h.items()
                if a >= 3 and COMMAND.search(t) and t not in forbidden and d[t] / len(daily) < 0.01
            ]
            for score, t in shortlist(ranked, set(), 20):
                register.append(
                    dict(
                        character_id=cid,
                        term=t,
                        register="high_stakes",
                        review_status="pending",
                        daily_rate=d[t] / len(daily),
                        high_rate=h[t] / len(high),
                        count=h[t],
                        z=score,
                        evidence=[reference(r) for r in high if t in r["text"]][:4],
                    )
                )
    by_pair = defaultdict(dict)
    for r in shared:
        if r["split"] == "train":
            by_pair[r["character_id"], r["other_character_id"]][r["page_id"], r["position"]] = r
    for (cid, other), unique in sorted(by_pair.items()):
        rows = list(unique.values())
        term_counts = Counter(t for r in rows for t in grams(r["text"]))
        ranked = [
            (v * log((total + 1) / (global_counts[t] + 1)), t)
            for t, v in term_counts.items()
            if v >= 2 and t not in forbidden and len(t) >= 2
        ]
        for score, t in shortlist(ranked, set(), 6):
            r = next(r for r in rows if t in r["text"])
            topics.append(
                dict(
                    character_id=cid,
                    other_character_id=other,
                    term=t,
                    count=term_counts[t],
                    **reference(r),
                    review_status="pending",
                )
            )
    return signatures, register, topics
