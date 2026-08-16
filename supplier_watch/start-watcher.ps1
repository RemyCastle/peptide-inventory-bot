# Start the supplier price watcher (separate process from the shop bot).
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m supplier_watch.watcher
