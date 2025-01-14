command = '/root/maksonchik20.ru/maksonchik.ru/venv/bin/gunicorn'
pythonpath = '/root/maksonchik20.ru/maksonchik.ru/main'
bind = 'maksonchik.ru:80'
workers = 1
user = 'root'
limit_request_fields = 32000
limit_request_field_size = 0
raw_env = 'DJANGO_SETTINGS_MODULE=main.settings'