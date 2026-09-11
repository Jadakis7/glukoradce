#!/bin/bash
# První spuštění vytvoří virtuální prostředí; při změně requirements.txt se závislosti doinstalují.
# Server běží ve smyčce: když se ukončí kódem 3 (změna kódu / tlačítko Restartovat) nebo spadne,
# spustí se znovu. Ctrl+C smyčku ukončí.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && touch .venv/.installed
fi
trap 'echo; echo "Glukorádce ukončen."; exit 0' INT TERM
while true; do
  .venv/bin/python app.py "$@"
  code=$?
  if [ $code -eq 3 ]; then
    echo "--- restart (změna kódu) ---"
    sleep 1
  elif [ $code -eq 0 ] || [ $code -eq 130 ]; then
    exit 0
  else
    echo "--- server skončil s chybou ($code), spouštím znovu za 5 s (Ctrl+C = konec) ---"
    sleep 5
  fi
done
