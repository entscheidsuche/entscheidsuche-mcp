#!/usr/bin/env bash
# Installations-Script für entscheidsuche-mcp auf Debian.
# Auf dem Zielserver als root ausführen — oder Schritte einzeln durchgehen.
#
#   curl -fsSL .../install.sh | bash
#
# Vorgänge:
#   1. Systemnutzer + Verzeichnis anlegen
#   2. Python-venv erzeugen, Abhängigkeiten installieren
#   3. systemd-Unit installieren und Service starten
#   4. Nginx-vhost installieren (TLS-Zertifikat anschließend mit certbot holen)

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/entscheidsuche/entscheidsuche-mcp.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/entscheidsuche-mcp}"
SERVICE_USER="${SERVICE_USER:-entscheidsuche}"
DOMAIN="${DOMAIN:-mcp.entscheidsuche.ch}"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo bash install.sh)" >&2
    exit 1
fi

echo "==> Pakete installieren"
apt-get update
apt-get install -y python3 python3-venv python3-pip git nginx

echo "==> Systemnutzer anlegen"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Code beziehen"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Python-venv erzeugen"
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install "$INSTALL_DIR"

echo "==> Konfigurations-Datei"
if [[ ! -f /etc/entscheidsuche-mcp.env ]]; then
    cp "$INSTALL_DIR/.env.example" /etc/entscheidsuche-mcp.env
    chmod 640 /etc/entscheidsuche-mcp.env
    chown root:"$SERVICE_USER" /etc/entscheidsuche-mcp.env
fi

echo "==> systemd-Unit installieren"
install -m 644 "$INSTALL_DIR/deploy/entscheidsuche-mcp.service" \
    /etc/systemd/system/entscheidsuche-mcp.service
mkdir -p /var/log/entscheidsuche-mcp
chown "$SERVICE_USER:$SERVICE_USER" /var/log/entscheidsuche-mcp
systemctl daemon-reload
systemctl enable --now entscheidsuche-mcp.service

echo "==> nginx vHost"
install -m 644 "$INSTALL_DIR/deploy/nginx.conf" \
    "/etc/nginx/sites-available/$DOMAIN"
ln -sf "../sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
nginx -t
systemctl reload nginx

echo
echo "Fertig. Nächster Schritt — TLS-Zertifikat:"
echo "    apt-get install -y certbot python3-certbot-nginx"
echo "    certbot --nginx -d $DOMAIN"
echo
echo "Status prüfen:"
echo "    systemctl status entscheidsuche-mcp"
echo "    journalctl -u entscheidsuche-mcp -f"
