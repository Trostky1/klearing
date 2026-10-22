# First launch: the security warnings you WILL see, and what to click

Klearing is distributed without a paid code-signing certificate
(Windows) or Apple notarization ($99/year) — keeping the app free of
recurring costs. The trade-off is honest and cosmetic-but-scary:
**both operating systems will warn you the first time you run it.**
The app is not damaged and nothing is wrong; the OS simply can't
verify a publisher signature that was never bought. Here is exactly
what you'll see and the safe way through, once, per machine.

## Windows 10 / 11

1. When you first run `Klearing.exe`, a blue full-screen dialog
   appears: **"Windows protected your PC — Microsoft Defender
   SmartScreen prevented an unrecognized app from starting."**
2. Click the small **More info** link under the text.
3. The app name appears, and a **Run anyway** button. Click it.
4. That's it — Windows remembers this file and won't ask again
   (until you install a new version, which is a new file).

Klearing then opens in **its own app window** (not a browser tab).
The window uses the Microsoft Edge WebView2 runtime, which ships with
Windows 10 and 11 — nothing extra to install. In the rare case it's
missing, Klearing falls back to opening in your default browser and
still works.

Optional variant that avoids the blue screen entirely: after
downloading the .zip and **before** extracting, right-click it →
Properties → check **Unblock** → OK, then extract.

**One real exception:** on PCs where **Smart App Control** is turned
on and in enforcement mode (some new Windows 11 machines), unsigned
apps are hard-blocked with no "Run anyway". Check Windows Security →
App & browser control. Turning Smart App Control off is permanent
(it can't be re-enabled without reinstalling Windows), so that's your
call to make, not ours — the honest alternative is running Klearing
from source with Python on that machine.

## macOS (Sequoia 15 and newer)

**The short version — no Terminal needed.** If macOS blocks the app
(*"cannot be scanned for malicious software," "Apple could not verify
'Klearing' is free of malware,"* or similar): open it once anyway, then
go to **System Settings → Privacy & Security**, scroll down, and click
**Open Anyway** next to the Klearing message. Enter your password to
confirm. That's the whole thing.

Step by step:

1. Double-click **Klearing.app**. A dialog appears with wording like
   **"Apple could not verify 'Klearing' is free of malware"** or
   **"'Klearing' can't be opened because Apple cannot check it for
   malicious software."** Click **Done** (not Move to Trash).
2. Open **System Settings → Privacy & Security**, scroll down to the
   **Security** section.
3. You'll see a line like *"Klearing was blocked to protect your Mac."*
   Click **Open Anyway**, then confirm and enter your password.
4. From then on it opens normally, in **its own app window** (using
   macOS's built-in WebKit — nothing extra to install).

The old right-click → Open trick was removed in macOS 15, so **Open
Anyway** in System Settings is the current path.

### Fallback: only if "Open Anyway" doesn't appear or doesn't work

On some macOS 26 (Tahoe) systems the dialog instead claims the app
**"is damaged and can't be opened,"** and occasionally the **Open
Anyway** button doesn't show at all. It isn't damaged — that's
Gatekeeper's wording for a quarantined unsigned app. **Only** in that
case, one Terminal line clears it (most users should never need this):

```
xattr -cr /Applications/Klearing.app
```

That strips the quarantine flag macOS adds to downloaded apps; adjust
the path if you keep Klearing.app somewhere other than Applications.
Then double-click the app again and it opens normally.

**Known cosmetic limitation:** because the app is unsigned, macOS may
re-ask "Klearing wants to use your keychain" after you install an
update (the new binary looks like a new app to Keychain). Click
Always Allow once and it's quiet until the next update.

## Why we ship it this way

Code-signing certificates for Windows cost real money per year, and
Apple notarization requires a $99/year developer account. Klearing's
ground rule is zero recurring costs — so instead of pretending the
warnings won't happen, this page tells you exactly what they look
like. If you're ever unsure you downloaded the real thing, get it
only from this project's official Releases page.
