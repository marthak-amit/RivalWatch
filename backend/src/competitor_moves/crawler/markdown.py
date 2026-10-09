"""HTML to markdown + links, ported from the UI's lib/markdown.js so /v1/web/scrape returns the same shape."""
import re
from html import unescape
from urllib.parse import urljoin

MAX_HTML = 2_000_000
_I = re.IGNORECASE
_TAGS = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    return _TAGS.sub("", s).strip()


def html_to_markdown(html: str, base_url: str) -> dict:
    html = html[:MAX_HTML]

    def absolute(href: str) -> str:
        try:
            return urljoin(base_url, unescape(href))
        except ValueError:
            return href

    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, _I)
    title = unescape(m.group(1)).strip() if m else ""
    md = re.sub(r"<(script|style|noscript|svg|head|template)\b[\s\S]*?</\1>", "", html, flags=_I)
    md = re.sub(r"<!--[\s\S]*?-->", "", md)
    md = re.sub(r"<h([1-4])[^>]*>([\s\S]*?)</h\1>", lambda x: f"\n\n{'#' * int(x[1])} {_strip(x[2])}\n\n", md, flags=_I)
    md = re.sub(r"<a\s[^>]*?href=[\"']([^\"']*)[\"'][^>]*>([\s\S]*?)</a>",
                lambda x: f"[{_strip(x[2])}]({absolute(x[1])})", md, flags=_I)
    md = re.sub(r"<(strong|b)\b[^>]*>([\s\S]*?)</\1>", r"**\2**", md, flags=_I)
    md = re.sub(r"<li[^>]*>", "\n- ", md, flags=_I)
    md = re.sub(r"<blockquote[^>]*>", "\n\n> ", md, flags=_I)
    md = re.sub(r"<tr[^>]*>", "\n", md, flags=_I)
    md = re.sub(r"</t[dh]>", " | ", md, flags=_I)
    md = re.sub(r"</?(p|div|section|header|footer|nav|ul|ol|table|blockquote|br|main|article|aside)\b[^>]*>",
                "\n", md, flags=_I)
    md = unescape(_TAGS.sub("", md))
    md = re.sub(r"[ \t]+", " ", md)
    md = re.sub(r" *\n *", "\n", md)
    markdown = re.sub(r"\n{3,}", "\n\n", md).strip()
    links = list(dict.fromkeys(re.findall(r"\[[^\]]*\]\((https?:[^)\s]+)\)", markdown)))
    return {"title": title, "markdown": markdown, "links": links}
