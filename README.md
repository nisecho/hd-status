# Home Doctors status page

A small status and uptime page for the Home Doctors website. It is not a part
of the website, so it still loads when the website does not.

- **The watcher** is `watch.py`, run by a scheduled GitHub Actions workflow
  (`.github/workflows/watch.yml`) about every 5 minutes.
- **The page** is `index.html`. The same workflow publishes it with GitHub
  Pages after every run.
- **The data** is in `data/`: one line per run in
  `data/history/<yyyy-mm>.jsonl`, and `data/summary.json`, which the page reads.

No server, no database, no other vendor and no secret.

## How it works

Every run asks six public addresses with a plain GET:

| Key | Address | It is working when |
|---|---|---|
| `app` | `https://app.homedoctors4u.com/api/health` | 200 and `"status": "healthy"` |
| `ready` | `https://app.homedoctors4u.com/api/health/ready` | 200 |
| `signin` | `https://app.homedoctors4u.com/login` | 200 |
| `agency` | `https://hhn.homedoctors4u.com/hhn/login` | 200 |
| `feed` | `https://app.homedoctors4u.com/api/status` | 200, JSON, `"version": 1` |
| `site` | `https://www.homedoctors4u.com` | 200, after redirects |

Each request has a 10-second timeout, one retry after 5 seconds for no answer
or a 5xx, the fixed User-Agent `hd-status-watcher`, and no cookies.

These are our three public host names: `app.` (the website for staff and
providers), `hhn.` (the agency portal) and `www.` (our public website). The
page and the data may name these three and no other host.

One run, in this order:

1. `watch.py` appends one line to the history and rewrites the summary.
2. The workflow commits `data/` to `main` and pushes.
3. It packs the site from that commit: `index.html`, `data/summary.json` and
   `assets/`, nothing else. Not `watch.py`, not `tests/`, not `.github/`, not
   the history.
4. A second job publishes the pack with GitHub Pages.

When the commit or the push fails, steps 3 and 4 do not run. The page keeps
the last published state, so it never shows a check that is not in the
history. When only step 4 fails, the data is safe in the repository and the
next run publishes it. The page says so when its newest check is more than 20
minutes old.

What the page shows:

- **The top sentence.** "The website is not answering." when the `app` check
  fails. "Some services have trouble." when another component is in trouble.
  Otherwise "All services are working."
- **Components.** Website (`app`), Staff and provider sign-in (`signin`),
  Agency portal sign-in (`agency`), Our public website (`site`), then the
  components of the status feed, with the names that the website gives them.
  The newest check decides "Working" or "Trouble since".
- **The readiness answer** (`ready`) is not a component of its own. When the
  website answers but is not ready, the Website component shows trouble with
  one sentence. The `degraded` mark is kept in the data and is not on the page.
- **Uptime** over 24 hours, 7 days, 30 days and 90 days: the share of checks
  that were ok. A window with fewer than 12 checks reads "not enough data yet".
- **The 90-day strip.** One bar per day, in US Central time. Good: no failed
  checks in a row. Partial: some. Bad: 10% or more of the day's checks. A
  failed check alone does not mark a day, except while it is the newest check.
- **Past incidents.** An incident is 2 or more failed checks in a row on one
  component. One failed check alone can be the watcher's own network. When the
  website itself is down, its sign-in pages do not add incidents of their own.
- **Old data.** If the newest check is more than 20 minutes old, the page says
  so and the top sentence loses its color.

### The status feed

`/api/status` is the website's own public answer about its services (contract
version 1):

```json
{"version": 1, "status": "ok", "components": [
  {"id": "fax", "name": "Fax", "state": "trouble",
   "since": "2026-11-12T22:45:02Z", "note": "..."}]}
```

The watcher keeps `id`, `name`, `state`, `since` and `note` and ignores every
other field. A 404, an answer that is not JSON, or a `version` that is not 1
means "no feed": the page then shows only what the watcher measured, with no
error.

## What it never stores

- The build version in the health answer.
- The list of internal checks in the readiness answer. Only its HTTP status
  and its top-level `status` and `degraded` are kept.
- Any field of the feed outside the contract.
- Response bodies, headers, cookies, IP addresses.
- Patient data. No address that the watcher asks returns any.

The page loads no font, script or tracker from another host. It reads
`data/summary.json` from its own address, and its typeface, mark and tab icon
from `assets/` at its own address, and nothing else.

## The look

The page looks like the website: the same typeface (Inter), grey scale, blue,
status badge (a dot and words on a tint) and plain cards with rows divided by
a line. The values are copied into `index.html` from the website's design
values on 11/13/2026; the page still loads nothing from the website, so it
keeps its look when the website is down. When the website's look changes,
copy the new values by hand. The page has one look, light, as the website
has.

`assets/` holds `inter-latin.woff2` (Inter 5.3.0, the Latin subset, under the
SIL Open Font License in `inter-LICENSE.txt`), `logo.png` (our mark) and
`favicon.png`. A working day is a white card with a green mark. Trouble tints
the top card, because colour marks only what needs attention.

## Run it locally

Python 3.10 or newer, standard library only.

```bash
python3 -m unittest discover -s tests   # the tests; none touches the network
python3 watch.py                        # one real run: six GETs, writes data/
python3 watch.py --summary-only         # rebuild data/summary.json, ask nothing
python3 watch.py --data /tmp/try        # write somewhere else
python3 -m http.server 8000 --bind 127.0.0.1   # then open http://127.0.0.1:8000/
```

