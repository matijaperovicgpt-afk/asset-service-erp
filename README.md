# asset-service-erp

A Django ERP for businesses that service, store and transport physical assets (machines, equipment, vehicles). It tracks each job as a case from the first fault report to handover.

- **Portal**: clients report a fault through a public form. They don't need an account.
- **Back office**: staff triage reports into clients and assets, then run intake, quotes (with per-role approval limits), service work orders, storage on a to-scale yard layout, transport scheduling and handover.
- **Workflow**: statuses and transitions are stored in the database, so you can change them in the Django admin without touching code.

Stack: Django 5, Python 3.12, MySQL 8, HTMX, Bootstrap. Redis/Celery is optional.

## Quickstart

```bash
cp .env.example .env
docker compose up -d mysql
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_workflow      # statuses, transitions, approval limits
python manage.py createsuperuser
python manage.py runserver
```

- Portal: http://localhost:8000/
- Back office: http://localhost:8000/backoffice/
- Admin: http://localhost:8000/admin/

## Usage

1. In the admin, create staff users and set each user's `role` (front desk, technician, manager or director).
2. Enter your price lists under the back-office pricing pages for Service, Storage and Transport.
3. Cases come in from the portal, or staff enter them in the back office. Each case page shows only the next steps its current status allows.

Branding, locations, language and time zone are environment variables. See `.env.example`.

## Tests

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
```

## Deployment

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` and the database credentials in `.env` first. An nginx example config is in `docker/`. If you set `PORTAL_ENABLED=False`, the app runs for staff only, with no public portal.

## License

MIT. Bundled assets keep their own licenses: Bootstrap and htmx (MIT), Barlow and Playfair Display fonts (OFL 1.1).
