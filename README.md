# Home Doctors status page

A small status and uptime page for the Home Doctors website. It is not a part
of the website, so it still loads when the website does not.

- **The watcher** is `watch.py`, run by a scheduled GitHub Actions workflow
  (`.github/workflows/watch.yml`) about every 5 minutes.
- **The page** is `index.html`, served by GitHub Pages from this branch.
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
| `agency` | `https://app.homedoctors4u.com/hhn/login` | 200 |
| `feed` | `https://app.homedoctors4u.com/api/status` | 200, JSON, `"version": 1` |
| `site` | `https://www.homedoctors4u.com` | 200, after redirects |

Each request has a 10-second timeout, one retry after 5 seconds for no answer
or a 5xx, the fixed User-Agent `hd-status-watcher`, and no cookies.

The run appends one line to the history and rewrites the summary. The workflow
commits `data/` to this branch, and GitHub Pages publishes the branch.

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
`data/summary.json` from its own address and nothing else.

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

1. **Create the repository and push `main`.** On a free plan GitHub Pages
   needs a public repository.
2. **Settings > Pages > Build and deployment.** Source: **Deploy from a
   branch**. Branch: **main**, folder: **/ (root)**. Save. The `.nojekyll`
   file tells Pages to serve the files as they are.
3. **Settings > Actions > General.** Actions must be allowed (the workflow
   uses `actions/checkout` only). The workflow asks for `contents: write`
   itself. If the push step fails with a 403, set **Workflow permissions** to
   **Read and write permissions**.
4. **Actions > watch > Run workflow** once. Do not wait for the schedule: the
   first scheduled run can take a while to start.
5. If `main` gets a branch protection rule, it must let the Actions bot push,
   or the watcher cannot commit.

## Open before the first publish

- The `agency` address in the table above answered **404** from production on
  11/12/2026: the website does not serve the agency sign-in page at that
  address. Until `PROBES` holds the address that agencies use, the page reads
  "Some services have trouble." Change the address in `PROBES` and in the test
  `test_the_six_addresses`, then remove this section.

## Add a component

A page of our own:

1. Add `("key", "https://...")` to `PROBES` in `watch.py`. It is working when
   it answers 200.
2. Add `("key", "Name on the page")` to `COMPONENTS`. Name the service by
   what it does, never by a vendor.
3. If it is a page of the website, add the key to `PARTS_OF_THE_WEBSITE`.
4. Add the address to the test `test_the_six_addresses` and run the tests.

A service behind the website (email, fax, text messages): nothing to do here.
The website adds it to the feed, and the page shows it on the next run.

## Custom domain (later)

For `status.homedoctors4u.com`:

1. DNS: one `CNAME` record, `status` -> `<owner>.github.io` (the account that
   owns this repository).
2. Add a file named `CNAME` at the root of this branch with one line:
   `status.homedoctors4u.com`. Settings > Pages > Custom domain writes that
   file for you.
3. When the certificate is ready, turn on **Enforce HTTPS**.

The `CNAME` file is not in the repository yet.

## Known limits

- **The schedule.** 5 minutes is the shortest interval. GitHub can start a
  scheduled run late or skip one when it is busy. The page shows the time of
  the last check and says when it is old.
- **Pages builds.** GitHub documents a soft limit of 10 builds an hour for a
  site that is published from a branch, and this watcher commits up to 12
  times an hour. If the page falls behind the data, publish from the workflow
  instead: source **GitHub Actions**, with `actions/upload-pages-artifact`
  and `actions/deploy-pages`. That needs two more permissions (`pages: write`
  and `id-token: write`), and the limit does not apply to it.
- **No activity.** GitHub turns a scheduled workflow off in a public
  repository with no activity for 60 days. The data commits are activity.
- **One vantage point.** The checks come from GitHub's network. A fault
  between GitHub and us looks like trouble. That is why one failed check alone
  is not an incident.
- **The timeout** is 10 seconds per socket read, not for the whole request.
  The job stops after 5 minutes.
- **Growth.** About 8,600 lines a month, each about 200 to 500 bytes, and
  about 105,000 commits a year.
- **Public.** The page, the history and this code are public. Keep vendor
  names, internal host names and build versions out of every file.
