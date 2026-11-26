"""
Fill in a role from a link (Add role → From a link).

Reads a public job posting and proposes values for the Add role form; nothing is saved
until you review the form and add the role. In order, each step filling only what the
earlier ones left blank:

  1. Job boards the tracker already scrapes:
       Indeed        Indeed's job API (the one JobSpy uses), by the job key in the link
       LinkedIn      the public job page (title, company, location, description, pay)
       ZipRecruiter  JobSpy's job-page reader, for the description
       Glassdoor     JobSpy's job-details query, for the description
  2. Any other page: the schema.org JobPosting data most career sites publish for
     Google Jobs (title, company, location, pay, date, description), then the page's
     meta tags and its readable text (pay and years are read from the text with the
     same rules as a pull).
  3. Optionally Claude reads the page and fills in what's left. It never writes the
     description: it only marks where the description starts and ends in the page
     text, and that span is copied exactly as it is (see cut_description).

Only public pages can be read: links to this computer or your local network are
refused, and pages behind a sign-in come back as a clear message.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qs, urljoin, urlsplit

import db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
TIMEOUT = 15            # seconds per request
MAX_BYTES = 4_000_000   # read at most this much of a page
MAX_REDIRECTS = 5
AI_TEXT_CHARS = 40_000  # page text sent to the AI (about 10K tokens)

# Fields the Add role form can take from a link (all optional).
FIELDS = ("title", "company", "location", "is_remote", "pay_min", "pay_max", "years_min",
          "years_max", "job_type", "date_posted", "description", "job_url", "site")
JOB_TYPES = ("fulltime", "parttime", "contract", "internship", "temporary")


class ImportProblem(Exception):
    """Something the user should be told (bad link, page refused, not a job posting...).
    retry: a headless browser might still get the page (refused or timed-out request)."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


