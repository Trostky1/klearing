# Connecting Google Analytics 4 (optional, honestly fiddly)

This is the same walkthrough the in-app guided setup shows
(Connections → Google Analytics → Guided setup), as a page you can
read ahead. GA4 unlocks the Insights tab: traffic and conversion
numbers next to your money, refreshed once a day. **Skipping it costs
you only that tab** — set it up whenever, or never.

## Read these two boxes first

> **Workspace warning.** If you're using a Google Workspace email (a
> custom domain, not @gmail.com) and hit a permission error creating
> a Cloud project, try a personal Gmail account instead — Workspace
> accounts often restrict project creation by default. The account
> only needs Viewer access to your Analytics property; it does not
> have to own your website.

> **Property ID vs Measurement ID.** The **Property ID** is a plain
> number (like `544081549`) — that's what Klearing needs. The
> **Measurement ID** starts with `G-` and belongs to your website's
> tag; it will NOT work here. (A third one, the Stream ID, can be
> ignored entirely.) Mixing these up is the single most common GA4
> setup mistake.

## The six stages

Each ends with a checkpoint. If a checkpoint doesn't match what you
see, stop and re-read that stage — pushing on is how you end up lost.

**1. Open Google Cloud and pick a project.**
Go to [console.cloud.google.com](https://console.cloud.google.com),
sign in, and use the project picker in the top bar (any existing
project works, or New Project — the name doesn't matter and nothing
here affects your website).
*Checkpoint: your project's name shows in the top bar.*

**2. Enable the Google Analytics Data API.**
Menu → APIs & Services → Library → search **"Google Analytics Data
API"** → Enable. Careful: you want the **Data API** (reads report
numbers). The similarly named **Admin API** manages Analytics
settings and is NOT the one — enabling only the Admin API is a
classic dead end.
*Checkpoint: the API's page shows a "Manage" button instead of
"Enable".*

**3. Create a service account.**
Menu → IAM & Admin → Service Accounts → Create service account. Name
it anything (say `klearing-reader`) and click through — it needs
**no roles** on the project. A service account is a robot identity
Klearing uses to read your numbers.
*Checkpoint: the list shows your account with an email ending in
`.iam.gserviceaccount.com`. Copy that email for stage 5.*

**4. Download a JSON key.**
Click the service account → Keys → Add key → Create new key → JSON →
Create. A `.json` file downloads — that's the credential you'll
upload to Klearing. Treat it like a password.
*Checkpoint: the file opens in a text editor and contains
`"type": "service_account"`.*

**5. Grant it access to your GA4 property.**
In [analytics.google.com](https://analytics.google.com): Admin (gear,
bottom left) → **Property access management** → "+" → Add users →
paste the service-account email → role **Viewer** → Add.
*Checkpoint: the email appears in the property's user list.*

If your account has more than one property, make sure you're granting
access on the one actually receiving data: open Reports → Realtime in
one tab, visit your website in another, and confirm your visit shows
up. Orphaned duplicate properties that receive nothing are a real
trap — access granted there "works" and every number stays zero.

**6. Find the Property ID.**
Admin → Property → Property details; the Property ID is at the top
right — a plain number.
*Checkpoint: you have a ~9–10 digit number, no letters, no `G-`.*

## Connect it in Klearing

Connections → Google Analytics → Guided setup: upload the JSON key,
enter the Property ID, click **Test Connection**. The test tells you
specifically if the key file is the wrong kind of file, if the ID is
actually a Measurement ID, or if the service account lacks access to
that property (and which email to add). Optionally list conversion
event names to track (e.g. `sign_up, purchase`); traffic and top
pages work with none.
