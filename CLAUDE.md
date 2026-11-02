# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Repo guide

- One Python module, `brief.py` (constants → indicators → fetch → render → email → self-test → CLI), and one shell entry point, `run.sh`. The README's "How it works" table names the function per stage.
- Entry points are load-bearing: `run.sh`, `brief.py --email | --out | --self-test`, the `SMTP_*` env names and `brief.html` are invoked by `com.umashankar.basebrief.plist` and `.github/workflows/daily-brief.yml` by exact name. Do not rename them.
- `--email` has no already-sent guard (two schedulers → two briefs, by design). Never run it while testing; never commit `.env`.
- Verify with `ruff check .`, `bash tests/smoke.sh`, `pytest -q` (43 tests, offline). `pytest` needs `pip install -r requirements-dev.txt`.
- `graphify-out/` is generated output; `requirements.txt` pins are shared with CI and must stay pinned.

<!-- source-fetch-toolkit:v1 (managed by ~/bin/add_source_fetch_section.py — edit there, not here) -->
## Source fetching for data collection & literature surveys

When a task needs a primary document (statute, regulator data, government release, paper, filing),
fetch the document itself — never cite a search snippet as if it had been read.

- **On the owner's Mac:** `fetch_source.py "<url | doi:… | celex:… | arxiv:…>" --out ./sources`
  (in `~/bin`; full guide `~/bin/SOURCE_FETCH_TOOLKIT.md`). Scheduled/unattended runs use only
  `bash /Users/umashankar/bin/fetch_source.sh "<target>"` (allowlisted) and then Read the `.txt` it reports.
- **Elsewhere (cloud sessions, CI):** follow the same ladder by hand — direct GET with a browser User-Agent →
  headless browser only if the page needs JavaScript → official alternates (DOI → OpenAlex
  `api.openalex.org/works/doi:<doi>` locations → repository copy; EUR-Lex → EU Publications Office
  `publications.europa.eu/resource/celex/<CELEX>`; arXiv pdf/abs) → Wayback Machine `id_` snapshot.
- **Hard rule:** a CAPTCHA, robot check, Cloudflare/Akamai/WAF challenge or login wall means stop on that
  host. Do not bypass it (no TLS impersonation, stealth browsers, identity rotation or challenge solving);
  look for the same document via another route, or report it as unavailable.
- **Record** for every figure: URL read, title/date, page/table, fetch route; mark abstract-only or
  snippet-only sources. Fetch politely (~1 request/second per host, no bulk crawling).
<!-- /source-fetch-toolkit -->
