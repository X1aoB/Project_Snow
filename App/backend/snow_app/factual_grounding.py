"""Small deterministic boundaries between relationship language and facts.

These checks do not infer canon from word frequency. Reviewed public knowledge
is authoritative; quotes, questions, negations and wishes cannot establish it.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .dialogue_persona import attributed_lines


_QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')
_SENTENCES = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_NON_ASSERTION = re.compile(
    r"不是|并非|并不|并未|不曾|没有|还没|尚未|未曾|从未|不算|不能说|不代表|不意味着|没(?:有)?(?:立|结)|"
    r"不要说|别说|误以为|误认为|误说|说错|谣言|传言|不该|不应|"
    r"如果|假如|假设|要是|倘若|万一|等到|将来|未来|有朝一日|总有一天|"
    r"希望|愿望|想象|梦想|想(?:要)?(?:和|跟|与|成为|当)|愿意|盼望|期待|打算|准备|"
    r"吗|么|是否|是不是|能不能|会不会|还未|尚无"
)
_CONDITIONAL_SCOPE = re.compile(r"如果|假如|假设|要是|倘若|万一|等到|等(?:我们|咱们|咱俩|我和你|你和我)|有朝一日|总有一天|希望|想象|梦想|^(?:以后|将来|未来)")
_REPORTED_OR_DENIED_ASSERTION = re.compile(
    r"^\s*(?:(?:你|分析员)(?:还|一直|仍然|也|曾经|之前|竟然|居然){0,2}(?:以为|觉得|认为)|"
    r"我(?:可|也|并|根本){0,2}没(?:有)?(?:说过?|讲过?|声称过?|承认过?))"
)
_FORMAL = re.compile(r"恒约|妻子|老婆|夫妻|夫妇|婚礼|结过?婚|成婚|成亲|丈夫|老公|嫁给|嫁了|已婚")
_COMPLETED = r"(?:(?:可是|确实|的确|现在|已经|早已|早就|已然|一同|一起|正式|都|也|早|已|了|过|是|有|立下|缔结|定下|结下|签订|结成|有了|一直是|本来就是|现在是|的){0,5})"
_SELF_BOND = re.compile(
    r"(?:我们|咱们|咱俩|我(?:和|跟|与)(?:你|分析员)|(?:你|分析员)(?:和|跟|与)我)" + _COMPLETED
    + r"(?:恒约|结过?婚|成婚|成亲|夫妻|夫妇|婚礼)|"
    r"我(?:已经|早已|早就|已|都|也)?(?:结过?婚|成婚|成亲)|"
    r"我(?:已经|早已|早就|已)(?:嫁给|嫁了)(?:你|分析员)|我(?:嫁给|嫁了)(?:你|分析员)了|"
    r"我(?:已经|早已|早就|已|现在|也|就){0,3}是已婚|"
    r"我(?:已经|早已|早就|现在|当然|本来|确实|的确|可|也|就|仍然|一直){0,3}(?:是|成为|当上|做了)(?:你|分析员)的?(?:妻子|老婆|丈夫|老公)|"
    r"(?:你的|分析员的)(?:妻子|老婆|丈夫|老公)(?:就是|正是|是)我|"
    r"(?:你|分析员)(?:已经|早已|早就|现在|当然|本来|确实|的确|可|也|就|仍然|一直){0,3}(?:是|成为|当上|做了)我的?(?:妻子|老婆|丈夫|老公)|"
    r"以(?:你|分析员)的?(?:妻子|老婆|丈夫|老公)(?:身份|名义)|"
    r"(?:自从|既然).{0,12}(?:恒约|结婚)|(?:恒约|结婚)(?:之)?后"
)
_ATTRIBUTION_FIELD = re.compile(r"^\s*(?:角色|说话人|speaker|对话|台词|voice_excerpt)\s*[=:：]", re.MULTILINE)
_STRUCTURED_FIELD = re.compile(r"^\s*[\w\u4e00-\u9fff]+\s*[=:：]")


def relationship_body(document: dict[str, Any], speaker_names: set[str]) -> str:
    """Strip normalized record headers and preserve speaker scope across lines.

    A page can contain several speakers. Once it uses explicit dialogue
    fields, only attributed lines by the selected character are eligible.
    Ordinary prose documents remain available after field headers are removed.
    """
    text = str(document.get("text") or "")
    if _ATTRIBUTION_FIELD.search(text):
        return "\n".join(quote for _speaker, quote in attributed_lines(document, speaker_names))
    body = []
    for line in text.splitlines():
        if _STRUCTURED_FIELD.match(line):
            match = re.match(r"^\s*(?:正文|邮件正文|故事正文|内容|text)\s*[=:：]\s*(.*)$", line)
            if match:
                body.append(match.group(1))
        elif not line.startswith(("剧情格式文本", "剧情旁白文本", "剧情选项")):
            body.append(line)
    return "\n".join(body)


def affirmative_clauses(text: str, *, keep_quotes: bool = False) -> list[str]:
    """Conservative assertion candidates, preserving contrast-clause polarity.

    A condition carries across its comma clauses; a local negation does not.
    Adversatives begin a new scope. Rhetorical "不是吗" is not a negation.
    Reporting the listener's belief or denying a previous utterance does not
    assert its content; the speaker's own affirmative belief remains a claim.
    """
    value = str(text or "")
    if not keep_quotes:
        value = _QUOTED.sub("", value)
    result = []
    for sentence in _SENTENCES.findall(value):
        sentence = re.sub(r"[，,]?(?:不是吗|对吧|是吧)[?？]?$", "", sentence)
        # 可是 after a subject is emphasis, including the copula in
        # "我可是你的妻子". Split it only at a sentence/comma boundary;
        # preserving it also retains named subjects for later attribution.
        for segment in re.split(r"但是|不过|然而|(?:^|[，,]\s*)可是|但(?=[^愿])", sentence):
            conditional = False
            for clause in re.split(r"[，,]", segment):
                conditional = conditional or bool(_CONDITIONAL_SCOPE.search(clause))
                if (conditional or clause.rstrip().endswith(("?", "？")) or _NON_ASSERTION.search(clause)
                        or _REPORTED_OR_DENIED_ASSERTION.search(clause)):
                    continue
                if clause.strip():
                    result.append(clause.strip())
    return result


def relationship_evidence(
    text: str, terms: Iterable[str], *, other_names: Iterable[str] = (),
) -> tuple[list[str], str]:
    """Return affirmative body evidence, never a title or another person's line."""
    # A named third party in a sentence can be the speaker of the following
    # comma clause. Do not reassign that line to the document's owner.
    passages = [clause for sentence in _SENTENCES.findall(str(text or ""))
                if not any(name and name in sentence for name in other_names)
                for clause in affirmative_clauses(sentence)]
    supported = [clause for clause in passages if any(term in clause for term in terms)]
    matches = [term for term in terms if any(term in clause for clause in supported)]
    return list(dict.fromkeys(matches)), " ".join(supported)[:520]


