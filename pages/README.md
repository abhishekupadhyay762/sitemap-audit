# SEO Tools (Streamlit)

Ek app, 2 tools (left sidebar me page switch hota hai):
1. **app** - Sitemap SEO Audit: sitemap/domain -> har page ka URL, Status, Title, Meta, H1
2. **Bulk URL Checker** - URLs paste/upload -> Title, Meta, H1, H2, Canonical, Robots, Word count, Dates, Alt, Schema, Issues

## Folder structure (zaroori)
    sitemap-audit/
      app.py
      requirements.txt
      pages/
        1_Bulk_URL_Checker.py

## Local run
    pip install -r requirements.txt
    python -m streamlit run app.py

## Live (Streamlit Community Cloud)
GitHub repo me same structure upload karein (pages folder ke saath), phir share.streamlit.io par Deploy.
