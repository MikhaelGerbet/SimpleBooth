# 📸 WizardPhotoBox Raspberry Pi

> **Application Flask pour WizardPhotoBox tactile avec flux vidéo temps réel, capture instantanée, effets IA, impression photo et intégration Telegram**

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-2.3.3-green.svg)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi%204%2F5-Compatible-red.svg)
![Runware](https://img.shields.io/badge/Runware%20AI-Intégré-purple.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![OpenCV](https://img.shields.io/badge/OpenCV-Support%20USB-brightgreen.svg)
![CUPS](https://img.shields.io/badge/CUPS-Canon%20SELPHY-orange.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## 🎯 Aperçu

Cette application transforme votre Raspberry Pi en un WizardPhotoBox professionnel avec :
- **Flux vidéo temps réel** en MJPEG 1280x720 (16:9)
- **Support multi-caméras** : Pi Camera (v1/v2/v3) ou caméra USB
- **Compatible Raspberry Pi 4 et 5** (détection automatique rpicam-vid/libcamera-vid)
- **Interface tactile optimisée** pour écran 7 pouces
- **Capture photo instantanée** directement depuis le flux vidéo
- **Effets IA** via l'API Runware pour transformer vos photos
- **Diaporama automatique** configurable après période d'inactivité
- **Bot Telegram** pour envoi automatique des photos sur un groupe/canal
- **Impression photo couleur** via Canon SELPHY CP1500 (CUPS)
- **Impression thermique** pour tickets/reçus (ESC/POS)
- **Interface d'administration** complète

## �️ Matériel requis

### Matériel supporté

| Composant | Options supportées |
|-----------|-------------------|
| **Raspberry Pi** | Pi 4, Pi 5 (recommandé) |
| **Caméra** | Pi Camera v1/v2/v3, HQ Camera, Webcam USB |
| **Écran** | Écran tactile 7" DSI (Waveshare recommandé) |
| **Imprimante photo** | Canon SELPHY CP1500 (USB) via CUPS |
| **Imprimante thermique** | Imprimantes ESC/POS série |

### 🛒 Liens d'achat (Affiliation)

Voici une liste de matériel compatible. Les liens sont affiliés et aident à soutenir le projet.

- **Raspberry Pi & Accessoires :**
  - [Raspberry Pi 5](https://amzlink.to/az0ncNNUsGjUH)
  - [Alimentation Raspberry Pi 5](https://amzlink.to/az01ijEmlFqxT)
- **Caméras :**
  - [Pi Camera 3](https://amzlink.to/az0eEXwhnxNvO)
  - [Pi Camera 2.1](https://amzlink.to/az0mgp7Sob1xh)
- **Imprimantes :**
  - [Canon SELPHY CP1500 (Photo couleur)](https://amzlink.to/az0CanonSELPHY)
  - [Imprimante Thermique (Amazon)](https://amzlink.to/az0wTKS9Bfig2)
  - [Imprimante Thermique (AliExpress)](https://s.click.aliexpress.com/e/_oFyCgCI)
- **Écran :**
  - [Ecran Waveshare 7" DSI (Amazon)](https://amzlink.to/az03G4UMruNnc)

## 🚀 Installation

L'installation peut se faire de deux manières : automatiquement via un script (recommandé) ou manuellement.

### Méthode 1 : Installation automatique avec `setup.sh` (Recommandé)

Un script `setup.sh` est fourni pour automatiser l'ensemble du processus sur Raspberry Pi OS.

```bash
# Cloner le repository
git clone https://github.com/MikhaelGerbet/SimpleBooth.git
cd SimpleBooth

# Rendre le script exécutable
chmod +x setup.sh

# Lancer l'installation (avec sudo)
sudo ./setup.sh
```

Le script s'occupe de :
- ✅ Mettre à jour les paquets système
- ✅ Installer les dépendances système (`libcamera-apps`, `python3-opencv`, `chromium`)
- ✅ Configurer l'écran Waveshare 7" DSI (optionnel)
- ✅ Configurer le port série GPIO pour imprimante thermique (optionnel)
- ✅ Créer un environnement virtuel Python `venv`
- ✅ Installer les dépendances Python
- ✅ Configurer le mode kiosk au démarrage
- ✅ Créer le service systemd

### Méthode 2 : Installation manuelle

```bash
# 1. Mettre à jour le système
sudo apt update && sudo apt upgrade -y

# 2. Installer les dépendances système
sudo apt install -y python3 python3-venv python3-pip libcamera-apps python3-opencv

# 3. Cloner et configurer le projet
git clone https://github.com/MikhaelGerbet/SimpleBooth.git
cd SimpleBooth

# 4. Créer et activer l'environnement virtuel
python3 -m venv venv
source venv/bin/activate

# 5. Installer les dépendances Python
pip install -r requirements.txt
```

## 🖨️ Configuration de l'imprimante Canon SELPHY CP1500

### Installation CUPS

```bash
# Installer CUPS et les drivers
sudo apt install -y cups cups-bsd printer-driver-gutenprint

# Ajouter l'utilisateur au groupe lpadmin
sudo usermod -aG lpadmin $USER

# Activer l'interface web CUPS
sudo cupsctl --remote-any

# Redémarrer CUPS
sudo systemctl restart cups
```

### Configuration de l'imprimante

1. **Connectez la SELPHY CP1500** en USB et allumez-la
2. **Vérifiez la détection USB** :
   ```bash
   lsusb | grep -i canon
   # Devrait afficher: Canon, Inc. SELPHY CP1500
   ```

3. **Accédez à l'interface CUPS** : `http://localhost:631` ou `http://<IP_du_Pi>:631`
   - Identifiants : votre utilisateur Linux (`pi`) et son mot de passe

4. **Ajouter l'imprimante** :
   - Administration → Ajouter une imprimante
   - Sélectionnez "Canon SELPHY CP1500" dans les périphériques USB
   - Choisissez le driver **Canon CP-330** (le plus compatible)

5. **Vérifiez l'installation** :
   ```bash
   lpstat -p -d
   ```

### Configuration dans SimpleBooth

1. Accédez à `/admin`
2. Section **Imprimante** :
   - Type : **Photo CUPS (Canon SELPHY)**
   - Imprimante : Sélectionnez votre Canon
   - Format : **Carte postale 4x6"** (10x15cm)
3. Sauvegardez

### Test d'impression

```bash
# Test direct
lp -d Canon_SELPHY_CP1500 /chemin/vers/photo.jpg

# Ou via le script
python3 print_cups.py --image photos/test.jpg --quality high
```

## 📷 Configuration des caméras

### Pi Camera (Raspberry Pi 4 et 5)

L'application détecte automatiquement la version du système :
- **Pi 5 / Bookworm** : utilise `rpicam-vid`
- **Pi 4 / Bullseye** : utilise `libcamera-vid`

Vérifiez que la caméra est détectée :
```bash
# Pi 5
rpicam-hello --list-cameras

# Pi 4
libcamera-hello --list-cameras
```

### Caméra USB

1. Dans l'admin, sélectionnez **"Caméra USB"**
2. Spécifiez l'ID de la caméra (généralement `0`)
3. Vérifiez les permissions :
   ```bash
   sudo usermod -a -G video $USER
   ```

## 🎮 Utilisation

### Démarrage manuel

```bash
cd SimpleBooth
source venv/bin/activate
python3 app.py
```

### Accès à l'interface

| Interface | URL |
|-----------|-----|
| **WizardPhotoBox** | `http://localhost:5000` |
| **Administration** | `http://localhost:5000/admin` |
| **CUPS** | `http://localhost:631` |

### Mode Kiosk (après installation avec setup.sh)

Le WizardPhotoBox démarre automatiquement en mode plein écran au démarrage du Raspberry Pi.

```bash
# Vérifier le statut du service
sudo systemctl status simplebooth-kiosk

# Redémarrer le service
sudo systemctl restart simplebooth-kiosk

# Voir les logs
journalctl -u simplebooth-kiosk -f
```

## 📂 Structure du projet

```
SimpleBooth/
├── app.py                 # Application Flask principale
├── camera_utils.py        # Gestion des caméras (Pi Camera, USB)
├── config_utils.py        # Configuration JSON
├── telegram_utils.py      # Bot Telegram
├── print_cups.py          # Impression CUPS (Canon SELPHY)
├── ScriptPythonPOS.py     # Impression thermique ESC/POS
├── setup.sh               # Script d'installation automatisée
├── requirements.txt       # Dépendances Python
├── config.json            # Configuration (créé au lancement)
├── static/                # Fichiers statiques
│   └── camera-placeholder.svg
├── templates/             # Templates HTML (Jinja2)
│   ├── base.html          # Template de base
│   ├── index.html         # Interface WizardPhotoBox
│   ├── review.html        # Prévisualisation photo
│   └── admin.html         # Administration
├── photos/                # Photos originales (créé automatiquement)
└── effet/                 # Photos avec effets IA (créé automatiquement)
```

## ⚙️ Configuration

La configuration est sauvegardée dans `config.json` :

### Général
| Option | Description | Défaut |
|--------|-------------|--------|
| `footer_text` | Texte en pied de photo | "WizardPhotoBox" |
| `timer_seconds` | Délai avant capture (1-10s) | 3 |

### Caméra
| Option | Description | Défaut |
|--------|-------------|--------|
| `camera_type` | `picamera` ou `usb` | "picamera" |
| `usb_camera_id` | ID de la caméra USB | 0 |

### Imprimante
| Option | Description | Défaut |
|--------|-------------|--------|
| `printer_enabled` | Activer l'impression | true |
| `printer_type` | `cups` ou `thermal` | "cups" |
| `printer_name` | Nom imprimante CUPS | "" (défaut système) |
| `paper_size` | Format papier | "4x6" |

### Diaporama
| Option | Description | Défaut |
|--------|-------------|--------|
| `slideshow_enabled` | Activer le diaporama | false |
| `slideshow_delay` | Délai d'inactivité (10-300s) | 60 |
| `slideshow_source` | Source photos | "photos" |

### Effets IA
| Option | Description | Défaut |
|--------|-------------|--------|
| `effect_enabled` | Activer les effets IA | false |
| `effect_prompt` | Description de l'effet | "Transform..." |
| `effect_steps` | Étapes de génération (1-50) | 5 |
| `runware_api_key` | Clé API Runware | "" |

### Telegram
| Option | Description | Défaut |
|--------|-------------|--------|
| `telegram_enabled` | Activer Telegram | false |
| `telegram_bot_token` | Token du bot | "" |
| `telegram_chat_id` | ID du chat/groupe | "" |
| `telegram_send_type` | Photos à envoyer | "photos" |

## 🤖 Configuration Telegram

1. **Créer un bot** via [@BotFather](https://t.me/BotFather)
2. **Obtenir l'ID du chat** :
   - Chat privé : [@userinfobot](https://t.me/userinfobot)
   - Groupe : [@GroupIDbot](https://t.me/GroupIDbot) (format: `-123456789`)
   - Canal : `@nom_du_canal` ou `-100123456789`
3. **Configurer dans l'admin** SimpleBooth

## 🔧 Dépannage

### Caméra

| Problème | Solution |
|----------|----------|
| Caméra Pi non détectée | Vérifier `rpicam-hello --list-cameras` ou `libcamera-hello --list-cameras` |
| Erreur "libcamera not found" sur Pi 5 | L'application utilise automatiquement `rpicam-vid` |
| Caméra USB non fonctionnelle | `sudo usermod -a -G video $USER` puis déconnexion/reconnexion |

### Imprimante

| Problème | Solution |
|----------|----------|
| SELPHY non détectée | Vérifier `lsusb \| grep -i canon` |
| Erreur CUPS "Forbidden" | `sudo usermod -aG lpadmin $USER` |
| Impression en noir et blanc | Vérifier le driver Canon CP-330 dans CUPS |
| Driver SELPHY CP1500 absent | Utiliser le driver **Canon CP-330** |

### Telegram

| Problème | Solution |
|----------|----------|
| "Chat not found" | Le bot doit être membre du groupe/canal |
| Pas d'envoi sur canal | Le bot doit être administrateur |

### Général

| Problème | Solution |
|----------|----------|
| Erreur Python au démarrage | `source venv/bin/activate` avant de lancer |
| Port série non accessible | `sudo usermod -a -G dialout $USER` |

## 📄 Licence

MIT License - Voir le fichier LICENSE pour plus de détails.

---

**Développé avec ❤️ par Les Frères Poulain**
