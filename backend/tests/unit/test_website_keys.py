"""Document key assignment for one crawl: canonical URLs, ownership, duplicates."""

from app.ingestion.website import KeyAssignment, KeyCandidate, assign_document_keys

S = "https://shop.example.com"


def page(path: str, content: str, canonical: str | None = None) -> KeyCandidate:
    return KeyCandidate(
        own=S + path, canonical=S + canonical if canonical else None, content_hash=content
    )


def owns(path: str) -> KeyAssignment:
    return KeyAssignment(S + path, None)


def duplicate(path: str) -> KeyAssignment:
    return KeyAssignment(S + path, S + path)


def test_a_canonical_claim_cannot_take_over_a_real_page() -> None:
    # /a (A) declares /b canonical, /b (B) is a real page, /a comes first.
    assert assign_document_keys([page("/a", "A", "/b"), page("/b", "B")]) == [
        owns("/a"),
        owns("/b"),
    ]
    # Same result in the other order: deterministic.
    assert assign_document_keys([page("/b", "B"), page("/a", "A", "/b")]) == [
        owns("/b"),
        owns("/a"),
    ]


def test_an_alias_of_a_real_page_is_its_duplicate_whatever_the_order() -> None:
    assert assign_document_keys([page("/a", "X", "/b"), page("/b", "X")]) == [
        duplicate("/b"),
        owns("/b"),
    ]
    assert assign_document_keys([page("/b", "X"), page("/a", "X", "/b")]) == [
        owns("/b"),
        duplicate("/b"),
    ]


def test_a_canonical_no_fetched_page_lives_at_goes_to_the_first_claimant() -> None:
    # Both declare /p (never fetched). Same content: one document; different: fall back.
    same = assign_document_keys([page("/p?ref=a", "X", "/p"), page("/p?ref=b", "X", "/p")])
    assert same == [owns("/p"), duplicate("/p")]
    different = assign_document_keys([page("/p?ref=a", "X", "/p"), page("/p?ref=b", "Y", "/p")])
    assert different == [owns("/p"), owns("/p?ref=b")]


def test_identical_content_without_canonicals_keeps_the_first_page() -> None:
    assert assign_document_keys([page("/faq", "X"), page("/help", "X")]) == [
        owns("/faq"),
        duplicate("/faq"),
    ]


def test_a_duplicate_is_never_of_itself() -> None:
    # /q declares /p canonical (same content as /p); /p declares /r (never fetched),
    # and /r's group is seen first. /p must not end up "a duplicate of /p".
    pages = [page("/q", "X", "/p"), page("/p", "X", "/r")]
    result = assign_document_keys(pages)
    for candidate, assignment in zip(pages, result, strict=True):
        assert assignment.duplicate_of != candidate.own
    assert sum(1 for a in result if a.duplicate_of is None) == 1  # one owner, one document


def test_distinct_pages_all_keep_their_own_documents() -> None:
    pages = [page("/", "H"), page("/a", "A", "/b"), page("/b", "B", "/a"), page("/c", "C")]
    assert assign_document_keys(pages) == [owns("/"), owns("/a"), owns("/b"), owns("/c")]