# ----------------------------------------------------------------------------
# Fetching a page safely
# ----------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """A cleaned-up http(s) link; raises ImportProblem for anything else."""
    url = (url or "").strip()
    if not url:
        raise ImportProblem("Paste the link to a job posting.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ImportProblem("That doesn't look like a web link (it should start with https://).")
    if parts.username or parts.password:
        raise ImportProblem("Links with a user name or password in them aren't read.")
    return url


def _check_public(url: str):
    """Refuse links that point at this computer or a private network (the tracker runs on
    your machine, so a link must not be able to reach other services on it)."""
    parts = urlsplit(url)
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise ImportProblem(f"Couldn't find the site {parts.hostname}. Check the link.") from None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _ip_is_public(ip):
            raise ImportProblem(_NOT_PUBLIC)
        ips.append(ip)
    return ips


_NOT_PUBLIC = "That link points to this computer or your local network; only public job pages can be read."


def _ip_is_public(ip) -> bool:
    return ip.is_global and not ip.is_multicast


class _PrivateAddress(OSError):
    """A connection that would have gone to this computer or a private network."""


def _public_only_session():
    """A requests session whose direct connections are checked when the socket connects,
    before anything is sent (before the TLS handshake too). The name is looked up again
    when connecting, so a site could answer the check in _check_public with a public
    address and the connection with a private one (DNS rebinding); this catches that, on
    every connection, however the server closes it. Connections through a proxy set in
    the environment go to the proxy and aren't checked."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    def checked(base):
        class Checked(base):
            def _new_conn(self):
                sock = super()._new_conn()
                try:
                    peer = sock.getpeername()[0]
                    public = _ip_is_public(ipaddress.ip_address(peer.split("%")[0]))
                except (OSError, ValueError):
                    public = False
                if not public:
                    sock.close()
                    raise _PrivateAddress(_NOT_PUBLIC)
                return sock
        return Checked

    class Pool(HTTPConnectionPool):
        ConnectionCls = checked(HTTPConnection)

    class SecurePool(HTTPSConnectionPool):
        ConnectionCls = checked(HTTPSConnection)

    class PublicOnly(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": SecurePool}

    session = requests.Session()
    adapter = PublicOnly(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _caused_by_private(e, depth=0) -> bool:
    """True if a requests/urllib3 error was raised by the private-address check."""
    if e is None or depth > 8:
        return False
    if isinstance(e, _PrivateAddress):
        return True
    nested = [e.__cause__, e.__context__, getattr(e, "reason", None)] + list(getattr(e, "args", ()))
    return any(_caused_by_private(x, depth + 1) for x in nested if isinstance(x, BaseException))


def fetch_page(url: str, session=None) -> tuple[str, str]:
    """(final URL, page text) for a public page. Every redirect is checked too.
    Raises ImportProblem with a message for the user."""
    import requests
    session = session or _public_only_session()
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url)
        try:
            resp = session.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                             "Accept-Language": "en-US,en;q=0.9"},
                               timeout=TIMEOUT, allow_redirects=False, stream=True)
        except requests.RequestException as e:
            if _caused_by_private(e):
                raise ImportProblem(_NOT_PUBLIC) from None
            raise ImportProblem(f"Couldn't load the page ({type(e).__name__}).", retry=True) from None
        with resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code in (429, 999):
                raise ImportProblem(f"The site is limiting automated visits right now (HTTP {resp.status_code}). "
                                    "Wait a minute and try again, or fill in the fields yourself.", retry=True)
            if resp.status_code in (401, 403, 407):
                raise ImportProblem(f"The site refused the request (HTTP {resp.status_code}). It may need a sign-in "
                                    "or block automated visits. Fill in the fields yourself, or paste the description.",
                                    retry=True)
            if resp.status_code == 404 or resp.status_code == 410:
                raise ImportProblem(f"The page wasn't found (HTTP {resp.status_code}). The posting may have closed.")
            if resp.status_code >= 400:
                raise ImportProblem(f"The site answered with an error (HTTP {resp.status_code}).")
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and not any(t in ctype for t in ("html", "xml", "json", "text/plain")):
                raise ImportProblem(f"That link is a file ({ctype.split(';')[0]}), not a web page.")
            body = b""
            for chunk in resp.iter_content(65536):
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            enc = resp.encoding if "charset" in ctype else None
            return url, body.decode(enc or "utf-8", errors="replace")
    raise ImportProblem("The page redirected too many times.")


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
_STATE_CODES = {}


def _state_code(name: str) -> str | None:
    if not _STATE_CODES:
        import daily_jobs
        _STATE_CODES.update({v: k.upper() for k, v in daily_jobs.US_STATES.items()})
    s = (name or "").strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _STATE_CODES.get(s.lower())


_US = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


def place(city=None, region=None, country=None) -> str | None:
    """'San Francisco', 'California', 'USA' -> 'San Francisco, CA' (other countries are kept)."""
    city, region, country = (str(x).strip() if x else "" for x in (city, region, country))
    parts = [city] if city else []
    if region:
        parts.append(_state_code(region) or region if (not country or country.lower() in _US) else region)
    if country and country.lower() not in _US:
        parts.append(country)
    return ", ".join(dict.fromkeys(p for p in parts if p)) or None


def html_to_markdown(fragment: str | None) -> str | None:
    """Job description HTML -> Markdown, same words (also undoes HTML that was escaped twice)."""
    if not fragment or not str(fragment).strip():
        return None
    text = str(fragment)
    if "<" not in text and "&lt;" in text:
        text = html_lib.unescape(text)
    from markdownify import markdownify
    md = markdownify(text, heading_style="ATX", strip=["img", "script", "style"])
    md = re.sub(r"[ \t]+\n", "\n", md.replace("\xa0", " "))
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md or None


def _date(value) -> str | None:
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _plain(v):
    """JobSpy enums -> their plain value (JobType.FULL_TIME -> 'fulltime', Country.USA -> 'usa')."""
    if hasattr(v, "value"):
        v = v.value
        return v[0].split(",")[0] if isinstance(v, tuple) else v
    return v


def _job_type(value) -> str | None:
    vals = value if isinstance(value, list) else [value]
    for v in vals:
        k = re.sub(r"[^a-z]", "", str(_plain(v) or "").lower())
        for jt, keys in (("fulltime", ("fulltime", "permanent")), ("parttime", ("parttime",)),
                         ("contract", ("contract", "contractor", "freelance")),
                         ("internship", ("intern", "internship")), ("temporary", ("temporary", "temp", "seasonal"))):
            if k in keys:
                return jt
    return None


INTERVALS = {"HOUR": "hourly", "DAY": "daily", "WEEK": "weekly", "MONTH": "monthly", "YEAR": "yearly"}


def _annual(lo, hi, unit) -> tuple[float | None, float | None]:
    import daily_jobs
    mult = daily_jobs.INTERVAL_MULT.get(INTERVALS.get(str(unit or "YEAR").upper(), "yearly"), 1)
    nums = []
    for x in (lo, hi):
        try:
            nums.append(float(str(x).replace(",", "")) * mult if x not in (None, "") else None)
        except ValueError:
            nums.append(None)
    lo, hi = nums
    lo, hi = lo or hi, hi or lo
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi


# ----------------------------------------------------------------------------
# Any page: schema.org JobPosting, meta tags, readable text
# ----------------------------------------------------------------------------
def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def job_posting_data(soup) -> dict | None:
    """The page's schema.org JobPosting object (from <script type="application/ld+json">), if any."""
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        raw = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[)|(-->|//\s*\]\]>)\s*$", "", raw)
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(str(t).lower() == "jobposting" for t in types):
                return node
    return None


def from_job_posting(jp: dict) -> dict:
    out: dict = {}
    out["title"] = html_lib.unescape(str(jp.get("title") or "")).strip() or None
    org = jp.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else org
    out["company"] = html_lib.unescape(company).strip() or None if isinstance(company, str) else None
    locs = jp.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    places = []   # (has a city, in the US, text)
    for loc in locs:
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            p = place(addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")
                      if not isinstance(addr.get("addressCountry"), dict) else addr["addressCountry"].get("name"))
        elif isinstance(addr, str):
            p = addr.strip()
        else:
            p = None
        if p and p not in [x[2] for x in places]:
            country = addr.get("addressCountry") if isinstance(addr, dict) else None
            country = country.get("name") if isinstance(country, dict) else country
            has_city = bool(isinstance(addr, dict) and addr.get("addressLocality")) or isinstance(addr, str)
            places.append((has_city, not country or str(country).lower() in _US, p))
    remote = "telecommute" in str(jp.get("jobLocationType") or "").lower()
    cities = [x for x in places if x[0]]
    if cities:
        # Several offices: show the first one with a city, US first, and how many more. Places
        # that only name a country ("USA") are left out: the AI or you can fill those in.
        best = sorted(cities, key=lambda x: not x[1])[0][2]
        out["location"] = best if len(locs) == 1 else f"{best} (+{len(locs) - 1} more)"
    elif remote:
        out["location"] = "Remote"
    if remote:
        out["is_remote"] = True
    sal = jp.get("baseSalary") or jp.get("estimatedSalary")
    sal = sal[0] if isinstance(sal, list) and sal else sal
    if isinstance(sal, dict) and str(sal.get("currency") or "USD").upper() == "USD":
        val = sal.get("value")
        if isinstance(val, dict):
            lo, hi = _annual(val.get("minValue", val.get("value")), val.get("maxValue", val.get("value")),
                             val.get("unitText"))
        else:
            lo, hi = _annual(val, val, sal.get("unitText"))
        if hi and 10_000 <= hi <= 5_000_000:
            out["pay_min"], out["pay_max"] = lo, hi
    out["date_posted"] = _date(jp.get("datePosted"))
    out["job_type"] = _job_type(jp.get("employmentType"))
    out["description"] = html_to_markdown(jp.get("description"))
    exp = jp.get("experienceRequirements")
    months = exp.get("monthsOfExperience") if isinstance(exp, dict) else None
    try:
        if isinstance(months, (int, float, str)) and not isinstance(months, bool) and months != "":
            years = float(months) // 12
            if 0 <= years <= 50:
                out["years_min"] = int(years)
    except (ValueError, OverflowError):
        pass
    return {k: v for k, v in out.items() if v not in (None, "")}


def _meta(soup, *names) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and (tag.get("content") or "").strip():
            return html_lib.unescape(tag["content"].strip())
    return None


def from_meta(soup) -> dict:
    """Title and company from the page's meta tags: 'Backend Engineer | depthfirst' -> both.
    (A company is only taken from the site name: guessing it from the tab title picks up
    things like "Life at Spotify" or a team name, so that's left to the AI or to you.)"""
    out = {}
    site = _meta(soup, "og:site_name", "application-name")
    title = _meta(soup, "og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else None)
    if title:
        parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+", title) if p.strip()]
        if len(parts) > 1 and site and parts[-1].lower() == site.lower():
            parts = parts[:-1]
        out["title"] = parts[0]
    if site:
        out["company"] = site
    return out


def readable(soup):
    """The page's main content element, with scripts, navigation and forms removed."""
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template"]):
        t.decompose()
    candidates = soup.select("main, article, [role=main]") or [soup.body or soup]
    return max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))


def page_text(el) -> str:
    text = el.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def from_text(text: str) -> dict:
    """Pay and years from free text, with the same rules a pull uses."""
    import daily_jobs
    out = {}
    pay = daily_jobs.salary_range({"description": text})
    if pay:
        out["pay_min"], out["pay_max"] = pay
    yrs = daily_jobs.years_required({"description": text})
    if yrs:
        out["years_min"], out["years_max"] = yrs
    return out


# ----------------------------------------------------------------------------
# Job boards the tracker already scrapes
# ----------------------------------------------------------------------------
def board_of(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for domain, site in (("linkedin.com", "linkedin"), ("indeed.com", "indeed"),
                         ("ziprecruiter.com", "zip_recruiter"), ("glassdoor.", "glassdoor")):
        if host == domain or host.endswith("." + domain) or (domain.endswith(".") and domain in host):
            return site
    return None


def indeed_job_key(url: str) -> str | None:
    q = parse_qs(urlsplit(url).query)
    for k in ("jk", "vjk"):
        if q.get(k) and re.fullmatch(r"[0-9a-f]{16}", q[k][0]):
            return q[k][0]
    m = re.search(r"-([0-9a-f]{16})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def _indeed_fields_block() -> str:
    """The job { ... } fields of JobSpy's Indeed search query (with its format braces undone)."""
    from jobspy.indeed import constant
    q = constant.job_search_query
    start = q.index("job {{") + len("job {{")
    depth, i = 1, start
    while depth:
        if q.startswith("{{", i):
            depth, i = depth + 1, i + 2
        elif q.startswith("}}", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return q[start:i - 2].replace("{{", "{").replace("}}", "}")


def from_indeed(url: str) -> dict:
    """One Indeed job from Indeed's job API, by its job key."""
    key = indeed_job_key(url)
    if not key:
        return {}
    from jobspy.indeed import Indeed, constant
    from jobspy.model import DescriptionFormat, ScraperInput
    from jobspy.util import RequestsRotating, create_session
    ind = Indeed()
    ind.scraper_input = ScraperInput(site_name=["indeed"], description_format=DescriptionFormat.MARKDOWN)
    ind.base_url = "https://www.indeed.com"
    ind.session = create_session(RequestsRotating(None), ind.scraper_input,
                                 constant.headers | {"indeed-co": "US", "indeed-locale": "en-US"})
    query = 'query GetJob { jobData(jobKeys: ["%s"]) { results { job { %s } } } }' % (key, _indeed_fields_block())
    resp = ind.session.post(Indeed.api_url, json={"query": query}, timeout=TIMEOUT)
    resp.raise_for_status()
    results = (((resp.json().get("data") or {}).get("jobData") or {}).get("results")) or []
    if not results or not results[0].get("job"):
        raise ImportProblem("Indeed didn't return that job. It may have been taken down.")
    post = ind._process_job(results[0]["job"])
    loc = post.location
    out = {"title": post.title, "company": post.company,
           "location": place(loc.city, loc.state, _plain(loc.country)) if loc else None,
           "is_remote": bool(post.is_remote) or None, "date_posted": _date(post.date_posted),
           "job_type": _job_type(post.job_type), "description": post.description,
           "job_url": f"https://www.indeed.com/viewjob?jk={key}"}
    if post.max_amount or post.min_amount:
        out["pay_min"], out["pay_max"] = _annual(post.min_amount, post.max_amount,
                                                 {"hourly": "HOUR", "daily": "DAY", "weekly": "WEEK",
                                                  "monthly": "MONTH"}.get(str(post.interval or ""), "YEAR"))
    return {k: v for k, v in out.items() if v not in (None, "")}


def from_linkedin_page(soup) -> dict:
    """A public LinkedIn job page (no sign-in needed): its top card, criteria and description."""
    pick = lambda *sels: next((el.get_text(" ", strip=True) for s in sels for el in [soup.select_one(s)] if el), None)
    out = {"title": pick("h1.top-card-layout__title", "h1"),
           "company": pick(".topcard__org-name-link", ".topcard__flavor"),
           "location": pick(".topcard__flavor--bullet")}
    desc = soup.select_one(".show-more-less-html__markup, .description__text")
    if desc:
        out["description"] = html_to_markdown(str(desc))
    for item in soup.select(".description__job-criteria-item"):
        head, val = item.select_one("h3"), item.select_one("span")
        if head and val and "employment type" in head.get_text().lower():
            out["job_type"] = _job_type(val.get_text(strip=True))
    sal = soup.select_one(".compensation__salary")
    if sal:
        out.update(from_text(sal.get_text(" ", strip=True) + " a year"))
    if out.get("location") and "remote" in out["location"].lower():
        out["is_remote"] = True
    return {k: v for k, v in out.items() if v not in (None, "")}


def board_description(site: str, url: str) -> str | None:
    """A description from JobSpy's own readers, for boards whose pages block plain requests."""
    import daily_jobs
    if site == "zip_recruiter":
        return (daily_jobs._zip_fetcher()({"id": None, "job_url": url}) or {}).get("description")
    if site == "glassdoor":
        m = re.search(r"[?&]jl=(\d+)", url) or re.search(r"_KE\d+,\d+\.htm.*?(\d{8,})", url)
        if not m:
            return None
        from jobspy.glassdoor import Glassdoor
        from jobspy.glassdoor.constant import headers
        from jobspy.model import DescriptionFormat, ScraperInput
        from jobspy.util import TLSRotating, create_session
        gd = Glassdoor()
        gd.scraper_input = ScraperInput(site_name=["glassdoor"], description_format=DescriptionFormat.MARKDOWN)
        gd.base_url = "https://www.glassdoor.com"
        gd.session = create_session(TLSRotating(None), gd.scraper_input, headers)
        gd.warn = lambda msg: None
        return (gd._fetch_details([f"gd-{m.group(1)}"]).get(f"gd-{m.group(1)}") or {}).get("description")
    return None


# ----------------------------------------------------------------------------
# AI
# ----------------------------------------------------------------------------
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_posting": {"type": "boolean"},
        "problem": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "company": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "location": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "is_remote": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "pay_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "pay_max": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "years_min": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "years_max": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "job_type": {"anyOf": [{"type": "string", "enum": list(JOB_TYPES)}, {"type": "null"}]},
        "date_posted": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "description_starts_with": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "description_ends_with": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["is_job_posting", "problem", "title", "company", "location", "is_remote", "pay_min", "pay_max",
                 "years_min", "years_max", "job_type", "date_posted", "description_starts_with",
                 "description_ends_with"],
    "additionalProperties": False,
}

AI_SYSTEM = """You read one web page and pull out the details of the job posting on it, for a job seeker's tracker.

Rules:
- Use only what the page says. Never guess; use null for anything the page doesn't state.
- is_job_posting: false if the page isn't a single job posting (a sign-in page, a list of many jobs, an error or "position filled" page); then say what it is in problem.
- title: the job title only, without the company or location. company: the employer's name.
- location: "City, ST" for US places (e.g. "San Francisco, CA"), "City, Country" elsewhere; "Remote" for a fully remote role with no city. is_remote: true only if the role can be done fully remote.
- pay_min/pay_max: the posted base pay range as yearly US dollars (hourly x 2080, monthly x 12). null if there's no pay range or it isn't in USD.
- years_min/years_max: the REQUIRED years of experience ("3+ years" -> 3 and null; "3-5 years" -> 3 and 5), not "nice to have".
- date_posted: YYYY-MM-DD if the page states when it was posted.
- You never write out the job description. Instead, mark where it is in the page text: description_starts_with is the first 8-15 words of the job description and description_ends_with is its last 8-15 words, each copied character for character from the page text (same words, same order, no changes). The description runs from its first section (usually about the company or the role) through responsibilities, requirements, benefits and pay; it leaves out navigation, cookie notices, application form fields, "similar jobs" and other listings. When unsure where it ends, include more rather than less: a pay or salary range, benefits, or an equal-opportunity or AI-in-hiring note right after the description belongs to it. Use null for both if you can't tell or if you're told the description was already found.
- If "Already found" values are given, keep them unless the page clearly shows they're wrong; fill in what's missing."""


_SKIP = set("*_`#>\\")
_SAME = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\xa0": " "}


def _normalized(text: str) -> tuple[str, list[int]]:
    """text lower-cased, Markdown marks dropped, quotes/dashes made plain and runs of
    whitespace made one space, with each character's position in the original."""
    out, where, space = [], [], False
    for i, ch in enumerate(text):
        ch = _SAME.get(ch, ch)
        if ch in _SKIP:
            continue
        if ch.isspace():
            if out and not space:
                out.append(" ")
                where.append(i)
            space = True
            continue
        space = False
        out.append(ch.lower())
        where.append(i)
    return "".join(out), where


def cut_description(text: str, starts_with: str, ends_with: str) -> str | None:
    """The part of text from the phrase starts_with through the phrase ends_with, exactly
    as it is in text (the AI only names where the job description begins and ends, so no
    words are ever changed). None if either phrase isn't found, or the part is too short to
    be a description."""
    norm, where = _normalized(text)
    a, b = _normalized(starts_with)[0].strip(), _normalized(ends_with)[0].strip()
    if not a or not b:
        return None
    i = norm.find(a)
    if i < 0:
        return None
    j = norm.find(b, i)
    if j < 0:
        return None
    start, end = where[i], where[j + len(b) - 1] + 1
    line_start = text.rfind("\n", 0, start) + 1               # keep a heading's "## " or a bullet's "- "
    if not text[line_start:start].strip(" #*_->+0123456789."):
        start = line_start
    while end < len(text) and text[end] in "*_`)]":            # and closing ** or )
        end += 1
    cut = text[start:end].strip()
    return cut if len(cut) >= 200 else None


def _ai_available() -> tuple[bool, str | None]:
    import ai_screen
    conn = db.connect()
    try:
        prefs = db.get_prefs(conn)
    finally:
        conn.close()
    if not ai_screen.key_status()["configured"]:
        return False, "No ANTHROPIC_API_KEY in job_search/.env, so the AI didn't read the page."
    if not prefs["ai"].get("enabled", True):
        return False, "AI is turned off in Preferences, so the AI didn't read the page."
    return True, None


def ai_extract(url: str, page_title: str | None, text: str, found: dict, client=None, model=None,
               have_description: bool = False) -> tuple[dict, dict]:
    """Ask Claude for the posting's details. Returns (fields, usage) where usage has the
    model, tokens and cost."""
    import ai_screen
    if model is None:
        conn = db.connect()
        try:
            model = db.get_prefs(conn)["ai"].get("model") or "claude-haiku-5-5"
        finally:
            conn.close()
    cli = client or ai_screen.client()
    known = {k: v for k, v in found.items() if k in FIELDS and k not in ("description", "job_url", "site")}
    content = (f"Page: {url}\nPage title: {page_title or '(none)'}\n"
               + (f"Already found: {json.dumps(known, default=str)}\n" if known else "")
               + ("The description was already found: return null for description_starts_with and "
                  "description_ends_with.\n" if have_description else "")
               + f"\nPage text:\n{text[:AI_TEXT_CHARS]}")
    resp = cli.messages.create(
        model=model, max_tokens=4000, system=AI_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    usage = {"model": model, "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    usage["cost"] = round(ai_screen.cost_of(model, usage["input_tokens"], usage["output_tokens"]), 5)
    if resp.stop_reason == "refusal":
        raise ImportProblem("The AI declined to read this page.")
    if resp.stop_reason == "max_tokens":
        raise ImportProblem("The page was too long for the AI to finish reading.")
    data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
    if not data.get("is_job_posting", True):
        raise ImportProblem(f"The AI says this isn't a job posting: {data.get('problem') or 'no details'}.")
    # Every field but the description (the AI only says where that is; see cut_description).
    out = {k: data.get(k) for k in FIELDS if k != "description" and data.get(k) not in (None, "")}
    start, end = data.get("description_starts_with"), data.get("description_ends_with")
    if isinstance(start, str) and isinstance(end, str) and start.strip() and end.strip():
        out["_description_bounds"] = (start, end)
    if "date_posted" in out:
        out["date_posted"] = _date(out["date_posted"])
    for k in ("pay_min", "pay_max", "years_min", "years_max"):
        if k in out and (not isinstance(out[k], (int, float)) or out[k] < 0):
            out.pop(k)
    return {k: v for k, v in out.items() if v not in (None, "")}, usage


def _log_ai_use(usage: dict, url: str):
    """Record the AI spend as a finished "import" run, so it shows in the pull history and
    Insights. (Not start_run: that would mark a pull running right now as interrupted.)"""
    conn = db.connect()
    try:
        ts = db.now_iso()
        conn.execute("INSERT INTO scrape_runs(started_at, finished_at, kind, trigger, status, ai_screened, "
                     "ai_input_tokens, ai_output_tokens, ai_cost, log) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (ts, ts, "import", "ui", "ok", 0, usage["input_tokens"], usage["output_tokens"],
                      usage["cost"], f"Filled in a role from {url}"))
        conn.commit()
    finally:
        conn.close()


def from_heading(soup) -> dict:
    """The page's main heading as the title, when it looks like one (not "Careers")."""
    h1 = soup.find("h1")
    t = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)) if h1 else ""
    if 3 <= len(t) <= 150 and not re.fullmatch(r"(?i)(careers?|jobs?|open (roles|positions)|join us)\W*", t):
        return {"title": t}
    return {}


def _parse(page: str, site: str | None) -> dict:
    """Everything one page offers, strongest first: [(values, source)...], plus its text."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    values = []
    if site == "linkedin":
        values.append((from_linkedin_page(soup), "board"))
    jp = job_posting_data(soup)
    if jp:
        try:
            values.append((from_job_posting(jp), "structured"))
        except (TypeError, ValueError, AttributeError, OverflowError):
            pass   # unusual JobPosting data: fall back to the page's meta tags and text
    values.append((from_heading(soup), "page"))
    values.append((from_meta(soup), "page"))
    page_title = soup.title.get_text(strip=True) if soup.title else None
    has_password = soup.find("input", attrs={"type": "password"}) is not None
    main = readable(soup)
    text = page_text(main)
    return {"values": values, "page_title": page_title, "text": text,
            "description": html_to_markdown(str(main)) if len(text) > 200 else None,
            "thin": not jp and len(text) < 1500,
            "sign_in": not jp and _looks_like_sign_in(has_password, page_title, text)}


# ----------------------------------------------------------------------------
# A headless browser, for pages built by JavaScript
# ----------------------------------------------------------------------------
RENDER_TIMEOUT = 45          # seconds for the whole browser run
RENDER_WAIT_MS = 10_000      # how long the page's scripts get to build it


def find_browser() -> str | None:
    """Chrome, Chromium or Edge, whichever is installed (JOBS_BROWSER can name one)."""
    import os
    import shutil
    if os.environ.get("JOBS_BROWSER"):
        return os.environ["JOBS_BROWSER"]
    candidates = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                           os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


class _CheckingProxy:
    """A tiny HTTP proxy on 127.0.0.1 for the headless browser. Every connection the page
    makes (the page itself, redirects, scripts, images, fetches, WebSockets) goes through it,
    and it connects only to public addresses, looking each name up itself and connecting to
    the address it checked. Without it a job page could redirect the browser to a private
    address written as numbers (http://127.0.0.1:...), which no name rule can stop."""

    def __init__(self):
        import socketserver
        import threading
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _connect(host: str, port: int):
        """A socket to host:port if every address it has is public, else None."""
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        except (OSError, UnicodeError, ValueError):
            return None
        if not ips or not all(_proxy_allows(ip) for ip in ips):
            return None
        try:
            return socket.create_connection((str(ips[0]), port), timeout=TIMEOUT)
        except OSError:
            return None

    def _handle(self, client):
        client.settimeout(TIMEOUT)
        try:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port)
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise ValueError(target)
                host, port = parts.hostname, parts.port or 80
            upstream = self._connect(host, port)
            if upstream is None:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            with upstream:
                if method == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    if rest:
                        upstream.sendall(rest)
                else:
                    # One request per connection, so a reused browser connection can't
                    # carry a request for another (unchecked) host to this upstream.
                    path = parts.path or "/"
                    if parts.query:
                        path += "?" + parts.query
                    kept = [h for h in header_lines
                            if h.split(":", 1)[0].strip().lower() not in ("connection", "proxy-connection", "keep-alive")]
                    upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1")
                                     + "".join(h + "\r\n" for h in kept + ["Connection: close"]).encode("latin-1")
                                     + b"\r\n" + rest)
                self._pipe(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _pipe(a, b):
        import threading

        def copy(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=copy, args=(b, a), daemon=True)
        t.start()
        copy(a, b)
        t.join(RENDER_TIMEOUT)


def _proxy_allows(ip) -> bool:
    return _ip_is_public(ip)


def render_page(url: str) -> str | None:
    """The page as a browser shows it after its scripts run (headless, with a fresh
    throwaway profile: none of your sign-ins or cookies). None if no browser is installed
    or it fails. The link is checked to be public first, like a plain fetch, and everything
    the browser loads goes through _CheckingProxy, which only connects to public addresses."""
    import subprocess
    import tempfile
    exe = find_browser()
    if not exe:
        return None
    _check_public(url)
    with _CheckingProxy() as proxy, \
            tempfile.TemporaryDirectory(prefix="jobtracker-browser-", ignore_cleanup_errors=True) as profile:
        try:
            out = subprocess.run(
                [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--disable-sync", "--mute-audio", "--blink-settings=imagesEnabled=false",
                 f"--user-data-dir={profile}",
                 # All traffic through the proxy, loopback included (Chrome skips the proxy for
                 # localhost unless told not to); QUIC can't go through an HTTP proxy.
                 f"--proxy-server=http://127.0.0.1:{proxy.port}", "--proxy-bypass-list=<-loopback>",
                 "--disable-quic", f"--virtual-time-budget={RENDER_WAIT_MS}", "--dump-dom", url],
                capture_output=True, timeout=RENDER_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
    html = out.stdout.decode("utf-8", errors="replace")
    return html if len(html) > 500 else None


# ----------------------------------------------------------------------------
# Putting it together
# ----------------------------------------------------------------------------
# How much each source is trusted: a later, weaker source only fills blanks; the AI may
# also replace values that came from meta tags or loose text ("page").
STRONG = ("board", "structured")


def import_from_url(url: str, use_ai: bool = False, fetch=None, ai=None, render=None) -> dict:
    """Propose Add role values for a job link. Returns
    {"fields": {...}, "sources": {field: "board"|"structured"|"page"|"ai"}, "messages": [...],
     "board": site or None, "ai": {"used", "cost", ...} or None}.
    Raises ImportProblem when nothing useful could be read."""
    from bs4 import BeautifulSoup
    url = normalize_url(url)
    fetch = fetch or fetch_page
    site = board_of(url)
    fields: dict = {}
    sources: dict = {}
    messages: list[str] = []

    def take(values: dict, source: str, override=()):
        for k, v in values.items():
            if k in FIELDS and v not in (None, "") and (k not in fields or k in override):
                fields[k], sources[k] = v, source

    if site == "linkedin":
        import daily_jobs
        job_id = daily_jobs.linkedin_job_id(url)
        if job_id:
            url = f"https://www.linkedin.com/jobs/view/{job_id}"
    elif site == "indeed":
        try:
            take(from_indeed(url), "board")
        except ImportProblem:
            raise
        except Exception as e:
            messages.append(f"Indeed's job API didn't answer ({type(e).__name__}); reading the page instead.")

    page_title, text, fetch_error = None, "", None
    if not (site == "indeed" and fields.get("description")):
        # A plain request first; if the page comes back (nearly) empty because it's built by
        # JavaScript, or the site refuses plain requests, let a headless browser render it.
        page, final_url, problem = None, url, None
        try:
            final_url, page = fetch(url)
        except ImportProblem as e:
            problem, fetch_error = e, str(e)
        parsed = _parse(page, site) if page else None
        may_render = site != "linkedin" and (parsed is not None or (problem is not None and problem.retry))
        if (parsed is None or parsed["thin"]) and may_render:
            rendered = (render or render_page)(final_url)   # after redirects, each one checked
            if rendered:
                again = _parse(rendered, site)
                if parsed is None or len(again["text"]) > len(parsed["text"]):
                    parsed, fetch_error = again, None
                    messages.append("Read the page with a headless browser (it builds its content with JavaScript "
                                    "or refuses plain requests).")
        if parsed and parsed["sign_in"]:
            # A sign-in wall: its heading and text are about signing in, not the job.
            fetch_error = "This page asks you to sign in, so it can't be read. Fill in the fields yourself."
        elif parsed:
            fetch_error = None
            page_title, text = parsed["page_title"], parsed["text"]
            for values, source in parsed["values"]:
                take(values, source)
            if not fields.get("description") and parsed["description"]:
                take({"description": parsed["description"]}, "page")
            take(from_text((fields.get("description") or "") + "\n" + text), "page")
            if final_url != url and not fields.get("job_url"):
                fields["job_url"] = final_url

    if site in ("zip_recruiter", "glassdoor") and not fields.get("description"):
        try:
            desc = board_description(site, url)
            if desc:
                take({"description": desc}, "board")
                take(from_text(desc), "page")
        except Exception as e:
            messages.append(f"Couldn't fetch the description from {site} ({type(e).__name__}).")

    ai_info = None
    if use_ai:
        ok, why = _ai_available()
        if not ok:
            messages.append(why)
        elif not text and not fields.get("description"):
            messages.append("There was no page text for the AI to read.")
        else:
            try:
                found = dict(fields)
                values, usage = (ai or ai_extract)(url, page_title, text or fields.get("description", ""), found,
                                                   have_description=sources.get("description") in STRONG)
                values = dict(values)
                values.pop("description", None)        # never AI-written text (see cut_description)
                bounds = values.pop("_description_bounds", None)
                weak = [k for k in values if sources.get(k) not in STRONG]
                take(values, "ai", override=weak)
                if bounds and sources.get("description") == "page":
                    cut = cut_description(fields["description"], *bounds)
                    if cut:
                        fields["description"] = cut
                        messages.append("The AI marked where the job description starts and ends on the page; "
                                        "the text in between is copied exactly as it is.")
                ai_info = {"used": True, **usage}
                _log_ai_use(usage, url)
            except ImportProblem as e:
                messages.append(str(e))
                ai_info = {"used": False, "error": str(e)}
            except Exception as e:
                messages.append(f"The AI couldn't read the page ({type(e).__name__}: {e}).")
                ai_info = {"used": False, "error": str(e)}

    if fetch_error and not (fields.get("title") or fields.get("description")):
        raise ImportProblem(fetch_error)
    if fetch_error:
        messages.append(fetch_error)
    if not any(fields.get(k) for k in ("title", "company", "description")):
        raise ImportProblem("Couldn't find a job posting on that page. It may load its content with "
                            "JavaScript or need a sign-in; fill in the fields yourself"
                            + ("." if use_ai else ", or try again with AI on."))
    fields.setdefault("job_url", url)
    fields["site"] = site or "manual"
    if fields.get("location") and "remote" in str(fields["location"]).lower() and "is_remote" not in fields:
        fields["is_remote"] = True
    for k in ("pay_min", "pay_max"):
        if k in fields:
            fields[k] = round(float(fields[k]))
    return {"fields": {k: fields[k] for k in FIELDS if k in fields}, "sources": sources,
            "messages": messages, "board": site, "ai": ai_info}


def _looks_like_sign_in(has_password: bool, title, text) -> bool:
    """A sign-in wall: a password box, or a short page that's mostly about signing in."""
    t = f"{title or ''} {text[:400]}".lower()
    return (has_password and len(text) < 3000) or bool(re.search(r"\b(sign in|log in|login)\b", t) and len(text) < 1500)
