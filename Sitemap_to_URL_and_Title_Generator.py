"""
Sitemap SEO Audit - Streamlit app
Input : XML sitemap URL (or a domain / homepage URL)
Output: every page URL with Status, Title, Meta Description, H1  ->  CSV / Excel

Run locally :  streamlit run app.py
"""

import gzip
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from defusedxml import ElementTree as SafeET
from requests.adapters import HTTPAdapter

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SitemapAuditBot/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_HTML_BYTES = 2_000_000        # read at most ~2 MB per page
MAX_SITEMAP_BYTES = 80_000_000    # sitemap files are capped by spec at 50 MB
MAX_SITEMAP_FILES = 1000          # safety cap for sitemap indexes
COLUMNS = ["URL", "Status", "Title", "Meta Description", "H1"]


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
def make_session(pool_size: int = 10) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def fetch(session, url, timeout=15, max_bytes=MAX_HTML_BYTES, retries=2):
    """Returns (status_code, content_bytes, content_type, error). status 0 = network error."""
    last_err = ""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
            try:
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        break
                return r.status_code, b"".join(chunks), r.headers.get("Content-Type", ""), ""
            finally:
                r.close()
        except requests.RequestException as e:
            last_err = e.__class__.__name__
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    return 0, b"", "", last_err or "Failed"


# --------------------------------------------------------------------------
# Sitemap discovery + parsing
# --------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def parse_sitemap(content: bytes):
    """Returns (kind, locs). kind is 'index', 'urlset' or 'text'."""
    if content[:2] == b"\x1f\x8b":  # .xml.gz served as a raw file
        content = gzip.decompress(content)
    try:
        root = SafeET.fromstring(content)
    except Exception:
        lines = [l.strip() for l in content.decode("utf-8", "replace").splitlines()]
        urls = [l for l in lines if l.startswith(("http://", "https://"))]
        return "text", urls
    kind = "index" if _local(root.tag) == "sitemapindex" else "urlset"
    locs = []
    for entry in root:  # <url> or <sitemap>
        for child in entry:  # direct children only, so <image:loc> is ignored
            if _local(child.tag) == "loc" and child.text and child.text.strip():
                locs.append(child.text.strip())
    return kind, locs


def discover_sitemaps(session, raw: str, timeout=15):
    """Accepts a sitemap URL or a domain/homepage URL. Returns (sitemap_urls, note)."""
    raw = raw.strip()
    if not raw:
        return [], "Please enter a sitemap URL or domain."
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    path = parsed.path.lower()
    if path.endswith((".xml", ".xml.gz", ".txt")) or "sitemap" in path:
        return [raw], ""

    origin = f"{parsed.scheme}://{parsed.netloc}"
    found = []
    status, body, _, _ = fetch(session, origin + "/robots.txt", timeout, max_bytes=500_000)
    if status == 200:
        for line in body.decode("utf-8", "replace").splitlines():
            m = re.match(r"\s*sitemap:\s*(\S+)", line, re.I)
            if m:
                found.append(m.group(1))
    if found:
        return list(dict.fromkeys(found)), "Sitemap(s) found in robots.txt"

    for cand in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml", "/sitemap.xml.gz"):
        status, body, _, _ = fetch(session, origin + cand, timeout, max_bytes=2_000_000, retries=0)
        if status == 200 and (body.lstrip()[:1] == b"<" or body[:2] == b"\x1f\x8b"):
            return [origin + cand], f"Sitemap found at {cand}"
    return [], "No sitemap found for this domain. Paste the sitemap URL directly."


def collect_urls(session, sitemap_urls, timeout=15, max_urls=100_000, progress_cb=None):
    """Walks sitemap indexes recursively. Returns (urls, notes)."""
    queue = list(sitemap_urls)
    seen_maps, seen_urls, urls, notes = set(), set(), [], []
    while queue and len(seen_maps) < MAX_SITEMAP_FILES and len(urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        status, body, _, err = fetch(session, sm, timeout, max_bytes=MAX_SITEMAP_BYTES)
        if status != 200:
            notes.append(f"Could not read {sm} ({status or err})")
            continue
        try:
            kind, locs = parse_sitemap(body)
        except Exception as e:
            notes.append(f"Could not parse {sm} ({e.__class__.__name__})")
            continue
        if kind == "index":
            queue.extend(locs)
        else:
            for u in locs:
                if u not in seen_urls:
                    seen_urls.add(u)
                    urls.append(u)
                    if len(urls) >= max_urls:
                        notes.append(f"Stopped at {max_urls} URLs (limit).")
                        break
        if progress_cb:
            progress_cb(len(seen_maps), len(urls))
    return urls, notes


# --------------------------------------------------------------------------
# Page extraction
# --------------------------------------------------------------------------
def _clean(t) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def extract(content: bytes):
    soup = BeautifulSoup(content, "lxml")  # bytes in -> encoding auto-detected
    title = _clean(soup.title.get_text()) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    desc = _clean(md.get("content")) if md and md.get("content") else ""
    h1 = soup.find("h1")
    h1_text = _clean(h1.get_text(" ")) if h1 else ""
    return title, desc, h1_text


def audit_page(session, url, timeout, delay=0.0):
    if delay:
        time.sleep(delay)
    status, body, ctype, err = fetch(session, url, timeout)
    row = {"URL": url, "Status": "", "Title": "", "Meta Description": "", "H1": ""}
    if status == 0:
        row["Status"] = f"Error: {err}"
    elif status != 200:
        row["Status"] = status
    elif ctype and "html" not in ctype.lower():
        row["Status"] = "200 (non-HTML)"
    else:
        row["Status"] = 200
        row["Title"], row["Meta Description"], row["H1"] = extract(body)
    return row


def crawl(urls, workers, timeout, delay, results, on_progress=None):
    """Fills `results` ({index: row}) as pages finish, so partial data survives a stop."""
    session = make_session(workers)
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(audit_page, session, u, timeout, delay): i for i, u in enumerate(urls)}
        for n, f in enumerate(as_completed(futs), 1):
            results[futs[f]] = f.result()
            if on_progress and (n % 5 == 0 or n == len(urls)):
                on_progress(n, len(urls))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# Export helpers
