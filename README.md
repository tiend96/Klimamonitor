# Klima-Monitor 🌡️

Watches the **price and availability** of two air-conditioners across German
shops and pings you (e-mail + phone/desktop popup) when something changes:

- 🟢 back in stock / a shop starts selling it
- 🛒 more shops start selling it
- 💶 the lowest price drops
- 🎯 the price falls below a target you set

Tracked products:
- **Midea PortaSplit 12.000 BTU (3,5 kW)**
- **Be Cool BCPSK12KW (12.000 BTU, WiFi)**

It runs **for free, 24/7, in GitHub Actions** — your PC does **not** need to be on.

---

## How it works

`monitor.py` checks every source listed in [`config.yaml`](config.yaml), figures
out the lowest obtainable price and how many shops sell each product, compares
against the last run (`state.json`), and notifies you only when something
**changed**. A GitHub Action runs it every 30 minutes.

Each source returns one of: `online`, `preorder`, `store_only` (e.g. OBI in-store
pickup), `out`, or `unknown`. A blocked/failed request is **`unknown`**, never
mistaken for "out of stock" — so you won't get false alarms.

---

## One-time setup (~15 min)

### 1. Get your notification channels ready

**ntfy (the popup, on phone + desktop) — free**
1. Think up a long, unguessable topic name, e.g. `klima-monitor-9f3k2x7q`
   (anyone who knows the topic can read it, so make it random).
2. Install the **ntfy** app on your phone (iOS/Android) and/or open
   `https://ntfy.sh/app` in your browser, and **subscribe to that topic**.
3. That's it — you'll keep this name for the secret `NTFY_TOPIC` below.

**Gmail (the e-mail) — free**
1. Your Google account needs 2-Step Verification enabled.
2. Create an **App password**: https://myaccount.google.com/apppasswords
   → pick "Mail" / "Other", name it "Klima", copy the 16-character code.
3. Use your address as `SMTP_USER` and that 16-char code as `SMTP_PASS`.
   (You can use only ntfy, only e-mail, or both — unset channels are skipped.)

### 2. Put the code on GitHub

1. Create a **new repository** (make it **Public** → unlimited free Actions
   minutes; the secrets stay encrypted and private either way).
2. Upload all the files in this folder to the repo (or `git push` them).

### 3. Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add the ones you want:

| Secret name   | Value |
|---------------|-------|
| `NTFY_TOPIC`  | your ntfy topic, e.g. `klima-monitor-9f3k2x7q` |
| `SMTP_USER`   | your Gmail address |
| `SMTP_PASS`   | the 16-char Gmail **App password** |
| `EMAIL_TO`    | where to send (optional; defaults to `SMTP_USER`) |

(Optional: `NTFY_SERVER`, `NTFY_TOKEN`, `SMTP_HOST`, `SMTP_PORT` if you use a
non-default server.)

### 4. Turn it on & test

1. Open the **Actions** tab → enable workflows if prompted.
2. Click **"Klima price & stock monitor" → Run workflow**. In the optional
   "mode" box type `--test` and run it → you should get a test e-mail/popup
   within a minute. (Empty box = a normal check.)
3. Run it once more with the box empty. The **first real run** sends a
   "✅ Monitor gestartet" message with the current status, then it goes quiet
   and only pings you on changes. Done — it now runs automatically every 30 min.

> GitHub auto-pauses scheduled workflows after **60 days with no commits**. The
> monitor commits `state.json` whenever something changes, which usually keeps it
> alive; if you ever get the "workflow disabled" e-mail, just click *Enable*.

---

## Run it locally (optional)

```powershell
pip install -r requirements.txt
python monitor.py            # check once, print status, notify on changes
python monitor.py --summary  # also send a full status report
python monitor.py --test     # send a test notification and exit
python monitor.py --quiet    # check + update state, never notify
```

For local notifications, set the same variables in your shell first, e.g.:

```powershell
$env:NTFY_TOPIC="klima-monitor-9f3k2x7q"
$env:SMTP_USER="you@gmail.com"; $env:SMTP_PASS="your16charapppw"
python monitor.py --test
```

(See [`.env.example`](.env.example) for the full list.)

---

## Tweaking it

Everything lives in [`config.yaml`](config.yaml):

- **Change a target price** — edit `target_price` (EUR) under a product, or
  delete the line to be alerted on *any* change.
- **Add another shop** — copy a source block and set:
  - `type: jsonld` for a normal shop product page (works for most German shops),
  - `type: shopify` for a Shopify store,
  - `type: geizhals` for a Geizhals/heise Preisvergleich page.
- **Check more/less often** — edit the `cron` in
  [`.github/workflows/check.yml`](.github/workflows/check.yml)
  (`*/30 * * * *` = every 30 min; `*/15 * * * *` = every 15 min).

### Notes on the sources

- **idealo, Bauhaus, Geizhals (direct), Amazon** aggressively block bots — even
  from a home connection, and more so from cloud IPs. They're marked best-effort
  and simply show `unknown` when blocked. **heise Preisvergleich** uses the same
  database as Geizhals and *does* respond, so it gives a market-wide "is anyone
  selling it?" signal that already includes Amazon/OBI/MediaMarkt offers.
- The heise price is rendered by JavaScript, so that source reports
  **availability** reliably but not always an exact price; the direct shops
  (tado, OBI, …) provide the actual numbers.
- ⚠️ **Verify the Midea heise URL** in `config.yaml` points to the **12.000 BTU /
  3,5 kW** listing before trusting its availability signal; replace it if needed.
- If a source from the cloud is *always* blocked and you really want it, the fix
  is to route that one request through a scraping proxy (e.g. ScraperAPI free
  tier) — ask and it can be wired in.
