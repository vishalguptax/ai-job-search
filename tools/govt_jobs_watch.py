#!/usr/bin/env python3
"""Daily watch of official Indian government recruitment pages for IT posts.

Run by .github/workflows/govt-jobs-watch.yml. For each official careers page in
SOURCES it fetches the page through tools/fetch_page.py (so robots.txt is
enforced and the request is GET-only), pulls out every link whose text looks
like an IT recruitment notice, drops obvious contract / deputation / consultant
posts, and prints a markdown report.

State lives in the rolling GitHub issue body, not in the repo: the previous
body is passed with --previous and the hashes of already-seen notices are
read from its trailing `<!-- seen: ... -->` comment. Notices not seen before
are marked NEW, and their markdown is also written to --new-out so the
workflow can post it as an issue comment (which triggers an email).

The report holds only public notice titles and URLs: no candidate data.
Whether a notice is truly permanent, the age cutoff, and the selection method
can only be read from the notice itself - the report is a lead list, verified
by hand before any application.

Usage:
  python3 tools/govt_jobs_watch.py [--previous FILE] [--new-out FILE]
"""

import argparse
import hashlib
import html
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
from page_decode import decode_page  # noqa: E402

TOOLS = Path(__file__).resolve().parent

# (organisation, official careers/recruitment page). Track A = usually exam,
# Track B = often shortlisting + interview; both are scanned the same way.
SOURCES = [
    # Central ministries / bodies. NITI Aayog and MeitY mostly hire on
    # contract or deputation; the title filter drops those, and anything left
    # is still checked by hand for "regular" wording.
    ("NITI Aayog", "https://niti.gov.in/vacancies"),
    ("MeitY", "https://www.meity.gov.in/vacancies"),
    ("SSC", "https://ssc.gov.in/"),
    ("STPI", "https://stpi.in/en/careers"),
    ("ISRO", "https://www.isro.gov.in/Careers.html"),
    ("C-DOT", "https://www.cdot.in/cdotweb/web/careers.php"),
    ("UPSC", "https://upsc.gov.in/recruitment/recruitment-advertisement"),
    ("NIC", "https://recruitment.nic.in/"),
    ("CERT-In", "https://www.cert-in.org.in/s2cMainServlet?pageid=VACANCIES"),
    ("IBPS", "https://www.ibps.in/"),
    ("SBI", "https://sbi.bank.in/web/careers/current-openings"),
    ("Bank of Baroda", "https://bankofbaroda.bank.in/career/current-opportunities"),
    ("Bank of India", "https://bankofindia.bank.in/career"),
    ("Indian Bank", "https://indianbank.bank.in/career/"),
    ("Punjab National Bank", "https://pnb.bank.in/Recruitments.aspx"),
    ("Canara Bank", "https://canarabank.bank.in/pages/Recruitment"),
    ("Union Bank of India", "https://unionbankofindia.bank.in/en/listing/recruitment"),
    ("UCO Bank", "https://ucobank.bank.in/job-opportunities"),
    ("Central Bank of India", "https://centralbank.bank.in/en/recruitments"),
    ("RBI", "https://opportunities.rbi.org.in/Scripts/Vacancies.aspx"),
    ("SEBI", "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=5&ssid=40&smid=0"),
    ("NABARD", "https://www.nabard.org/careers-notices1.aspx?cid=693&id=26"),
    ("SIDBI", "https://www.sidbi.in/en/careers"),
    ("CRIS", "https://cris.org.in/"),
    ("C-DAC", "https://www.cdac.in/index.aspx?id=current_jobs"),
    ("BEL", "https://bel-india.in/job-notifications/"),
    ("RailTel", "https://www.railtel.in/careers/current-jobs.html"),
]

