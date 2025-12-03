#!/usr/bin/env bash
# ---------------------------------------------------------------------
# SimpleBooth WiFi Setup with Fallback Hotspot
# Configure un WiFi principal + WiFi de secours (hotspot téléphone)
# ---------------------------------------------------------------------

set -euo pipefail

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}╭─────────────────────────────────────────────────────────────╮${NC}"
echo -e "${GREEN}│     Configuration WiFi avec Hotspot de Secours             │${NC}"
echo -e "${GREEN}╰─────────────────────────────────────────────────────────────╯${NC}"
echo

# Vérifier les privilèges root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}❌ Ce script doit être exécuté en root (sudo)${NC}"
   exit 1
fi

# Fichier de configuration NetworkManager
NM_CONNECTIONS_DIR="/etc/NetworkManager/system-connections"
WPA_SUPPLICANT_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"

# Déterminer si on utilise NetworkManager ou wpa_supplicant
USE_NETWORK_MANAGER=false
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    USE_NETWORK_MANAGER=true
    echo -e "${GREEN}✓ NetworkManager détecté${NC}"
else
    echo -e "${YELLOW}ℹ wpa_supplicant sera utilisé${NC}"
fi

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${YELLOW}Configuration du WiFi PRINCIPAL (priorité haute)${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

read -p "📡 Nom du réseau WiFi principal (SSID): " MAIN_SSID
read -sp "🔐 Mot de passe WiFi principal: " MAIN_PASSWORD
echo

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${YELLOW}Configuration du WiFi de SECOURS (hotspot téléphone)${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

read -p "📱 Nom du hotspot téléphone (SSID): " FALLBACK_SSID
read -sp "🔐 Mot de passe hotspot: " FALLBACK_PASSWORD
echo

echo
echo -e "${YELLOW}📝 Récapitulatif:${NC}"
echo "   WiFi Principal : $MAIN_SSID (priorité 10)"
echo "   WiFi Secours   : $FALLBACK_SSID (priorité 5)"
echo
read -p "Confirmer la configuration? (o/N): " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Oo]$ ]]; then
    echo "Annulé."
    exit 0
fi

if $USE_NETWORK_MANAGER; then
    # ==========================================
    # Configuration via NetworkManager
    # ==========================================
    echo
    echo -e "${GREEN}▶ Configuration via NetworkManager...${NC}"
    
    # Supprimer les anciennes connexions si elles existent
    nmcli connection delete "$MAIN_SSID" 2>/dev/null || true
    nmcli connection delete "$FALLBACK_SSID" 2>/dev/null || true
    
    # Créer la connexion WiFi principale (priorité haute)
    nmcli connection add \
        type wifi \
        con-name "$MAIN_SSID" \
        ssid "$MAIN_SSID" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$MAIN_PASSWORD" \
        connection.autoconnect yes \
        connection.autoconnect-priority 10
    
    echo -e "${GREEN}✓ WiFi principal configuré${NC}"
    
    # Créer la connexion WiFi de secours (priorité basse)
    nmcli connection add \
        type wifi \
        con-name "$FALLBACK_SSID" \
        ssid "$FALLBACK_SSID" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$FALLBACK_PASSWORD" \
        connection.autoconnect yes \
        connection.autoconnect-priority 5
    
    echo -e "${GREEN}✓ WiFi de secours configuré${NC}"
    
    # Activer l'auto-roaming
    echo -e "${GREEN}▶ Activation de l'auto-roaming...${NC}"
    
    # Créer un dispatcher pour le roaming automatique
    cat > /etc/NetworkManager/dispatcher.d/99-wifi-fallback << 'DISPATCHER'
#!/bin/bash
# Script de fallback WiFi automatique

INTERFACE=$1
STATUS=$2

# Seulement pour les événements réseau
if [[ "$STATUS" != "down" && "$STATUS" != "connectivity-change" ]]; then
    exit 0
fi

# Vérifier la connectivité internet
check_internet() {
    ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1 || ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1
}

# Si pas de connexion internet, tenter de se reconnecter
if ! check_internet; then
    logger "WiFi fallback: Pas de connexion internet, tentative de reconnexion..."
    nmcli device wifi rescan
    sleep 2
    nmcli --wait 10 device wifi connect || true
fi
DISPATCHER

    chmod +x /etc/NetworkManager/dispatcher.d/99-wifi-fallback
    
else
    # ==========================================
    # Configuration via wpa_supplicant
    # ==========================================
    echo
    echo -e "${GREEN}▶ Configuration via wpa_supplicant...${NC}"
    
    # Backup de la configuration existante
    if [[ -f "$WPA_SUPPLICANT_CONF" ]]; then
        cp "$WPA_SUPPLICANT_CONF" "${WPA_SUPPLICANT_CONF}.backup.$(date +%Y%m%d%H%M%S)"
        echo -e "${YELLOW}ℹ Backup créé: ${WPA_SUPPLICANT_CONF}.backup.*${NC}"
    fi
    
    # Générer les PSK hashés (plus sécurisé que le mot de passe en clair)
    MAIN_PSK=$(wpa_passphrase "$MAIN_SSID" "$MAIN_PASSWORD" | grep -E "^\s+psk=" | cut -d= -f2)
    FALLBACK_PSK=$(wpa_passphrase "$FALLBACK_SSID" "$FALLBACK_PASSWORD" | grep -E "^\s+psk=" | cut -d= -f2)
    
    # Créer la nouvelle configuration
    cat > "$WPA_SUPPLICANT_CONF" << WPACONF