def unsupported_formal_claims(
    answer: str, *, speaker_names: Iterable[str], other_names: Iterable[str],
) -> bool:
    """Detect an asserted self marriage, retaining affection and future wishes.

    Explicit third-person reporting is handled by roster validation; it must
    not be interpreted as the current speaker's own relationship.
    """
    for sentence in affirmative_clauses(answer):
        for clause in re.split(r"[，,]", sentence):
            if not _FORMAL.search(clause):
                continue
            mentions_other = any(name and name in clause for name in other_names)
            mentions_self = any(name and name in clause for name in speaker_names)
            if mentions_other and not mentions_self and not re.search(r"我们|咱们|咱俩|我(?:已|是|和|与|跟)", clause):
                continue
            # Names in third-person action blocks act as the current speaker,
            # but the same relationship predicate is still required.
            normalized = clause
            if mentions_self:
                for name in sorted(speaker_names, key=len, reverse=True):
                    if name:
                        normalized = normalized.replace(name, "我")
            # Keep completed markers after the joint subject, including the
            # common "我已经和你立下了恒约" ordering.
            normalized = re.sub(r"我((?:已经|早已|早就|正式|也|已){1,3})((?:和|跟|与)(?:你|分析员))", r"我\2\1", normalized)
            if _SELF_BOND.search(normalized):
                return True
    return False
