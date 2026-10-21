# Manual job candidates

Rows in `manual_jobs.csv` were added only where the exact URL appeared in a web
search result. No firm below was left unverified, but each seeded row points at
a program landing page or the firm's student job board rather than one posting,
and locations are a default of New York, NY. Nick: replace with per-role URLs
(and the right office) when you can open the pages.

- McKinsey: https://www.mckinsey.com/careers/students/undergraduate-degree (blocks automated requests; not fetched). Find the specific Business Analyst application page for your target campus.
- Bain: https://www.bain.com/careers/find-a-role/position/?jobid=10403 (Associate Consultant Internship; job id is cycle-specific and may be retired). Find the current US posting.
- Evercore, Jefferies, Moelis: tal.net student boards, which serve a bot check. Open each board, pick a specific US summer analyst posting, and swap in its URL.

Goldman Sachs is crawled automatically (`goldman` platform); nothing manual needed.
