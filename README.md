# GlobMate — India Marketplace

Fresh Flask marketplace replacing the old Court Tracker application on the dedicated `globmate-conversion` branch. No Court Tracker application data or SQLite database is included.

## Included
- Customer and service-provider registration/login
- Service listings with pending/approved/rejected moderation
- Super Admin approval/rejection
- Public search by keyword, category and city
- Customer booking requests and booking history
- SQLite for local development; PostgreSQL through DATABASE_URL

## Local setup
```bash
python -m venv .venv
pip install -r requirements.txt
flask --app app init-db
flask --app app run
```

## Create Super Admin
Set `ADMIN_EMAIL` and a strong `ADMIN_PASSWORD`, then run:
```bash
flask --app app create-admin
```

## Render
Build: `pip install -r requirements.txt`
Start: `gunicorn app:app`
Set environment variables: `SECRET_KEY` (strong random value), `DATABASE_URL` (new empty PostgreSQL database), `ADMIN_EMAIL`, `ADMIN_PASSWORD`, optionally `ADMIN_NAME`.

**Important:** Connect a new empty PostgreSQL database. Do not point this app at the old Court Tracker database. Payments, automated commissions, provider payouts, KYC, notifications, reviews and production security hardening are not yet implemented. This is an MVP foundation, not a production-ready marketplace.
