"""
Bulk URL SEO Checker - Streamlit page
Input : list of URLs (paste or upload CSV/TXT/XLSX)
Output: Status, Title, Meta Description, H1, H2, Canonical, Robots, Word count, Dates,
        Images without alt, Schema, Issues  ->  CSV / Excel
"""

import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SEOMetaChecker/2.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_HTML_BYTES = 2_000_000

COLS = [
    "URL", "Status", "Final URL", "Title", "Title Len", "Meta Description", "Meta Len",
    "H1", "H1 Count", "H2 Tags", "H2 Count", "Canonical", "Robots", "Word Count",
    "Published", "Updated", "Imgs No Alt", "Schema", "Issues",
]


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
def looks_like_url(s: str) -> bool:
    s = s.strip()
    return bool(s) and " " not in s and "." in s


def normalize_url(raw: str) -> str:
    u = raw.strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    return u if p.netloc else ""


def parse_uploaded(file) -> list:
    """First column of a CSV / XLSX, or one URL per line for TXT."""
    name = file.name.lower()
    raw = file.getvalue()
    if name.endswith(".txt"):
        vals = raw.decode("utf-8-sig", "replace").splitlines()
    elif name.endswith((".xlsx", ".xls")):
        vals = pd.read_excel(io.BytesIO(raw), header=None).iloc[:, 0].dropna().astype(str).tolist()
    else:
        vals = pd.read_csv(io.BytesIO(raw), header=None, encoding="utf-8-sig", usecols=[0]).iloc[:, 0].dropna().astype(str).tolist()
    return [v.strip() for v in vals if looks_like_url(v)]


def collect_inputs(text: str, file) -> list:
    items = [l.strip() for l in text.splitlines() if l.strip()]
    if file is not None:
        items += parse_uploaded(file)
    seen, out = set(), []
    for it in items:
        u = normalize_url(it)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount("http://", ad)
    s.mount("https://", ad)
    return s


def fetch_page(session, url, timeout, retries=2):
    """Returns (status, body_bytes, content_type, final_url, error)."""
    err = ""
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
                    if size >= MAX_HTML_BYTES:
                        break
                return r.status_code, b"".join(chunks), r.headers.get("Content-Type", ""), r.url, ""
            finally:
                r.close()
        except requests.RequestException as e:
            err = e.__class__.__name__
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    return 0, b"", "", url, err or "Failed"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def clean(t) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def get_meta(soup, key: str) -> str:
    for m in soup.find_all("meta"):
        for attr in ("name", "property"):
            k = m.get(attr)
            if k and k.strip().lower() == key and m.get("content"):
                return clean(m.get("content"))
    return ""


def get_canonical(soup) -> str:
    for l in soup.find_all("link"):
        rel = l.get("rel") or []
        rel = [rel] if isinstance(rel, str) else rel
        if "canonical" in [x.lower() for x in rel] and l.get("href"):
            return l.get("href").strip()
    return ""


def jsonld_date(soup, key: str) -> str:
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.string or s.get_text() or ""
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, txt)
        if m:
            return m.group(1)
    return ""


def fmt_date(s: str) -> str:
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return s.strip()[:10]


def find_issues(r: dict) -> str:
    st_ = str(r["Status"])
    if st_ != "200":
        return "Page not OK (" + st_ + ")"
    out = []
    tl, ml = r["Title Len"], r["Meta Len"]
    if not r["Title"]:
        out.append("Title missing")
    elif tl > 60:
        out.append("Title too long")
    elif tl < 30:
        out.append("Title short")
    if not r["Meta Description"]:
        out.append("Meta description missing")
    elif ml > 160:
        out.append("Meta too long")
    elif ml < 70:
        out.append("Meta short")
    if r["H1 Count"] == 0:
        out.append("H1 missing")
    elif r["H1 Count"] > 1:
        out.append("Multiple H1")
    if not r["Canonical"]:
        out.append("Canonical missing")
    if "noindex" in r["Robots"].lower():
        out.append("Noindex")
    if r["Imgs No Alt"]:
        out.append(f"{r['Imgs No Alt']} img without alt")
    return "; ".join(out)


def extract(body: bytes, row: dict):
    soup = BeautifulSoup(body, "lxml")
    title = clean(soup.title.get_text()) if soup.title else ""
    desc = get_meta(soup, "description")
    h1s = soup.find_all("h1")
    h2s = [clean(h.get_text(" ")) for h in soup.find_all("h2")]
    h2s = [h for h in h2s if h]
    pub = get_meta(soup, "article:published_time") or jsonld_date(soup, "datePublished")
    mod = get_meta(soup, "article:modified_time") or jsonld_date(soup, "dateModified")
    imgs_no_alt = sum(1 for i in soup.find_all("img") if not (i.get("alt") or "").strip())
    schema = "Yes" if soup.find("script", type="application/ld+json") else "No"

    words = 0
    if soup.body:
        for t in soup.body(["script", "style", "noscript"]):
            t.decompose()
        words = len(soup.body.get_text(" ").split())

    row.update({
        "Title": title, "Title Len": len(title),
        "Meta Description": desc, "Meta Len": len(desc),
        "H1": clean(h1s[0].get_text(" ")) if h1s else "", "H1 Count": len(h1s),
        "H2 Tags": " || ".join(h2s), "H2 Count": len(h2s),
        "Canonical": get_canonical(soup), "Robots": get_meta(soup, "robots"),
        "Word Count": words, "Published": fmt_date(pub), "Updated": fmt_date(mod),
        "Imgs No Alt": imgs_no_alt, "Schema": schema,
    })