The page needs a web server: a browser does not let `index.html` read
`data/summary.json` from a `file://` address.

Three tests run the page's own functions (the stale rule, the times) in Node.
Without Node they are skipped and say so.

## One-time GitHub settings

1. **Create the repository and push `main`.** On GitHub Free, Pages needs a
   public repository. `main` must be the default branch: a scheduled workflow
   runs only there.
2. **Settings > Pages > Build and deployment > Source: GitHub Actions.** Not
   "Deploy from a branch": GitHub does not build Pages from a commit that a
   workflow pushes with the built-in token, and a branch site has a soft limit
   of 10 builds an hour.
3. **Settings > Environments > github-pages.** GitHub creates this environment
   by itself. GitHub recommends a rule there so that only the default branch
   can deploy: check that `main` is allowed, and only `main`.
4. **Settings > Actions > General.** Actions must be allowed. The workflow
   uses three actions, all by GitHub: `actions/checkout`,
   `actions/upload-pages-artifact` (which calls `actions/upload-artifact`) and
   `actions/deploy-pages`. The workflow asks for its own permissions
   (`contents: write` in one job; `pages: write` and `id-token: write` in the
   other). If the push step fails with a 403, set **Workflow permissions** to
   **Read and write permissions**.
5. **Actions > watch > Run workflow** once. Do not wait for the schedule: the
   first scheduled run can take a while to start.
6. If `main` gets a branch protection rule, it must let the Actions bot push,
   or the watcher cannot commit.

No secret and no personal access token. Each action is pinned to a commit:

| Action | Version | Commit |
|---|---|---|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/upload-pages-artifact` | v5.0.0 | `fc324d3547104276b827a68afc52ff2a11cc49c9` |
| `actions/deploy-pages` | v5.0.1 | `368f82528645a54fb793d4d04e342629a3f51346` |

Read from each action's tags and release pages on 11/12/2026. To move to a
newer version, read the commit of the new tag there and change both the
workflow and this table.

## Add a component

A page of our own:

1. Add `("key", "https://...")` to `PROBES` in `watch.py`. It is working when
   it answers 200.
2. Add `("key", "Name on the page")` to `COMPONENTS`. Name the service by
   what it does, never by a vendor.
3. If it is a page of the website, add the key to `PARTS_OF_THE_WEBSITE`.
4. Add the address to the test `test_the_six_addresses` and run the tests.
   A new host name must be one that the public may know.

A service behind the website (email, fax, text messages): nothing to do here.
The website adds it to the feed, and the page shows it on the next run.

## Custom domain (later)

For `status.homedoctors4u.com`:

1. Settings > Pages > Custom domain: type `status.homedoctors4u.com`, Save.
2. DNS: one `CNAME` record, `status` -> `<owner>.github.io` (the account that
   owns this repository, with no repository name).
3. When the certificate is ready, turn on **Enforce HTTPS**. GitHub says this
   can take up to 24 hours.

There is no `CNAME` file. GitHub's documentation: with a custom GitHub
Actions workflow "no CNAME file is created, and any existing CNAME file is
ignored and is not required."

## Known limits

- **The schedule.** 5 minutes is the shortest interval. GitHub can start a
  scheduled run late, most of all at the start of an hour, and can drop one
  when it is busy. Measured here: from 11/13/2026 3 PM to 11/14/2026 noon
  Central, GitHub started 4 scheduled runs, 4 to 7 hours apart. So the
  schedule alone does not keep the page current. A run that someone starts
  (Actions > watch > Run workflow, or `gh workflow run watch`) starts in
  seconds, so a timer outside GitHub starts one every 5 minutes. The checks
  still run on GitHub. The schedule stays as the fallback for when that timer
  is off. The page shows the time of the last check and says when it is old.
- **One run at a time.** A run waits for the run before it, and at most one
  run waits. A deployment that hangs stops after 3 minutes.
- **GitHub Pages limits** (GitHub's documentation, read 11/12/2026): a
  published site may be no larger than 1 GB, a deployment times out after 10
  minutes, and there is a soft bandwidth limit of 100 GB a month. The soft
  limit of 10 builds an hour "does not apply if you build and publish your
  site with a custom GitHub Actions workflow", which this is. The published
  site is the page, its summary and four small files in `assets/`.
- **GitHub Pages terms.** The same page says: "GitHub Pages is not intended
  for or allowed to be used as a free web-hosting service to run your online
  business, e-commerce site, or any other website that is primarily directed
  at either facilitating commercial transactions or providing commercial
  software as a service (SaaS)." Our judgement: a status page is neither. It
  sells nothing and runs no service, and many projects publish their status
  page this way. The owner should know that the rule exists.
- **No activity.** GitHub turns a scheduled workflow off in a public
  repository with no activity for 60 days. The data commits are activity.
- **One vantage point.** The checks come from GitHub's network. A fault
  between GitHub and us looks like trouble. That is why one failed check alone
  is not an incident.
- **The timeout** is 10 seconds per socket read, not for the whole request.
  Each job stops after 5 minutes.
- **Growth.** About 8,600 lines a month, each about 200 to 500 bytes, and
  about 105,000 commits a year. The history is in the repository only; the
  published site does not grow.
- **Actions minutes.** Every run is two jobs. Standard runners are free in a
  public repository. A private repository would use paid minutes for about
  17,000 jobs a month.
- **Public.** The page, the history and this code are public. Keep vendor
  names, build versions and every host name other than our three public ones
  (`app.`, `hhn.` and `www.homedoctors4u.com`) out of every file.
