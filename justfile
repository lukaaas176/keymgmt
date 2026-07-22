# keymgmt — common dev tasks. Run `just` for the list. (https://just.systems)
#
# Local development runs with DEBUG on (the built-in insecure key is fine here);
# production defaults are secure and live in the NixOS module — see DEPLOY.md.

export KEYMGMT_DEBUG := "1"

# list available recipes
default:
    @just --list

# run the dev server → http://127.0.0.1:8000
dev *ARGS:
    uv run python manage.py runserver {{ARGS}}

# run the test suite (login gate + secret guard relax automatically)
test *ARGS:
    uv run python manage.py test access {{ARGS}}

# apply database migrations
migrate:
    uv run python manage.py migrate

# create new migrations after model changes
makemigrations *ARGS:
    uv run python manage.py makemigrations {{ARGS}}

# Django system checks
check:
    uv run python manage.py check

# open the Django shell
shell:
    uv run python manage.py shell

# create/update a login user (interactive)
createuser:
    uv run python manage.py createsuperuser

# print a fresh SECRET_KEY (for a real deployment)
secret:
    @python3 -c 'import secrets; print(secrets.token_urlsafe(64))'

# bulk-import printouts from a folder or files
import *PATHS:
    uv run python manage.py loadpdfs {{PATHS}}

# capture hollow-cross (pending-removal) marks from a matrix PDF
import-removed pdf:
    uv run python manage.py import_removed {{pdf}}

# export the reprogramming worklist as a PDF
export-changes out="aenderungen.pdf":
    uv run python manage.py exportpdf --mode changes --size a4 -o {{out}}

# export the Soll/Ist diff matrix as a PDF
export-diff out="diff.pdf":
    uv run python manage.py exportpdf --mode diff --size a3 -o {{out}}

# back up the local dev SQLite file (online, WAL-safe)
backup out="db-backup.sqlite3":
    uv run python -c "import sqlite3; sqlite3.connect('db.sqlite3').backup(sqlite3.connect('{{out}}')); print('wrote {{out}}')"
