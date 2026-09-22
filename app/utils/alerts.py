# utils/alerts.py
import dash_bootstrap_components as dbc

# This one list is the single source of truth:
global_external_alerts: list[dbc.Alert] = []