def audit_url(session, url, timeout, delay):
    if delay:
        time.sleep(delay)
    status, body, ctype, final, err = fetch_page(session, url, timeout)
    row = {c: "" for c in COLS}
    row["URL"] = url
    row["Final URL"] = final if final != url else ""
    if status == 0:
        row["Status"] = f"Error: {err}"
    elif status != 200:
        row["Status"] = str(status)
    elif ctype and "html" not in ctype.lower():
        row["Status"] = "200 (non-HTML)"
    else:
        row["Status"] = "200"
        extract(body, row)
    row["Issues"] = find_issues(row) if row["Status"] == "200" else f"Page not OK ({row['Status']})"
    return row


def run_audit(urls, workers, timeout, delay, results, on_progress):
    session = make_session(workers)
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(audit_url, session, u, timeout, delay): i for i, u in enumerate(urls)}
        for n, f in enumerate(as_completed(futs), 1):
            results[futs[f]] = f.result()
            if n % 3 == 0 or n == len(urls):
                on_progress(n, len(urls))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def to_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Bulk SEO Check")
        ws = xw.sheets["Bulk SEO Check"]
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        widths = {"URL": 50, "Final URL": 40, "Title": 45, "Meta Description": 60, "H1": 40,
                  "H2 Tags": 60, "Canonical": 40, "Issues": 45}
        for i, c in enumerate(df.columns, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(c, 13)
    return buf.getvalue()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Bulk URL SEO Checker", page_icon="📋", layout="wide")
    st.title("📋 Bulk URL SEO Checker")
    st.caption("Paste URLs or upload a list → Title, Meta, H1/H2, Canonical, Robots, Word count, Dates, Alt, Schema → CSV / Excel.")

    with st.sidebar:
        st.header("Settings")
        max_urls = st.number_input("Max URLs to analyze", 10, 20_000, 1_000, step=100)
        workers = st.slider("Parallel requests", 1, 20, 10, help="Higher = faster, but may get blocked by some sites.")
        timeout = st.slider("Timeout per page (sec)", 5, 60, 15)
        delay = st.number_input("Delay per request (sec)", 0.0, 5.0, 0.0, step=0.1)

    c1, c2 = st.columns([3, 2])
    text = c1.text_area("URLs (one per line)", height=180, placeholder="https://example.com/page-1\nhttps://example.com/page-2")
    file = c2.file_uploader("...or upload CSV / TXT / XLSX (URLs in first column)", type=["csv", "txt", "xlsx", "xls"])

    if st.button("Analyze URLs", type="primary"):
        try:
            urls = collect_inputs(text, file)
        except Exception as e:
            st.error(f"Could not read the uploaded file: {e}")
            urls = []
        if not urls:
            st.warning("Please paste or upload at least one valid URL.")
        else:
            if len(urls) > max_urls:
                st.info(f"{len(urls):,} URLs given. Only the first {int(max_urls):,} will be analyzed (change in sidebar).")
                urls = urls[: int(max_urls)]
            results = {}
            st.session_state["bulk_results"] = results
            bar = st.progress(0.0, text="Starting...")
            started = time.time()

            def on_progress(n, total):
                eta = (time.time() - started) / n * (total - n)
                bar.progress(n / total, text=f"{n:,} / {total:,} URLs · ~{int(eta // 60)}m {int(eta % 60)}s left")

            run_audit(urls, workers, timeout, delay, results, on_progress)
            bar.empty()
            st.toast("Analysis complete", icon="✅")

    results = st.session_state.get("bulk_results")
    if results:
        df = pd.DataFrame([results[i] for i in sorted(results)], columns=COLS)
        ok = df["Status"] == "200"
        m = st.columns(6)
        m[0].metric("URLs analyzed", f"{len(df):,}")
        m[1].metric("Status 200", f"{ok.sum():,}")
        m[2].metric("Missing title", f"{(ok & (df['Title'] == '')).sum():,}")
        m[3].metric("Missing meta", f"{(ok & (df['Meta Description'] == '')).sum():,}")
        m[4].metric("Missing H1", f"{(ok & (df['H1 Count'] == 0)).sum():,}")
        m[5].metric("Multiple H1", f"{(ok & (df['H1 Count'] > 1)).sum():,}")

        has_issue = df["Issues"] != ""
        only = st.checkbox(f"Show only URLs with issues ({int(has_issue.sum()):,})")
        view = df[has_issue] if only else df
        st.dataframe(view, width="stretch", height=520, hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL")})

        d1, d2, _ = st.columns([1, 1, 4])
        d1.download_button("⬇ Download CSV", to_csv_bytes(view), "bulk-seo-check.csv", "text/csv")
        d2.download_button("⬇ Download Excel", to_excel_bytes(view), "bulk-seo-check.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    main()
