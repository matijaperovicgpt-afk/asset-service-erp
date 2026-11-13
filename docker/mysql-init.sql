-- Runs only on FIRST initialization of the MySQL data volume.
-- Grants the app user rights on the Django test database so that
-- `manage.py test` (which creates/drops `test_<DB_NAME>`) works.
GRANT ALL PRIVILEGES ON `test_erp`.* TO 'erp'@'%';
FLUSH PRIVILEGES;
