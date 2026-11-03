"""robots.txt parsing and matching, following RFC 9309."""

import pytest

from app.crawler.robots import MAX_ROBOTS_BYTES, RobotsRules, parse_robots

TOKEN = "KnowbaseBot"


def rules(text: str) -> RobotsRules:
    return parse_robots(text, TOKEN)


def test_disallowed_paths_are_refused_and_others_allowed() -> None:
    robots = rules("User-agent: *\nDisallow: /private/\nDisallow: /tmp")
    assert not robots.allows("/private/report")
    assert not robots.allows("/tmp")
    assert not robots.allows("/tmpfiles")  # prefix match
    assert robots.allows("/public/page")
    assert robots.allows("/")


def test_longest_match_wins_and_allow_wins_a_tie() -> None:
    robots = rules("User-agent: *\nDisallow: /\nAllow: /docs\nDisallow: /docs/internal")
    assert robots.allows("/docs/guide")
    assert not robots.allows("/docs/internal/x")
    assert not robots.allows("/pricing")
    tie = rules("User-agent: *\nDisallow: /page\nAllow: /page")
    assert tie.allows("/page")


def test_wildcards_and_end_anchor() -> None:
    robots = rules("User-agent: *\nDisallow: /*.php$\nDisallow: /*?sessionid=\nDisallow: /a*/b")
    assert not robots.allows("/index.php")
    assert robots.allows("/index.php?x=1")  # $ anchors the end
    assert not robots.allows("/shop?sessionid=42")
    assert not robots.allows("/abc/b/c")
    assert robots.allows("/abc/c")


def test_our_own_group_beats_the_wildcard_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: knowbasebot\nDisallow: /drafts\n"
    robots = rules(text)
    assert robots.allows("/products")  # only our group applies
    assert not robots.allows("/drafts/1")


def test_other_bots_groups_do_not_apply() -> None:
    robots = rules("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /cart\n")
    assert robots.allows("/products")
    assert not robots.allows("/cart")


def test_agent_matching_ignores_case_and_versions() -> None:
    robots = rules("User-agent: KNOWBASEBOT/2.0\nDisallow: /x\n")
    assert not robots.allows("/x")


def test_groups_for_the_same_agent_are_merged() -> None:
    text = (
        "User-agent: KnowbaseBot\nDisallow: /a\n\n"
        "User-agent: Other\nDisallow: /b\n\n"
        "User-agent: KnowbaseBot\nDisallow: /c\n"
    )
    robots = rules(text)
    assert not robots.allows("/a") and not robots.allows("/c")
    assert robots.allows("/b")


def test_consecutive_user_agent_lines_share_one_group() -> None:
    robots = rules("User-agent: Googlebot\nUser-agent: KnowbaseBot\nDisallow: /shared\n")
    assert not robots.allows("/shared")


def test_empty_disallow_and_empty_file_allow_everything() -> None:
    assert rules("User-agent: *\nDisallow:\n").allows("/anything")
    assert rules("").allows("/anything")
    assert rules("# just a comment\n").allows("/anything")


def test_rules_before_any_user_agent_are_ignored() -> None:
    assert rules("Disallow: /\nUser-agent: *\nDisallow: /x\n").allows("/y")


def test_comments_and_whitespace_are_tolerated() -> None:
    robots = rules("  user-agent : *   # everyone\n  disallow : /secret  # keep out\n")
    assert not robots.allows("/secret")


def test_robots_txt_itself_is_always_allowed() -> None:
    assert rules("User-agent: *\nDisallow: /\n").allows("/robots.txt")


def test_percent_encoding_is_compared_consistently() -> None:
    robots = rules("User-agent: *\nDisallow: /%7Euser/\nDisallow: /caf%C3%A9\n")
    assert not robots.allows("/~user/home")
    assert not robots.allows("/caf%c3%a9")


def test_crawl_delay_and_sitemaps_are_read() -> None:
    robots = rules(
        "Sitemap: https://shop.example.com/sitemap-a.xml\n"
        "User-agent: *\nCrawl-delay: 3\nDisallow: /x\n"
        "Sitemap: https://shop.example.com/sitemap-b.xml\n"
    )
    assert robots.crawl_delay == 3
    assert robots.sitemaps == (
        "https://shop.example.com/sitemap-a.xml",
        "https://shop.example.com/sitemap-b.xml",
    )
    assert rules("User-agent: *\nCrawl-delay: 9999\n").crawl_delay == 60  # capped
    assert rules("User-agent: *\nCrawl-delay: soon\n").crawl_delay is None


def test_only_the_first_500_kib_are_parsed() -> None:
    padding = "# " + "x" * MAX_ROBOTS_BYTES + "\n"
    robots = parse_robots(("User-agent: *\n" + padding + "Disallow: /late\n").encode(), TOKEN)
    assert robots.allows("/late")


@pytest.mark.parametrize("path", ["/", "/anything"])
def test_deny_everything_policy(path: str) -> None:
    assert not RobotsRules.deny_everything().allows(path)
    assert RobotsRules.allow_everything().allows(path)
