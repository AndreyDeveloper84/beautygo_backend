#!/bin/bash
set -e

echo "Waiting for PostgreSQL..."
while ! python -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect(('db', 5432))
    s.close()
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    echo "PostgreSQL not ready, waiting..."
    sleep 1
done
echo "PostgreSQL is ready."

# All containers built from this image share the entrypoint. Behaviour
# branches by the CMD (compose ``command:`` field):
#   - no command  → web mode: migrate → collectstatic → gunicorn (default).
#   - any command → other-process mode (celery worker / celery beat / shell):
#     wait for db (above) then exec the command as-is. Skip migrate +
#     collectstatic — the web container is the single source of truth
#     for those, N containers running them in parallel race each other.
if [ "$#" -gt 0 ]; then
    echo "Running custom command: $*"
    exec "$@"
fi

echo "Applying migrations..."
python manage.py migrate --noinput

# DRF-2717: знание о процедурах из файла куратора
# (services/seeds/procedure_knowledge.json). На стенде нет оболочки, поэтому
# команда зовётся здесь, при каждом старте web. Она заводит только новые строки
# и существующие не трогает; пока файла нет — печатает это и завершается
# успехом. Сбой (например, код шаблона, которого нет на этом контуре) НЕ
# останавливает сайт: знание — не условие его работы. Но и не глотается молча —
# строка ниже уходит в stderr контейнера.
echo "Seeding procedure knowledge..."
python manage.py seed_procedure_knowledge || echo "WARNING: seed_procedure_knowledge failed - the site starts without the new knowledge rows" >&2

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting gunicorn..."
exec gunicorn djangoProject.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
