# Warehouse Stock Manager

Track products, restocks and stock-outs. When a product runs out, a **WhatsApp message is sent automatically** to the owner's phone (free, using CallMeBot).

Stack: Python (FastAPI). Data is kept in SQLite (a local file) or in Postgres (for free cloud hosting).

---

## A. Run it on one computer

Needs Python 3.10 or newer.

**Easiest (Windows):** open `start.bat` in Notepad, replace `CHANGE-THIS-PASSWORD` with your own password (8+ characters, avoid `%`), save, and double-click it. First run installs everything. (Mac/Linux: use the same flow as the manual steps below.)

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

Render runs the app, Neon keeps the data. You need three free accounts: GitHub, Neon and Render. Menu names on these sites may change slightly, but the steps stay the same. If they offer a region option, pick one close to your users.

### 1. Create the database (Neon)
1. Sign up at **neon.com** and create a project.
2. Find the **connection string** (it starts with `postgresql://` and ends with `sslmode=require`) and copy it. This is your `DATABASE_URL`. Treat it like a password.

### 2. Put the code on GitHub
1. Create a new repository (private is fine).
2. Upload the contents of the `warehouse-app` folder (the files `main.py`, `store.py`, `auth.py`, `requirements.txt`, the `static` folder, etc.). Do **not** upload a `venv` folder, any `.db` file, or a hidden `.env` file.

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
4. Click **Create Web Service**. When the build finishes, open the `https://<your-name>.onrender.com` link, log in, and set up the alerts (section C).

If the build complains about the Python version, add an environment variable `PYTHON_VERSION` set to a full version such as `3.12.8`.

### What to expect on the free plan
- **Sleeping:** Render puts the free app to sleep after 15 minutes without visits. The first visit afterwards takes about a minute to wake it up. Neon's database also sleeps after 5 minutes idle but wakes automatically.
- **Data is safe across sleeps and restarts** because it lives in Neon, not on Render's disk (which is wiped on every restart).
- **Limits:** Neon's free plan is 1 GB of data and a monthly compute allowance; Render's free plan has 750 hours a month. A single small warehouse should be far below these. Free plans can change, so monitor the pricing pages before depending on them.
- **Backups:** Neon's free restore history is only about 6 hours. Use the **Backup** button in the app regularly and keep the downloaded file somewhere safe. It contains all products and stock history, but it does not contain the API key or secret settings.
- **Alerts still work** because a stock-out is recorded through a visit, which wakes the app.

---

## C. Set up free Gmail alerts

Use a normal personal Gmail account with a Google App Password. That is the easiest free alert option and it works well on Android because the message arrives as an email notification.

### 1. Create a Google App Password
1. Open your Google Account: https://myaccount.google.com/
2. Go to **Security**.
3. Turn on **2-Step Verification** if it is not already enabled.
4. Under **Signing in to Google**, choose **App passwords**.
5. Select **Mail** and the device name you want.
6. Copy the generated 16-character password.

### 2. Add the email settings
Set these environment variables before starting the app:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your-16-char-app-password"
export ALERT_EMAIL_TO="you@gmail.com"
```

On Windows PowerShell:

```powershell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="you@gmail.com"
$env:SMTP_PASS="your-16-char-app-password"
$env:ALERT_EMAIL_TO="you@gmail.com"
```

Then restart the app. The app will send stock-out and expiry alerts by email automatically.

---

## D. Things to know

- **Alerts depend on stock being recorded.** The app can only alert when someone records the stock-out in it.
- **Email alerts are free and practical** for personal use, and they work well on Android as notifications.
- **Security:** use a long password. Logins last 12 hours. Anyone who knows the password can change the stock. The Gmail app password is stored in the app settings and is never sent back to the browser as a plain text value.
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

