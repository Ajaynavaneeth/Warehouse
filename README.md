# Warehouse Stock Manager

Track products, restocks and stock-outs. When a product runs out, a **WhatsApp message is sent automatically** to the owner's phone (free, using CallMeBot).

Stack: Python (FastAPI). Data is kept in SQLite (a local file) or in Postgres (for free cloud hosting).

---

## A. Run it on one computer

Needs Python 3.10 or newer.

**Easiest (Windows):** open `start.bat` in Notepad, replace `CHANGE-THIS-PASSWORD` with your own password (8+ characters, avoid `%`), save, and double-click it. First run installs everything. (Mac/Linux: edit `start.sh`, then `./start.sh`.)

**Manually:**

```bash
python -m venv venv
# Windows: venv\Scripts\activate        Mac/Linux: source venv/bin/activate
pip install -r requirements.txt

# Mac / Linux
export APP_PASSWORD="your-long-password"
# Windows PowerShell
$env:APP_PASSWORD="your-long-password"

uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and log in. Phones on the same Wi-Fi can use `http://<this-computer's-IP>:8000`. The data is the file `warehouse.db`.

---

## B. Put it online for free (Render + Neon)

Render runs the app, Neon keeps the data. You need three free accounts: GitHub, Neon and Render. Menu names on these sites may change slightly, but the steps stay the same. If they offer a region choice, pick the same region for both (Singapore is closest to India).

### 1. Create the database (Neon)
1. Sign up at **neon.com** and create a project.
2. Find the **connection string** (it starts with `postgresql://` and ends with `sslmode=require`) and copy it. This is your `DATABASE_URL`. Treat it like a password.

### 2. Put the code on GitHub
1. Create a new repository (private is fine).
2. Upload the contents of the `warehouse-app` folder (the files `main.py`, `store.py`, `auth.py`, `requirements.txt`, the `static` folder, etc.). Do **not** upload a `venv` folder, any `.db` file, or `start.bat` / `start.sh` (those can contain your password and are only for running on one computer). The `.gitignore` already skips them if you use Git.

### 3. Create the web service (Render)
1. At **render.com**, choose **New → Web Service** and connect your GitHub repository.
2. Settings:
   - **Language/Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT --forwarded-allow-ips "*"`
   - **Instance Type:** Free
3. Add these **Environment Variables**:
   | Name | Value |
   |---|---|
   | `APP_PASSWORD` | the login password you choose (8+ characters) |
   | `DATABASE_URL` | the Neon connection string from step 1 |
   | `SESSION_SECRET` | any long random text (32+ characters), e.g. from `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
4. Click **Create Web Service**. When the build finishes, open the `https://<your-name>.onrender.com` link, log in, and set up the WhatsApp alerts (section C).

If the build complains about the Python version, add an environment variable `PYTHON_VERSION` set to a full version such as `3.12.8`.

### What to expect on the free plan
- **Sleeping:** Render puts the free app to sleep after 15 minutes without visits. The first visit afterwards takes about a minute to wake it up. Neon's database also sleeps after 5 minutes idle but keeps all data.
- **Data is safe across sleeps and restarts** because it lives in Neon, not on Render's disk (which is wiped on every restart).
- **Limits:** Neon's free plan is 1 GB of data and a monthly compute allowance; Render's free plan has 750 hours a month. A single small warehouse should be far below these. Free plans can change, so check each provider's pricing page.
- **Backups:** Neon's free restore history is only about 6 hours. Use the **Backup** button in the app regularly and keep the downloaded file somewhere safe. It contains all products and stock history.
- **Alerts still work** because a stock-out is recorded through a visit, which wakes the app.

---

## C. Set up free WhatsApp alerts (one time, about 5 minutes)

The alert goes through CallMeBot's free WhatsApp API. The number that receives alerts is the number that activates it, so do these steps **from the phone that should get the alerts**:

1. Open **callmebot.com** and find the WhatsApp section. Save the bot's phone number in your contacts (the number changes sometimes, so copy the current one from the site).
2. From WhatsApp, send the bot the message **I allow callmebot to send me messages**.
3. The bot replies with an **API key**. (If nothing arrives within 2 minutes, CallMeBot says to try again after 24 hours.)
4. In the app, click **🔔 WhatsApp alerts**, enter the same number and the API key, then press **Save & send test**.

From then on, every time a **− Stock out** takes a product to zero, the message arrives on WhatsApp. The **Alert Log** tab shows each alert and whether it was sent or failed. Tick "Also alert me when stock is low" for an early warning.

## D. Things to know

- **Alerts depend on stock being recorded.** The app can only alert when someone records the stock-out in it.
- **CallMeBot is free for personal use only.** It can send only to the number that activated it (no groups) and it is an unofficial service that can change or stop. For a business that depends on alerts, the paid, official options are the WhatsApp Business API through Twilio, or an SMS service such as MSG91 or Fast2SMS (India SMS needs DLT registration). Only `send_whatsapp()` in `store.py` would need to change.
- **Security:** use a long password. Logins last 12 hours. Anyone who knows the password can change the stock. The WhatsApp API key is stored in the database and is never sent back to the browser.
- **Changing the password** on Render: edit `APP_PASSWORD` in the service's Environment tab; it redeploys. To log everyone out, also change `SESSION_SECRET`.

## E. Tests

```bash
python -m unittest test_store test_auth -v
```

To run the database tests against Postgres too (this **drops and recreates** the app's tables in that database, so use an empty test database):

```bash
# Mac / Linux
TEST_DATABASE_URL="postgresql://user:password@host/testdb" python -m unittest test_store -v
```
