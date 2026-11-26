# Job search tracker

Everything lives in **`jobs.db`**, a SQLite file. By default it's in this folder; set `JOBS_DB` in `.env` to keep it elsewhere (recommended: outside OneDrive, see [Backups and where the database lives](#backups-and-where-the-database-lives)).
Each role is stored with all its scraped metadata, its status, the date you applied, the path to the resume you used, notes and a status history.
The scraper skips postings already in the database. The same posting on two job boards (same URL or employer link, or same title and company within 30 days) is one role. Boards often name the employer differently ("Globex", "Globex.com", "Globex.com Services LLC"), so names are compared without case, punctuation, ".com" or legal endings like Inc and LLC, and one name may be the other plus more words, as long as the title and city match too; a repost or new opening after that gets its own row. When a later listing of a role you already have carries details the first one lacked (for example a description LinkedIn didn't return but ZipRecruiter did), they're filled in, never overwriting what's there, and the role is AI-screened again if it had no description to go on.

## 1. Run it (local deployment)
The tracker is a small web service on your computer:
- a Python API (Flask, served by Waitress, a production server) at **http://localhost:5055**
- a web UI that does everything through that API
- a SQLite database file, `jobs.db`

### Always-on background service (recommended)
Install it once from the `job_search` folder. After that it starts by itself whenever you log in to Windows, runs with no window, and restarts itself if it crashes:
```powershell
powershell -ExecutionPolicy Bypass -File service.ps1 install
```
Other commands:
```powershell
powershell -ExecutionPolicy Bypass -File service.ps1 status
```
```powershell
powershell -ExecutionPolicy Bypass -File service.ps1 restart
```
```powershell
powershell -ExecutionPolicy Bypass -File service.ps1 logs
```
```powershell
powershell -ExecutionPolicy Bypass -File service.ps1 uninstall
```
Run `restart` after updating code. `uninstall` removes the auto-start and stops the server. The service runs the **latest release** unless you pick a version (see [Versions](#versions)).

### Or run it in a window (for debugging)
Double-click `start_tracker.bat`, or run:
```powershell
.\.venv\Scripts\python.exe launch.py
```

### Versions
Each release has a version number (`MAJOR.MINOR.PATCH`: a new feature is the next minor version, a fix the next patch) and a git tag `vX.Y.Z`; [CHANGELOG.md](CHANGELOG.md) says what changed in each. The version that's running shows at the bottom of the sidebar.

- **Which version runs:** the latest release, unless you say otherwise. Run your own unreleased changes with `local` (it restarts by itself a moment after you save a `.py` file or `.env`, so edits show up right away; a running pull finishes first), or an older release by its number:
  ```powershell
  powershell -ExecutionPolicy Bypass -File service.ps1 restart -Version local
  ```
  ```powershell
  powershell -ExecutionPolicy Bypass -File service.ps1 restart -Version 1.6.0
  ```
  ```powershell
  powershell -ExecutionPolicy Bypass -File service.ps1 restart -Version latest
  ```
  The service remembers the version (also after you log in again). In a window: `start_tracker.bat --version 1.6.0`, or `.\.venv\Scripts\python.exe launch.py --version 1.6.0` (add a script to run something else at that version, e.g. `launch.py --version 1.6.0 jobs.py list`).
- **List the releases:** `service.ps1 versions` (or `launch.py --list`) shows them, what's in your folder, and which version last used your database.
- **How older releases run:** a release other than the code in your folder is unpacked once into `%LOCALAPPDATA%\JobTracker\versions\<version>` (`JOBS_VERSIONS_DIR`) and run with your folder's settings, database, resumes and virtual environment. It needs Git.
- **Database versions:** your database stores its schema version (the structure of its tables). Releases 1.0.0 to 1.8.0 all use schema 2. When a release changes the structure, it upgrades your database the first time it starts: it saves a backup first (`jobs-<time>-before-1.9.0.db` in the backup folder), then runs each change it hasn't had yet, in order and in its own transaction, so a change that fails is undone and your data is left as it was.
- **Going back to an older release** works as long as it uses the same schema. A release that's older than your database's schema refuses to start rather than risk your data (`launch.py --list` shows which schema each release uses). To go back past a schema change, stop the tracker, restore the backup from before the upgrade (or point `JOBS_DB` at it), then start that release.
- **Check your database** with `.\.venv\Scripts\python.exe jobs.py db`: its schema version, the changes it has had, which version last used it and its backups. Add `--check` to check the file for damage, or `--backup` to save a copy now.
- **Changing the database (for contributors):** never edit the tables in place. Add a numbered migration in `db.py` (see "Database versions" there) and raise `SCHEMA_VERSION`; every existing database gets the change the next time it starts, and new databases take the same path.
- **Making a release:** write what changed under **Unreleased** in `CHANGELOG.md` as you go, commit, then run `.\.venv\Scripts\python.exe release.py 1.9.0`. It sets the version, dates the changelog entry, commits and tags `v1.9.0`; then push with `git push origin <branch> --follow-tags`.

### Configuration
Settings go in `.env` and are all optional except the API key:

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | key for the AI screen |
| `JOBS_PORT` | `5055` | port |
| `JOBS_HOST` | `127.0.0.1` | `127.0.0.1` = only this computer can connect |
| `JOBS_DB` | `jobs.db` | database file location |
| `JOBS_RESUME_DIR` | `resumes` (inside `job_search`) | folder whose PDF/DOCX files appear in the resume picker; a relative path starts from `job_search` (`..` = the folder above). Only files in it can be opened from the app, so point it at your resumes, not your whole home folder |
| `JOBS_BACKUP_DIR` | `%LOCALAPPDATA%\JobTracker\backups` | where daily database backups go |
| `JOBS_ALLOWED_HOSTS` | — | extra host names the server answers to (only if you open it from another device; see the warning below) |
| `JOBS_LOG_DIR` | `logs` | where the server log goes |
| `JOBS_VERSIONS_DIR` | `%LOCALAPPDATA%\JobTrackerersions` | where older releases are unpacked to run them |
| `JOBS_THREADS` | `8` | web server worker threads |

The server only answers requests addressed to `localhost` / `127.0.0.1`, and refuses changes sent from other websites, so a page you visit can't use the tracker behind your back.

> **There is no login.** Setting `JOBS_HOST=0.0.0.0` and adding names to `JOBS_ALLOWED_HOSTS` lets every device on your network read and change your applications, notes and resumes and start paid AI screens. Only do that on a network you trust, and prefer a VPN or SSH tunnel to `127.0.0.1` instead.

### Backups and where the database lives
- **Daily backups:** the server (and each pull) copies the database to `JOBS_BACKUP_DIR` once a day and keeps the last 14. Before a schema upgrade it also makes a `-before-upgrade` copy. Backups are written to a temp file first and moved in when complete, so it's safe to keep them in OneDrive.
- **Restoring a backup:** stop the tracker (`service.ps1 stop`), then in the folder that holds `jobs.db` (`JOBS_DB` in `.env`, otherwise this folder) delete `jobs.db-wal` and `jobs.db-shm` if they exist. Leftover ones would be replayed onto the restored file and corrupt it. Then copy the backup over `jobs.db` and run `service.ps1 start`.
- **Keep the live database out of OneDrive.** This folder is synced by OneDrive, and syncing a SQLite file while it's open can corrupt it or create conflict copies. The server logs a warning while it is. To move it, stop the tracker:
  ```powershell
  powershell -ExecutionPolicy Bypass -File service.ps1 stop
  ```
  move the files:
  ```powershell
  New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\JobTracker"; Move-Item jobs.db, jobs.db-wal, jobs.db-shm "$env:LOCALAPPDATA\JobTracker\" -ErrorAction SilentlyContinue
  ```
  then add `JOBS_DB=C:\Users\<you>\AppData\Local\JobTracker\jobs.db` to `.env` and run `service.ps1 start`.
- `.env` holds your API key, so OneDrive syncs it too. If that bothers you, set `ANTHROPIC_API_KEY` as a Windows user environment variable instead; real environment variables take priority over `.env`.

### API
- `GET /api` lists every endpoint.
- `GET /api/health` is the health check.
- Server logs are written to `logs/server.log`.

### Fresh install on another machine
```powershell
python -m venv .venv
```
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
Then create `.env` and run `service.ps1 install`.

## Using the UI
- **Pull new roles** (top right) scrapes the job boards using your saved preferences, adds new matches as **Not applied**, then has Claude screen them. It asks for confirmation first, because the AI screen costs money (about $0.0005 per role with Haiku 5.5). Tick **Don't show this again** to skip that step in this browser; turn **Ask before each pull** back on in Preferences → AI screening. A run takes about 2-5 minutes. Only one pull or re-screen runs at a time, including the daily scheduled one; starting a second shows a message instead. When it finishes, a summary shows how many searches ran, postings found per board, how many were already tracked (and which got missing details filled in), why the rest were filtered out, how many new roles were added, and what the AI screen did and cost.
- **Pull new roles ▾ → Fetch missing descriptions** re-reads the job page of roles you already have that came without a description (LinkedIn and ZipRecruiter; other boards' pages can't be re-read, so add those by hand). The dialog shows how many roles are missing one and has two switches: **Assess with AI** (on by default) screens every role that gets a description, and a role the AI had marked Not a fit goes back to Not applied if it now fits; **Only open roles** (on by default) leaves out Not a fit and archived roles. Pages are fetched a few seconds apart, at most 100 per board per run, stopping early if a board keeps refusing; run it again later for the rest. It shows up in the pull history as a Descriptions run.
- **Preferences:** click the logo and choose Preferences to edit:
  - search terms, locations, experience and pay
  - employment type, title and company filters
  - industries, company size and visa sponsorship
  - interests and a free-text description

  Set each preference to **Must have** (roles that clearly break it are marked Not a fit) or **Nice to have** (it only moves the match level). This page also shows pull history and AI spend.
- **Fit column:** the AI's match level for each role:

  | Level | Meaning |
  |---|---|
  | Strong | meets every must-have and most nice-to-haves, and lines up with your interests |
  | Good | meets every must-have, with a gap or two |
  | Partial | breaks no must-have, but has notable gaps or key details missing |
  | Weak | allowed, but a poor match |
  | Not a fit | breaks a must-have |
  | Unknown | not enough information to judge |

  Hover over a level to see its definition and the AI's reason. Click the column header to sort from Strong down.
- **Teal rows marked NEW** were posted today or yesterday and you haven't applied yet. (Job boards only give the posting date, not the time, so "the last 24 hours" can't be measured exactly.)
- **Insights** (logo menu → Insights) shows applying stats and targets. It sticks to what you control, reviewing roles and applying, rather than interview or offer outcomes.
  - **Targets:** applications per week (with a streak count) and a maximum for roles waiting for review. Each has a progress bar and a note such as "6 to go · 2 days left this week" or "Review 12 more to get under 20". Click **Edit** on a card to change its target.
  - **Applying stats:** total applications, weekly average (over the weeks you've used the tracker), best week, and average days from a role being posted to you applying. Archived roles are left out.
  - **Charts** for the last 8, 12 or 26 weeks: applications per week (with your target line), the review funnel (Tracked → Reviewed → Applied; roles the AI marked Not a fit don't count as reviewed), the match levels of roles you applied to and of roles waiting for review, new roles found per week, and AI spend per week.
  - **Details:** hover over a bar for its value, or click **Table** to see the numbers. All of it comes from `GET /api/insights`; targets are set with `PUT /api/targets`.
- **Default sort:** Not applied first, then Interested, Interviewing, Offer, Applied, Rejected, Withdrawn and No longer accepting applications, with Not a fit last.
- **Search** by title, company, location, description or notes. Press `/` to jump to the search box.
- **Filters** are in the left sidebar, in groups that collapse (click a heading; which are open is remembered). A badge on a group shows how many of its filters are on, and **Reset filters** clears them all and collapses every group.
  - **Status:** click one to show only that status; once one is selected, clicking others adds them.
  - **Location:** a **Remote** chip, and places: type a city or state and press Enter, or click any city from your roles (most common first, including the city a remote listing names; **+N more** shows the rest). Matching is by whole word, any case ("CA" matches "San Jose, CA" but not "Chicago"). Everything picked here is an "or": Remote plus Brooklyn shows remote roles and Brooklyn roles.
  - **Company:** type an employer and press Enter (the box suggests every employer you have), or click one of the employers with the most roles. A company matches under any of its names ("Globex", "Globex.com", "Globex Services LLC"); several companies match any of them. Kept in the URL as `co=`.
  - **Assessed Fit:** the AI's match level: Strong, Good, Partial, Weak, Not a fit, Unknown or Not screened.
  - **Interests:** roles tagged with your interests.
  - **Role details:** pay listed, has a description, pays up to at least $X, and years required at most N (roles that don't say are kept).
  - **Dates:** when the role was posted, and when it was added to the tracker.
  - **Source:** the job board it came from, or added by you.
  - **More:** has a step to do (a screen or interview still to do), and include archived roles.
  - **The address bar keeps your view:** the search, filters, sort and page are in the URL, for example `http://localhost:5055/#/?q=machine%20learning&fit=strong,good&remote=1`. Bookmark it, share it or reload; opening a link like that applies its filters. The role you have open is in it too (`role=69`), so a refresh reopens it.
- **Click a role** to open its details:
  - set the status with one click
  - set the applied date
  - pick the resume version you used (any PDF or DOCX in the resume folder, `JOBS_RESUME_DIR`, appears in the list)
  - add notes
  - edit details
  - read the full description, with experience requirements highlighted
  - at the bottom, **Other roles at** the company: your other roles at the same employer (under any of its names, archived ones marked), with their status and fit. Click one to open it, or **Show in list** to filter the roles list to that employer
  - add or edit the description yourself (**+ Add description** / **Edit**) when the scrape didn't get it, e.g. LinkedIn rate-limiting. Turn on the switch before saving to have the AI screen just that role against the new text, which fills in its fit, years and pay (about $0.0005; on by default)
  - see the status history
  - view all scraped metadata
- **Changing status opens a popup** whose fields depend on the new status. Every status asks for notes, which are saved to the role's history. Some ask for more:
  - **Applied:** the date, the resume version you used, and the application or screening questions with your answers (**+ Add question**).
  - **Interviewing:** adds or updates a step: its type (recruiter screen, technical screen, take-home, onsite…), its date or deadline, and where it stands (to do / done and waiting to hear back / passed / rejected / cancelled). Choosing "passed" offers to add the next step straight away. You can also add the questions you were asked in that step.
  - **Offer:** the deadline to respond (with a countdown), the final salary, and other details such as bonus or equity.
  - **Rejected:** which stage it ended at.
  - **Not a fit:** a switch offers to block the company too, so future pulls skip its roles (off by default).

  **Skip details** changes the status without filling anything in.
- **Steps:** after you apply, each part of the process (recruiter call, technical screen, take-home, interviews) is a *step* on the role, not a separate status. The status stays broad (Applied → Interviewing → Offer) and each step has its own state:
  - **+ Add step** in the role panel's Steps card logs one. Adding a step to an Applied role moves it to Interviewing.
  - A step starts as **To do** (with an optional date or deadline). **Mark done** moves it to "done, waiting to hear back"; then set Passed or Rejected. Passing offers to add the next step.
  - The Status column shows the current step: the next one still to do, soonest first, e.g. "Tech screen · to do · Nov 25" (orange; red once the date has passed), otherwise the latest step, e.g. "Tech screen · waiting". For an offer it shows the response deadline.
- **The role panel** also has Offer and Questions & answers cards.
- **Archive** (button in the role panel) hides a role from the list, search and counts. Roles that have been **Not a fit** for 7 days are archived automatically; change the number of days or turn it off in Preferences → Archiving. A role you unarchive yourself stays unarchived. Turn on **Include archived** in the sidebar to see archived roles again. It turns itself off on every reload, and archived roles aren't loaded at all until you turn it on.
- **Keys:** `j` / `k` move to the next or previous role. `Esc` closes the panel.
- **Add role** is for jobs you found yourself. It warns you if you already have a role at that company. Choose how to fill it in:
  - **From a link:** paste the link to a job posting and press **Fill in from link**. The form below fills in (filled fields are marked) for you to check before you add the role; nothing is saved until then.
    - **Indeed** links use Indeed's job API, and **LinkedIn** links read the public job page (title, company, location, pay, description). **ZipRecruiter** and **Glassdoor** descriptions come from the same readers a pull uses.
    - **Any other public page** (a company careers page, Greenhouse, Lever, Ashby, Workable…) is read from the job data most career sites publish for Google Jobs, then its title, headings and text; pay and years are found with the same rules as a pull. Pages that build their content with JavaScript are opened in a headless Chrome or Edge (whichever is installed; set `JOBS_BROWSER` to pick one) with a fresh, empty profile, so none of your sign-ins are used.
    - **Use AI to read the page** (on by default when AI screening is set up; about $0.001) has Claude fill in what the page doesn't state plainly, such as the company or a pay range in the text. It keeps values that came from the job board or the page's job data. It never writes the description: that is always the posting's own text, word for word. When the description comes from the page's general text (which can include navigation or the application form), the AI only marks where the job description starts and ends, and the text in between is copied exactly as it is. The cost shows in the pull history as an Import run.
    - Pages behind a sign-in can't be read, and only public web addresses are fetched: links to this computer or your local network are refused, every redirect is checked, and the address is checked again when the connection is made (so a site can't switch its name to a private address in between). The headless browser loads everything through a small built-in proxy that only connects to public addresses, so a page can't send it to your local network either.
  - **Enter manually:** type the details in yourself.
  - New roles start as **Not applied**. When a link is read, its details are checked against your must-have preferences with the same rules a pull uses (blocked companies, title words, location, remote, employment type, years, pay); if one is broken, the status is set to **Not a fit** with the reason shown (and saved in the role's history). You can change it before adding.
  - Either way, **Screen this role with AI after adding** (on when AI is set up) assesses its fit once it's added, if it has a description; like a pulled role, a Not applied role the AI finds clearly breaks a must-have is marked Not a fit.
- **Theme:** the half-moon button switches between light and dark.

## AI screening setup
1. Create an API key at console.anthropic.com and add some prepaid credit. You can also set a monthly spend limit there.
2. Create a file named `.env` in this folder containing one line:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Restart the tracker. On the Preferences page, use **Test key**, then **Screen unscreened "Not applied" roles** to screen the roles already in the database.

Without a key, pulls still work. They just skip the AI screen.

## 2. Or update from the command line
```powershell
.\.venv\Scripts\python.exe jobs.py applied "globex"
```
This marks the role applied today, attached to your newest resume.

```powershell
.\.venv\Scripts\python.exe jobs.py applied initech --resume "C:\path\to\resume_v2.pdf"
```
```powershell
.\.venv\Scripts\python.exe jobs.py status initech interviewing --note "Phone screen Tue 2pm"
```
```powershell
.\.venv\Scripts\python.exe jobs.py add "Software Engineer" "Acme" --url https://... --status applied
```
```powershell
.\.venv\Scripts\python.exe jobs.py find acme
```
List roles that look like the same posting (read-only), then merge a group into one role. Merging keeps every history entry, step and question, the earliest applied date and the furthest status, fills in missing details, and backs up the database first:
```powershell
.\.venv\Scripts\python.exe jobs.py dupes
```
```powershell
.\.venv\Scripts\python.exe jobs.py merge 12 15 18
```
```powershell
.\.venv\Scripts\python.exe jobs.py list --status applied,interviewing
```
```powershell
.\.venv\Scripts\python.exe jobs.py scrape
```
If a search matches several roles, the tool lists them and asks which one you mean by `#id`.

Statuses: `not_applied`, `interested`, `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`, `closed` (no longer accepting applications), `not_a_fit`.

## 3. Daily scrape on a schedule (optional)
Run this once from the `job_search` folder. Windows Task Scheduler then runs the scrape at 8:00 AM every day with no window; output goes to `run_log.txt`.
```powershell
Register-ScheduledTask -TaskName "Daily Job Search" -Force -Trigger (New-ScheduledTaskTrigger -Daily -At 8am) -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Action (New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" -Argument "daily_jobs.py --trigger schedule" -WorkingDirectory "$PWD")
```
If you set it up earlier with `run_daily.bat`, running this replaces it.

## Search filters (Preferences page)
Filters are edited on the Preferences page and stored in the database, so your own search never lives in the code. A new install starts from neutral defaults: the search term "software engineer", posted in the last 24 hours, full-time, remote roles in the US (add your cities), and no experience, pay or title limits. Set the rest on the Preferences page:
- **Location:** cities with a radius, plus remote roles in the US. NYC boroughs count as New York.
- **Title:** words the title must include or must not include. When "must not include" is a must-have, senior level numbers such as "Engineer 5" or "E4" are removed too.
- **Experience:** the range of required years; when a description states its minimum, it must fit.
- **Pay:** the least the top of the posted range can be.
- **Blocked companies:** pulls skip every role from these companies. Add them here or when you mark a role Not a fit, and click × to unblock. "Acme" also blocks "Acme Corp", but not "Acmex".
- **Results per search:** at most this many postings per search, per board (40 by default). Turning off **Limit results per search** takes everything the boards return for the posted-within window, up to about 1,000 per search. That can get you throttled (LinkedIn especially) and makes AI screening costs less predictable, so the page shows a warning while it's off.

Each listing is filtered on its own, so a role posted in several cities is kept if any listing matches. Listings that fail the filters aren't remembered: if you loosen a preference, they're checked again on the next pull (while they're still inside the posted-within window).

## Files
`.env` holds your API key and is listed in `.gitignore`.

| File | What it is |
|---|---|
| `jobs.db` | the database (at `JOBS_DB` if set; backed up daily to `JOBS_BACKUP_DIR`) |
| `app.py` + `static/` | the web UI |
| `jobs.py` | command-line updates |
| `daily_jobs.py` | the scraper and filters |
| `db.py` | database schema and helpers |
| `launch.py` | starts the tracker at a chosen version (latest release by default) |
| `release.py`, `CHANGELOG.md` | making a release, and what changed in each |
| `results/` | each day's raw scrape |
| `tests/` | automated tests (see below) |

## Tests
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```
They use a temporary database, so `jobs.db` is never touched.

## Limitations
- LinkedIn blocks rapid page visits too, so a pull searches LinkedIn without opening each job's page, then opens the pages of only the listings worth it: new ones (not already in your tracker or seen before) that pass your rules on what the listing shows (title words, place, remote, company, type, listed pay; no AI), plus roles you have that still lack a description, a few seconds apart and at most 50 per pull. Years of experience are checked again once the description is in, before a role is added.
- ZipRecruiter refuses rapid requests: the scraper gets about 20 job pages in a row before it's blocked, so later listings arrive without a description. Each pull fetches those again, a few seconds apart, for listings that pass your filters (up to 50 per pull, stopping if ZipRecruiter keeps refusing); **Fetch missing descriptions** (or `jobs.py descriptions`, with `--all` to include Not a fit and archived roles and `--no-ai` to skip the AI) does the same for LinkedIn and ZipRecruiter roles you already have.
- LinkedIn sometimes rate-limits scraping ("429" errors). Those postings come in without a description: open the posting and paste it into the role with **+ Add description**.
- Experience and pay are read from description text, so check each posting before applying.
- Scraping is against LinkedIn's terms of service. One run a day is a reasonable pace.
