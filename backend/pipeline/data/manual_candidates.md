# Manual job candidates

All five rows in `manual_jobs.csv` use a URL that appeared verbatim in a web
search result (Jefferies' page also returned HTTP 200). Nothing was left out as
unverified. The rows point at each firm's own program or students page, not at
a single posting, and the location is "United States" because no office is
known. Nick: when you can open these pages, swap in per-posting URLs and cities.

- McKinsey: https://www.mckinsey.com/careers/students/undergraduate-degree (blocks automated requests; not fetched).
- Bain: https://www.bain.com/careers/find-a-role/position/?jobid=10403 (job id is cycle-specific and may be retired; find the current US posting).
- Evercore: https://www.evercore.com/careers/students-graduates/ (returns 403 to scripts; not fetched).
- Jefferies: https://www.jefferies.com/careers/students-and-graduates/
- Moelis: https://www.moelis.com/careers/explore-opportunities/ (returns 403 to scripts; not fetched).

The firms' tal.net application boards serve a bot check and were deliberately not used.
Goldman Sachs is crawled automatically (`goldman` platform).