# Configuration WiFi SimpleBooth
# Généré le $(date)
# WiFi principal + Hotspot de secours

ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=FR

# ==========================================
# WiFi PRINCIPAL (priorité haute = 10)
# Se connecte en premier si disponible
# ==========================================
network={
    ssid="$MAIN_SSID"
    psk=$MAIN_PSK
    priority=10
    key_mgmt=WPA-PSK
    id_str="main_wifi"
}

# ==========================================
# WiFi de SECOURS - Hotspot téléphone (priorité = 5)
# Se connecte automatiquement si le WiFi principal est indisponible
# ==========================================
network={
    ssid="$FALLBACK_SSID"
    psk=$FALLBACK_PSK
    priority=5
    key_mgmt=WPA-PSK
    id_str="fallback_hotspot"
}
WPACONF
    
    # Sécuriser les permissions
    chmod 600 "$WPA_SUPPLICANT_CONF"
    
    echo -e "${GREEN}✓ wpa_supplicant.conf configuré${NC}"
    
    # Créer un service de surveillance pour le fallback automatique
    cat > /etc/systemd/system/wifi-fallback.service << 'FALLBACK_SERVICE'
[Unit]
Description=WiFi Fallback Monitor for SimpleBooth
After=network.target wpa_supplicant.service
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/wifi-fallback-monitor.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
FALLBACK_SERVICE

    # Script de surveillance
    cat > /usr/local/bin/wifi-fallback-monitor.sh << 'MONITOR_SCRIPT'
#!/bin/bash
# Surveillance de la connexion WiFi et fallback automatique

PING_TARGET="8.8.8.8"
PING_INTERVAL=30
FAIL_THRESHOLD=3
fail_count=0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
    logger -t wifi-fallback "$1"
}

check_internet() {
    ping -c 1 -W 5 $PING_TARGET >/dev/null 2>&1
    return $?
}

reconnect_wifi() {
    log "Tentative de reconnexion WiFi..."
    wpa_cli -i wlan0 reassociate
    sleep 10
    
    if ! check_internet; then
        log "Scan des réseaux disponibles..."
        wpa_cli -i wlan0 scan
        sleep 5
        wpa_cli -i wlan0 scan_results
        
        # Forcer la reconnexion en sélectionnant le meilleur réseau
        wpa_cli -i wlan0 reconfigure
        sleep 10
    fi
}

log "Démarrage du moniteur WiFi fallback"

while true; do
    if check_internet; then
        fail_count=0
    else
        ((fail_count++))
        log "Échec ping ($fail_count/$FAIL_THRESHOLD)"
        
        if (( fail_count >= FAIL_THRESHOLD )); then
            reconnect_wifi
            fail_count=0
        fi
    fi
    
    sleep $PING_INTERVAL
done
MONITOR_SCRIPT

    chmod +x /usr/local/bin/wifi-fallback-monitor.sh
    
    # Activer et démarrer le service
    systemctl daemon-reload
    systemctl enable wifi-fallback.service
    
    echo -e "${GREEN}✓ Service de surveillance WiFi créé${NC}"
fi

# ==========================================
# Configuration DHCP pour failover rapide
# ==========================================
echo
echo -e "${GREEN}▶ Optimisation des timeouts DHCP...${NC}"

DHCPCD_CONF="/etc/dhcpcd.conf"
if [[ -f "$DHCPCD_CONF" ]]; then
    # Ajouter les options de timeout si elles n'existent pas
    if ! grep -q "# SimpleBooth WiFi Failover" "$DHCPCD_CONF"; then
        cat >> "$DHCPCD_CONF" << 'DHCP_CONFIG'

# SimpleBooth WiFi Failover Configuration
# Timeout rapide pour basculer plus vite sur le WiFi de secours
timeout 15
waitip 10

# Préférer IPv4
noipv6
DHCP_CONFIG
        echo -e "${GREEN}✓ Timeouts DHCP optimisés${NC}"
    fi
fi

# ==========================================
# Résumé final
# ==========================================
echo
echo -e "${GREEN}╭─────────────────────────────────────────────────────────────╮${NC}"
echo -e "${GREEN}│              ✅ Configuration terminée !                    │${NC}"
echo -e "${GREEN}╰─────────────────────────────────────────────────────────────╯${NC}"
echo
echo -e "${YELLOW}📋 Réseaux configurés:${NC}"
echo "   1. $MAIN_SSID (principal, priorité haute)"
echo "   2. $FALLBACK_SSID (secours, priorité basse)"
echo
echo -e "${YELLOW}🔄 Comportement:${NC}"
echo "   • Le Raspberry se connecte au WiFi principal en priorité"
echo "   • Si indisponible, bascule automatiquement sur le hotspot"
echo "   • Retour automatique au WiFi principal quand il revient"
echo
echo -e "${YELLOW}📝 Pour tester:${NC}"
echo "   1. Redémarrer le Raspberry: sudo reboot"
echo "   2. Vérifier la connexion: iwconfig wlan0"
echo "   3. Voir les logs: journalctl -u wifi-fallback -f"
echo
echo -e "${GREEN}Redémarrer maintenant pour appliquer? (o/N):${NC}"
read -r REBOOT
if [[ "$REBOOT" =~ ^[Oo]$ ]]; then
    reboot
fi
