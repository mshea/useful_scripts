import re
import html as html_mod


def _normalize_quotes(text):
    return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")


def _clean_keyword(kw):
    """Trim punctuation and normalize whitespace."""
    kw = kw.strip(" .,;:!?\"'()")
    kw = re.sub(r'\s+', ' ', kw)
    return kw.strip()


def _is_subsumed(kw_i, kw_j):
    """Return True if kw_i is fully contained in kw_j (case-insensitive)."""
    return kw_i.lower() != kw_j.lower() and kw_i.lower() in kw_j.lower()


_JUNK_WORDS = {
    "this", "that", "with", "from", "your", "have", "more", "they", "their",
    "what", "when", "where", "which", "while", "than", "then", "these", "those",
    "about", "after", "before", "between", "under", "over", "into", "just", "only",
    "also", "some", "many", "most", "other", "another", "such", "very", "much",
    "well", "make", "take", "come", "could", "would", "should", "will", "shall",
    "been", "being", "were", "was", "are", "isnt", "arent", "wasnt", "werent",
    "dont", "doesnt", "didnt", "hasnt", "havent", "hadnt", "wont", "wouldnt",
    "couldnt", "shouldnt", "cant", "cannot"
}


def extract_keywords(text, max_n=2, top=10):
    """Return a list of keyword strings extracted from text using YAKE."""
    try:
        import yake
    except ImportError:
        return []

    text = html_mod.unescape(text)
    text = _normalize_quotes(text)
    text = re.sub(r'\s+', ' ', text)

    kw_extractor = yake.KeywordExtractor(
        lan="en",
        n=max_n,
        dedupLim=0.7,
        top=top * 3,
        features=None,
    )
    raw = kw_extractor.extract_keywords(text)

    cleaned = []
    for kw, score in raw:
        kw_norm = _normalize_quotes(kw)
        kw_lower = kw_norm.lower()
        if "n't" in kw_lower or "'ve" in kw_lower or "'ll" in kw_lower or "'re" in kw_lower:
            continue
        kw_clean = _clean_keyword(kw)
        if not kw_clean or len(kw_clean) < 3:
            continue
        if re.fullmatch(r'\d+([.,]\d+)?', kw_clean):
            continue
        if " " not in kw_clean and kw_clean in _JUNK_WORDS:
            continue
        cleaned.append((kw_clean, score))

    deduped = []
    for i, (kw_i, s_i) in enumerate(cleaned):
        subsumed = False
        for j, (kw_j, s_j) in enumerate(cleaned):
            if i != j and _is_subsumed(kw_i, kw_j):
                subsumed = True
                break
        if not subsumed:
            deduped.append((kw_i, s_i))

    deduped.sort(key=lambda x: x[1])
    return [kw for kw, _ in deduped[:top]]
