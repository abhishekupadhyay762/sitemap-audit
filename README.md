# Sitemap SEO Audit

Sitemap URL ya domain do -> har page ka URL, Status, Title, Meta Description, H1 -> CSV / Excel.

## Local run
    python -m venv .venv
    # Windows: .venv\Scripts\activate   |  Mac/Linux: source .venv/bin/activate
    pip install -r requirements.txt
    streamlit run app.py

Browser me http://localhost:8501 khulega.

## Team ko live URL (free)
1. app.py + requirements.txt GitHub repo me push karein.
2. share.streamlit.io par login -> New app -> repo, branch, `app.py` chunein -> Deploy.
3. Milne wala `https://<name>.streamlit.app` link team ko share karein.
