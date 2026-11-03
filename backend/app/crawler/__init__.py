"""Website crawling (SPEC.md §7.3): safe fetching, robots.txt, sitemaps, BFS, extraction.

Nothing here touches the database or Gemini. The worker turns a crawl report
into documents through the M4 indexing path (app.ingestion.website).
"""