# A link must match one of these to count as an IT notice.
IT_TERMS = re.compile(
    r"\b(IT|I\.T\.|information technology|software|programmer|system analyst|"
    r"systems? officer|computer|developer|data (?:processing|scien\w*|analy\w*)|"
    r"scientist[- ]?['\"]?b|specialist (?:cadre )?officers?|SCO|SO\b|"
    r"technical assistant|cyber|digital|full[- ]?stack|front[- ]?end|"
    r"artificial intelligence|\bAI\b|engineer)",
    re.IGNORECASE,
)
# A link matching one of these is dropped: not a permanent post, or not a notice.
EXCLUDE_TERMS = re.compile(
    r"(contract|contractual|deputation|consultant|young professional|apprentice|"
    r"tenure|outsourc|project[- ]based|\bproject (?:engineer|manager|associate|support|scientist)|"
    r"retired|internship|empanel|walk[- ]?in|"
    r"result|answer key|admit card|call letter|marks|cut[- ]?off|corrigendum to result|"
    r"selected|shortlisted|select list|selection panel|withheld|postponed|"
    r"scheduled on|\bbot\b|"
    r"tender|rfp|eoi|expression of interest|auction|quotation)",
    re.IGNORECASE,
)
# Only links that read like a recruitment notice.
NOTICE_TERMS = re.compile(
    r"(recruit|vacanc|advertisement|advt|notification|engagement|appointment|"
    r"post of|posts|officer|scientist|programmer|analyst|assistant|manager|"
    r"specialist|apply|careers?)",
    re.IGNORECASE,
)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append((text, self._href))
            self._href = None


def fetch(url: str, tmp: Path) -> tuple[str | None, str]:
    out = tmp / (hashlib.sha1(url.encode()).hexdigest() + ".html")
    try:
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "fetch_page.py"), url, "--out", str(out)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    out_lines = proc.stdout.strip().splitlines()
    http = [ln for ln in out_lines if ln.startswith("HTTP ")]
    status = (http or out_lines or [proc.stderr.strip() or "no output"])[-1]
    if proc.returncode != 0 or not out.exists():
        return None, status
    if not status.startswith("HTTP 200"):
        return None, status
    return decode_page(out.read_bytes()), status


def notices(org: str, base: str, page: str) -> list[dict]:
    parser = LinkParser()
    parser.feed(page)
    found, seen_urls = [], set()
    for text, href in parser.links:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        title = html.unescape(text)
        if len(title) < 12 or len(title) > 400:
            continue
        if not IT_TERMS.search(title) or not NOTICE_TERMS.search(title):
            continue
        if EXCLUDE_TERMS.search(title):
            continue
        url = urljoin(base, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        key = hashlib.sha1(f"{org}|{title.lower()}|{url}".encode()).hexdigest()[:12]
        found.append({"org": org, "title": title, "url": url, "key": key})
    return found


def previous_seen(path: str | None) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    m = re.search(r"<!-- seen: ([0-9a-f ]*) -->", Path(path).read_text(encoding="utf-8"))
    return set(m.group(1).split()) if m else set()


def md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--previous", help="previous rolling-issue body (state)")
    ap.add_argument("--new-out", help="write markdown for NEW notices here")
    args = ap.parse_args(argv)

    seen = previous_seen(args.previous)
    first_run = not seen
    all_items, failures = [], []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for org, url in SOURCES:
            page, status = fetch(url, tmp)
            if page is None:
                failures.append((org, url, status))
                continue
            all_items.extend(notices(org, url, page))

    new = [i for i in all_items if i["key"] not in seen]
    lines = [
        "Leads only. Every notice must be opened and checked for: **regular/permanent** "
        "wording, **age limit vs DOB on the cutoff date**, **selection method** "
        "(exam vs shortlisting + interview), GATE requirement, and last date.",
        "",
        f"**{len(all_items)}** IT-looking notices on {len(SOURCES) - len(failures)}/{len(SOURCES)} "
        f"official pages; **{len(new)} new** since last run"
        + (" (first run: everything counts as new)" if first_run else "") + ".",
        "",
    ]
    by_org: dict[str, list[dict]] = {}
    for i in all_items:
        by_org.setdefault(i["org"], []).append(i)
    for org, items in by_org.items():
        lines.append(f"### {org}")
        for i in items:
            flag = "🆕 " if i["key"] not in seen else ""
            lines.append(f"- {flag}[{md_escape(i['title'])}]({i['url']})")
        lines.append("")
    if failures:
        lines.append("<details><summary>Pages not read this run "
                     f"({len(failures)})</summary>\n")
        for org, url, status in failures:
            lines.append(f"- {org}: {url} - `{status[:120]}`")
        lines.append("\n</details>\n")
    keys = sorted(seen | {i["key"] for i in all_items})
    lines.append(f"<!-- seen: {' '.join(keys)} -->")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))

    if args.new_out:
        body = ""
        if new and not first_run:
            body = "New IT notices on official recruitment pages:\n\n" + "\n".join(
                f"- **{i['org']}**: [{md_escape(i['title'])}]({i['url']})" for i in new
            )
        Path(args.new_out).write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
