"""Search query syntax, carried over from the legacy viewer and extended with exclusions.

    word word      either word (OR)
    "exact phrase" the words in order
    word + word    both words (AND); chains like a + b + c work
    -word          exclude posts containing the word ( -"a phrase" also works)
    /regex/i       PostgreSQL regex on the raw content (flag i = case-insensitive)

Positive terms are OR'd together; exclusions apply to all of them.
Words go through Postgres' english stemmer, so "running" also finds "run".
"""
import re
from dataclasses import dataclass, field

_REGEX = re.compile(r'(?<!\S)/((?:\\/|[^/])+)/([a-z]*)')
_EXCLUDED_PHRASE = re.compile(r'(?<!\S)-"([^"]+)"')
_PHRASE = re.compile(r'"([^"]+)"')
_AND = re.compile(r'[^\s+"]+(?:\s*\+\s*[^\s+"]+)+')
_EXCLUDED_WORD = re.compile(r'(?<!\S)-([^\s"]+)')


@dataclass
class ParsedQuery:
    words: list[str] = field(default_factory=list)          # any of these
    phrases: list[str] = field(default_factory=list)
    and_groups: list[list[str]] = field(default_factory=list)
    regexes: list[tuple[str, bool]] = field(default_factory=list)  # (pattern, case_insensitive)
    excluded: list[str] = field(default_factory=list)       # words or phrases

    @property
    def positive(self) -> bool:
        return bool(self.words or self.phrases or self.and_groups or self.regexes)

    @property
    def empty(self) -> bool:
        return not (self.positive or self.excluded)


def parse_search_query(query: str) -> ParsedQuery:
    parsed = ParsedQuery()
    rest = query or ""

    def take(pattern, handle):
        nonlocal rest
        rest = pattern.sub(lambda m: (handle(m), " ")[1], rest)

    take(_REGEX, lambda m: parsed.regexes.append((m.group(1).replace("\\/", "/"), "i" in m.group(2))))
    take(_EXCLUDED_PHRASE, lambda m: parsed.excluded.append(m.group(1).strip()))
    take(_PHRASE, lambda m: parsed.phrases.append(m.group(1).strip()))
    take(_AND, lambda m: parsed.and_groups.append(
        [t for t in (p.strip() for p in m.group(0).split("+")) if t]))
    take(_EXCLUDED_WORD, lambda m: parsed.excluded.append(m.group(1)))

    parsed.words = [w for w in rest.split() if w not in ("+", "-")]
    parsed.phrases = [p for p in parsed.phrases if p]
    return parsed


def build_text_filter(parsed: ParsedQuery, prefix: str = "q") -> tuple[str | None, dict]:
    """Turn a parsed query into a SQL condition on alias `p` plus its named parameters.

    Word/phrase matching uses the tsv GIN index; regexes fall back to content scanning.
    """
    if parsed.empty:
        return None, {}

    params: dict = {}
    counter = 0

    def param(value) -> str:
        nonlocal counter
        name = f"{prefix}{counter}"
        counter += 1
        params[name] = value
        return f"%({name})s"

    tsqueries = []
    for word in parsed.words:
        tsqueries.append(f"plainto_tsquery('english', {param(word)})")
    for phrase in parsed.phrases:
        tsqueries.append(f"phraseto_tsquery('english', {param(phrase)})")
    for group in parsed.and_groups:
        tsqueries.append("(" + " && ".join(
            f"plainto_tsquery('english', {param(t)})" for t in group) + ")")

    positive = []
    if tsqueries:
        positive.append(f"p.tsv @@ ({' || '.join(tsqueries)})")
    for pattern, insensitive in parsed.regexes:
        op = "~*" if insensitive else "~"
        positive.append(f"p.content {op} {param(pattern)}")

    conditions = []
    if positive:
        conditions.append(positive[0] if len(positive) == 1 else "(" + " OR ".join(positive) + ")")
    for term in parsed.excluded:
        conditions.append(f"NOT p.tsv @@ phraseto_tsquery('english', {param(term)})")
    return " AND ".join(conditions), params
