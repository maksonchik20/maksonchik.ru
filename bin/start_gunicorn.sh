#!/bin/bash
source /root/maksonchik20.ru/maksonchik.ru/venv/bin/activate
exec gunicorn -c /root/maksonchik20.ru/maksonchik.ru/main/gunicorn_config.py main.wsgi