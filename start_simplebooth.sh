#!/usr/bin/env bash

# Attendre que Wayland soit prêt
sleep 2

# Rotation de l'écran
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
wlr-randr --output DSI-2 --transform 180

# Désactiver l'économiseur d'écran
xset s off -dpms 2>/dev/null || true

# Cacher le curseur
pkill unclutter 2>/dev/null
unclutter --timeout 0 --jitter 0 --hide-on-touch &

# Lancer l'application Flask
cd "/home/pi/SimpleBooth"
source "/home/pi/SimpleBooth/venv/bin/activate"
python app.py &

# Attendre que le serveur soit prêt
sleep 5

# Lancer Chromium en mode kiosk
exec chromium \
  --kiosk \
  --no-sandbox \
  --password-store=basic \
  --disable-infobars \
  --disable-features=TranslateUI,Translate,LockProfileCookieDatabase \
  --disable-translate \
  --disable-extensions \
  --disable-plugins \
  --disable-notifications \
  --disable-popup-blocking \
  --disable-default-apps \
  --disable-background-mode \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-field-trial-config \
  --disable-ipc-flooding-protection \
  --no-default-browser-check \
  --no-first-run \
  --disable-component-update \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --lang=fr \
  http://localhost:5000
