#!/usr/bin/with-contenv bashio

bashio::log.info "Batterie-Planer v2 startet..."
exec python3 /planner.py