# --------------------------------------------------------------------------
def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="SEO Audit")
        ws = xw.sheets["SEO Audit"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, w in zip("ABCDE", (60, 12, 50, 70, 50)):
            ws.column_dimensions[col].width = w
    return buf.getvalue()


def issues_mask(df: pd.DataFrame):
    return (df["Status"].astype(str) != "200") | (df["Title"] == "") | (df["Meta Description"] == "") | (df["H1"] == "")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Sitemap SEO Audit", page_icon="🔎", layout="wide")
    st.title("🔎 Sitemap SEO Audit")
    st.caption("Enter a sitemap URL or domain → get every page's URL, Title, Meta Description and H1 → download CSV / Excel.")

    with st.sidebar:
        st.header("Settings")
        max_urls = st.number_input("Max URLs to analyze", 10, 100_000, 10_000, step=500)
        workers = st.slider("Parallel requests", 1, 20, 10, help="Higher = faster, but may get blocked by some sites.")
        timeout = st.slider("Timeout per page (sec)", 5, 60, 15)
        delay = st.number_input("Delay per request (sec)", 0.0, 5.0, 0.0, step=0.1, help="Add a delay for sensitive servers.")

    target = st.text_input("Sitemap URL or domain", placeholder="https://example.com/sitemap.xml  or  example.com")

    # ---- Step 1: fetch URLs from sitemap
    if st.button("1. Fetch URLs from sitemap", type="primary"):
        st.session_state.pop("results", None)
        session = make_session(5)
        with st.spinner("Looking for sitemap..."):
            sitemaps, note = discover_sitemaps(session, target, timeout)
        if not sitemaps:
            st.session_state["urls"] = []
            st.error(note)
        else:
            if note:
                st.info(note)
            holder = st.empty()
            urls, notes = collect_urls(
                session, sitemaps, timeout, max_urls=100_000,
                progress_cb=lambda m, u: holder.write(f"Read {m} sitemap file(s) · {u:,} URLs so far"),
            )
            holder.empty()
            st.session_state["urls"] = urls
            for n in notes:
                st.warning(n)
            if not urls:
                st.error("Sitemap was found but no URLs could be read.")

    urls = st.session_state.get("urls", [])
    if urls:
        c1, c2 = st.columns(2)
        inc = c1.text_input("Only include URLs containing (optional)", placeholder="/blog/")
        exc = c2.text_input("Exclude URLs containing (optional)", placeholder="/tag/, /author/")
        sel = urls
        if inc.strip():
            sel = [u for u in sel if inc.strip().lower() in u.lower()]
        if exc.strip():
            bad = [x.strip().lower() for x in exc.split(",") if x.strip()]
            sel = [u for u in sel if not any(b in u.lower() for b in bad)]
        sel = sel[: int(max_urls)]
        st.success(f"{len(urls):,} URLs found in sitemap · **{len(sel):,} selected** for analysis")

        with st.expander("Preview URL list"):
            st.dataframe(pd.DataFrame({"URL": sel[:500]}), width="stretch", height=250)

        # ---- Step 2: analyze pages
        if st.button("2. Analyze pages", disabled=not sel):
            results = {}
            st.session_state["results"] = results
            bar = st.progress(0.0, text="Starting...")
            started = time.time()

            def on_progress(n, total):
                elapsed = time.time() - started
                eta = (elapsed / n) * (total - n)
                bar.progress(n / total, text=f"{n:,} / {total:,} pages · ~{int(eta // 60)}m {int(eta % 60)}s left")

            crawl(sel, workers, timeout, delay, results, on_progress)
            bar.empty()
            st.toast("Analysis complete", icon="✅")

    # ---- Results (kept in session so a stopped run still shows partial data)
    results = st.session_state.get("results")
    if results:
        df = pd.DataFrame([results[i] for i in sorted(results)], columns=COLUMNS)
        df["Status"] = df["Status"].astype(str)
        bad = issues_mask(df)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Pages analyzed", f"{len(df):,}")
        m2.metric("Status 200", f"{(df['Status'] == '200').sum():,}")
        m3.metric("Missing title", f"{((df['Status'] == '200') & (df['Title'] == '')).sum():,}")
        m4.metric("Missing meta desc", f"{((df['Status'] == '200') & (df['Meta Description'] == '')).sum():,}")
        m5.metric("Missing H1", f"{((df['Status'] == '200') & (df['H1'] == '')).sum():,}")

        only_issues = st.checkbox(f"Show only pages with issues ({int(bad.sum()):,})")
        view = df[bad] if only_issues else df
        st.dataframe(
            view, width="stretch", height=520, hide_index=True,
            column_config={"URL": st.column_config.LinkColumn("URL")},
        )

        d1, d2, _ = st.columns([1, 1, 4])
        d1.download_button("⬇ Download CSV", to_csv_bytes(view), "seo-audit.csv", "text/csv")
        d2.download_button(
            "⬇ Download Excel", to_excel_bytes(view), "seo-audit.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()
