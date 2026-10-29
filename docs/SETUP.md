# Set up Klearing

This page takes you from nothing to a working set of books. Follow the steps
in order. Each step says what you should see when it worked.

Klearing is free and open source. Everything runs on your own computer. Your
transactions never go to a Klearing server, because there is no Klearing
server.

## Before you start

You need:

- A Windows 10 or 11 PC, or a Mac. On Linux, build from source (Path B).
- A bank connection. Pick one in step 4.
- About ten minutes.

Optional things you can add later: Klaviyo, Google Analytics 4, Gmail
notifications, Shopify, Stripe. You can skip all of them now.

## Step 1. Get Klearing

Pick one path.

**Path A. Download the app (easiest).**

1. Open the [latest release](https://github.com/Trostky1/klearing/releases/latest).
2. Download `Klearing-windows.zip` or `Klearing-macos.zip`.
3. Unzip it anywhere. Your Documents folder is fine.

You should see a `Klearing` folder (Windows) or `Klearing.app` (Mac).

**Path B. Run from source (any computer with Python 3.12, the version the tests use).**

```bash
git clone https://github.com/Trostky1/klearing
cd klearing
python -m venv venv
venv/Scripts/pip install -r requirements.txt
venv/Scripts/python main.py
```

On a Mac or Linux, use `venv/bin/pip` and `venv/bin/python`.

You should see a Klearing window open. Skip to step 3.

## Step 2. Open the app the first time (Path A only)

Klearing is not signed with a paid certificate, so your computer will warn you
once. This is normal.

- **Windows:** a blue "Windows protected your PC" screen appears. Click
  **More info**, then **Run anyway**.
- **Mac:** a message says Apple could not verify Klearing. Click **Done**.
  Open **System Settings, Privacy & Security**, scroll down, and click
  **Open Anyway**.

Pictures and every edge case are in [FIRST-LAUNCH.md](FIRST-LAUNCH.md).

You should see Klearing open in its own window with a Setup screen.

## Step 3. Set an app password

Setup step 1 asks for a password. This is a lock on the pages, so someone else
on your computer cannot read your finances.

1. Type a password you will remember.
2. Click continue.

You should see step 2.

If you skip the password, anyone who can open Klearing on this computer can use
it. Set one.

## Step 4. Name your business, then connect a bank

Setup step 2 asks for your business name. Type it and continue.

Setup step 3 connects a bank. Pick the row that fits you:

| Your bank | Pick | What you need |
|---|---|---|
| Mercury | Mercury | A read-only API token from Mercury. |
| Wells Fargo, Chase, a credit union, or most others | Plaid | Your own free Plaid account. Plaid may charge for real production use. |
| A bank Plaid does not cover | SimpleFIN Bridge | A SimpleFIN account. SimpleFIN sets its own price. |

Rules that save time:

- **Paste tokens. Do not retype them.** A token is shown once.
- **Read-only only.** Klearing never moves money. It cannot.
- Click **Test Connection** before you continue. It checks the format at once,
  then tries the bank for real. The error says exactly what is wrong.

More detail for Plaid and SimpleFIN is in the [README](../README.md).

You should see a green check after Test Connection.

## Step 5. Skip the optional steps

Setup steps 4, 5 and 6 are Klaviyo, Google Analytics 4 and email. Each has a
**Skip** option. Skip them now. You can connect any of them later in
**Settings, Connections**.

You should see step 7, "You're set."

## Step 6. Check your first transactions

1. Click **Review** in the sidebar.
2. Click **Check for new transactions**.
3. Wait a few seconds.

You should see your bank transactions listed. Nothing is recorded yet. Klearing
never records anything without your approval.

If the list is empty, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Step 7. Approve a few

1. Click **Review** on a row.
2. Check the category. Change it if it is wrong.
3. Click **Approve**.

You should see the row leave the list. It is now in your books. Open **Ledger**
to see it as a balanced journal entry.

## Step 8. Turn on the daily check

1. Open **Settings**, then the **Automation** tab.
2. Click **Turn automation on**.

Klearing now starts when you log in. It checks your bank every morning at 8, or
when the computer wakes. It sends a digest on Monday mornings. Your computer
must be on, or asleep, for the check to run.

You should see each job and its last result on the Automation tab.

## You are done

Your books are set up. From here:

- Make it yours: [CUSTOMIZE.md](CUSTOMIZE.md).
- Something went wrong: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- How your data stays safe: [../SECURITY.md](../SECURITY.md).

## How it works, in one minute

1. Your bank sends new transactions. Klearing puts them in **Review**.
2. You approve, change, or dismiss each one.
3. Approved ones become balanced entries in the **Ledger**.
4. Reports, **Home**, **Summary**, and **Tax** read from that Ledger.

Nothing is posted and nothing is sent unless you approve it.
