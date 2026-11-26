# Changelog

All notable changes to the job tracker. Versions follow [semantic versioning](https://semver.org/)
(a new feature is a minor version, a fix a patch); each release is a git tag `vX.Y.Z`.
Run any of them with `launch.py --version X.Y.Z` (see the README's Versions section), and make a
new one with `release.py`. Write changes under **Unreleased** as you go.

## [Unreleased]
### Added
- With `--version local` (or `service.ps1 ... -Version local`) the tracker restarts by itself a
  moment after you save a `.py` file or `.env`, so your edits show up right away; a pull or
  re-screen that's running finishes first, and code that fails to start is retried on the next
  save. `--no-reload` (or `JOBS_RELOAD=0`) turns it off. `service.ps1 stop` also stops the launcher.
### Changed
- Pulls are much faster on LinkedIn: it's searched without opening every result's job page (up to
  200 per search, which took about 2 minutes a search and got pulls blocked). Descriptions are
  fetched afterwards only for new listings that pass your rules on what the listing shows (no AI),
  and for roles you have that still lack one. The pull summary lists the descriptions fetched per board.
### Removed
- Google Jobs is no longer a job site option. Google Search now answers programs that don't run
  JavaScript (JobSpy included) with a "turn on JavaScript" page instead of results, so every Google
  search came back empty, and JobSpy has no fix. Saved preferences that still list it skip it; if it
  was the only site picked, pulls use the default sites (LinkedIn, Indeed and ZipRecruiter) instead.
### Fixed
- ZipRecruiter searches returned nothing since Nov 23: ZipRecruiter now answers with a redirect,
  which JobSpy didn't follow, and the empty result wasn't reported as a failure. Searches follow
  the redirect again.
- A board that refuses every search in a pull is now reported instead of silently returning
  nothing: "ZipRecruiter blocked your searches" (or LinkedIn, Indeed, ...) in the pull summary and log.

## [1.8.0] - 2026-11-25
### Added
- Versions and this changelog. `launch.py` starts the latest release by default, a specific one
  with `--version 1.6.0`, or the code in your folder as it is with `--version local`;
  `launch.py --list` lists the releases. `service.ps1` and `start_tracker.bat` start through it
  (`service.ps1 restart -Version 1.6.0`, `service.ps1 versions`).
- `release.py X.Y.Z` makes a release: moves the Unreleased notes under the new version, sets the
  version number, commits and tags it.
- The version that's running shows at the bottom of the sidebar and in `service.ps1 status`.
- Database versions (migrations). The database stores its schema version; the 1.x structure is
  version 2. Changes to the structure are numbered migrations in `db.py` that run once, in order,
  each in its own transaction (a failed one changes nothing), on every database, new or old.
  A backup is saved before any upgrade (`...-before-1.8.0.db` in the backup folder).
- A release whose schema is older than your database's refuses to start instead of touching it;
  releases on the same schema run fine. `launch.py --list` shows each release's schema.
- `jobs.py db` shows the database's schema version, the migrations it has had, the newest version
  that has used it and its backups; `--check` checks the file for damage, `--backup` saves a copy.
### Changed
- `JOBS_LOG_DIR` sets where the server log goes.

## [1.7.0] - 2026-11-25
### Added
- Add role from a link: paste a job posting link and the form fills itself in for you to check.
  Indeed links use Indeed's job API and LinkedIn links its public job page; ZipRecruiter and
  Glassdoor descriptions come from the same readers a pull uses; any other public page is read
  from its job data, headings and text, in a headless browser if it's built with JavaScript.
  "Use AI to read the page" fills in what the page doesn't state plainly; the description is
  always the posting's own text, word for word.
- "Enter manually" for typing a role in yourself, and "Screen this role with AI after adding".
### Changed
- New roles start as Not applied, or Not a fit (with the reason) when the posting breaks one of
  your must-have preferences.

## [1.6.0] - 2026-11-25
### Added
- "Other roles at this company" at the bottom of a role's panel, matching the employer under any
  of its names, with "Show in list".
- Company filter in the sidebar.
### Changed
- Reset filters stays at the bottom of the sidebar while the filters scroll.

## [1.5.0] - 2026-11-24
### Added
- Pull new roles ▾ → Fetch missing descriptions, for LinkedIn and ZipRecruiter roles you already
  have, with "Assess with AI" and "Only open roles" switches.
### Changed
- The description editor's "Screen this role with AI" switch is on by default.
### Fixed
- A role the AI marked Not a fit stopped counting as an AI rejection once any later note was
  logged, so it could never be restored.

## [1.4.1] - 2026-11-24
### Fixed
- Remote listings with no location text were filtered out.
- Infinite and NaN numbers were accepted in role edits.
- Pay with only a minimum was annualised twice.
- A failed pull or re-screen kept what it had half-written.
- A change in JobSpy could fail a whole pull instead of skipping ZipRecruiter descriptions.
- Steps and questions are validated, and can't be attached to another role.
- Insights counts applications even after the role is archived.
### Changed
- The resume folder defaults to `job_search/resumes`.

## [1.4.0] - 2026-11-22
### Added
- ZipRecruiter descriptions that a pull didn't get are fetched again, slowly.
- The open role is kept in the URL, so a refresh reopens it.

## [1.3.0] - 2026-11-22
### Added
- Not a fit roles are archived automatically after a number of days.
- Location filter with every city in your roles and a Remote chip; Remote and places combine as "or".
### Changed
- "AI fit" is now "Assessed Fit".

## [1.2.0] - 2026-11-22
### Added
- The same posting is matched across differently named companies ("Globex" / "Globex.com
  Services LLC"), and `jobs.py dupes` / `jobs.py merge` merge duplicates.

## [1.1.0] - 2026-11-22
### Added
- Search, filters, sort and page are kept in the URL.
- "Don't show this again" on the pull confirmation.
- Location filter; Reset filters collapses the filter groups.

## [1.0.0] - 2026-11-22
### Added
- The job tracker: scrapes job boards with JobSpy, filters postings against your preferences,
  screens them with Claude, and tracks applications in a local web app (Flask + SQLite), with a
  command-line tool, Insights, daily backups and a Windows background service.
