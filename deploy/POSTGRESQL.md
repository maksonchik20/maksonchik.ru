# PostgreSQL production runbook

Production uses PostgreSQL 17 in the `who-update-postgresql` Docker container on
the same VM as Django. PostgreSQL listens only on `127.0.0.1:5432`; it is not
exposed to the internet.

## Files on the VM

- Data: `/srv/postgresql/data`
- PostgreSQL initialization environment: `/srv/postgresql/postgres.env`
- Django database environment: `/srv/maksonchik/database.env`
- Backups: `/srv/maksonchik/backups/postgresql`
- Final SQLite cutover copy: `/srv/maksonchik/backups/cutover-sqlite-20260831.sqlite3`

The environment files must remain readable only by root and the application
group. Never commit them to Git.

## Services

```text
who-update-postgresql.service
who-update-postgresql-backup.service
who-update-postgresql-backup.timer
```

The application services require PostgreSQL and load
`/srv/maksonchik/database.env`. The database service waits for `pg_isready`
before dependent services can start.

## Backups

The timer creates a compressed custom-format `pg_dump` every day around 02:30
Europe/Moscow and keeps 14 days. Each archive is checked with
`pg_restore --list` before it receives its final name.

Run a backup immediately:

```bash
sudo systemctl start who-update-postgresql-backup.service
sudo systemctl status who-update-postgresql-backup.service
```

Periodically verify an archive by restoring it into a separate temporary
database. Listing an archive alone does not test the whole restore path.

## Rollback to the final SQLite copy

1. Stop all application writers and Gunicorn.
2. Preserve the current PostgreSQL data and create one final `pg_dump`.
3. Copy the final SQLite backup to `/srv/maksonchik/app/db.sqlite3`.
4. Remove `DATABASE_ENGINE`/PostgreSQL variables from the application service
   environment (or temporarily remove `/srv/maksonchik/database.env`).
5. Start Gunicorn, workers, long polling, background tasks and timers.
6. Verify public pages, `/start`, queue sizes and database row counts.

Do not run PostgreSQL and SQLite writers at the same time. Data written to one
database after the cutover is not automatically replicated to the other.
