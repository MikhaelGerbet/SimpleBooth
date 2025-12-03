#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, flash, Response, abort
import os
import time
import subprocess
import threading
import asyncio
import requests
import logging
import signal
import atexit
import base64
import sys
from datetime import datetime
from werkzeug.utils import secure_filename
from PIL import Image
from runware import Runware, IImageInference
from config_utils import (
    PHOTOS_FOLDER,
    EFFECT_FOLDER,
    OVERLAYS_FOLDER,
    load_config,
    save_config,
    ensure_directories,
)
from camera_utils import UsbCamera, detect_cameras
from telegram_utils import send_to_telegram

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'wizardphotobox_secret_key_2024')

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Initialiser les dossiers nécessaires
ensure_directories()

def check_printer_status():
    """Vérifier l'état de l'imprimante thermique"""
    try:
        # Vérifier si le module escpos est disponible
        try:
            from escpos.printer import Serial
        except ImportError:
            return {
                'status': 'error',
                'message': 'Module escpos manquant. Installez-le avec: pip install python-escpos',
                'paper_status': 'unknown'
            }
        
        # Récupérer la configuration de l'imprimante
        printer_port = config.get('printer_port', '/dev/ttyAMA0')
        printer_baudrate = config.get('printer_baudrate', 9600)
        
        # Vérifier si l'imprimante est activée
        if not config.get('printer_enabled', True):
            return {
                'status': 'disabled',
                'message': 'Imprimante désactivée dans la configuration',
                'paper_status': 'unknown'
            }
        
        # Tenter de se connecter à l'imprimante
        try:
            printer = Serial(printer_port, baudrate=printer_baudrate, timeout=1)
            
            # Vérifier l'état du papier (commande ESC/POS standard)
            printer._raw(b'\x10\x04\x01')  # Commande de statut en temps réel
            
            # Lire la réponse (si disponible)
            # Note: Cette partie peut varier selon le modèle d'imprimante
            
            printer.close()
            
            return {
                'status': 'ok',
                'message': 'Imprimante connectée',
                'paper_status': 'ok',
                'port': printer_port,
                'baudrate': printer_baudrate
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'message': f'Erreur de connexion: {str(e)}',
                'paper_status': 'unknown',
                'port': printer_port,
                'baudrate': printer_baudrate
            }
            
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Erreur lors de la vérification: {str(e)}',
            'paper_status': 'unknown'
        }


# Fonction pour détecter les imprimantes CUPS disponibles
def detect_cups_printers():
    """Détecte les imprimantes CUPS disponibles sur le système"""
    printers = []
    try:
        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.startswith('printer '):
                    # Format: "printer NAME is idle..."
                    parts = line.split()
                    if len(parts) >= 2:
                        printers.append(parts[1])
    except Exception as e:
        logger.info(f"[CUPS] Erreur détection imprimantes: {e}")
    return printers


# Fonction pour détecter les ports série disponibles
def detect_serial_ports():
    """Détecte les ports série disponibles sur le système"""
    available_ports = []
    
    # Détection selon le système d'exploitation
    if sys.platform.startswith('win'):  # Windows
        # Vérifier les ports COM1 à COM20
        import serial.tools.list_ports
        try:
            ports = list(serial.tools.list_ports.comports())
            for port in ports:
                available_ports.append((port.device, f"{port.device} - {port.description}"))
        except ImportError:
            # Si pyserial n'est pas installé, on fait une détection basique
            for i in range(1, 21):
                port = f"COM{i}"
                available_ports.append((port, port))
    
    elif sys.platform.startswith('linux'):  # Linux (Raspberry Pi)
        # Vérifier les ports série courants sur Linux
        common_ports = [
            '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
            '/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyACM2',
            '/dev/ttyS0', '/dev/ttyS1', '/dev/ttyAMA0'
        ]
        
        for port in common_ports:
            if os.path.exists(port):
                available_ports.append((port, port))
    
    # Si aucun port n'est trouvé, ajouter des options par défaut
    if not available_ports:
        if sys.platform.startswith('win'):
            available_ports = [('COM1', 'COM1'), ('COM3', 'COM3')]
        else:
            available_ports = [('/dev/ttyAMA0', '/dev/ttyAMA0'), ('/dev/ttyS0', '/dev/ttyS0')]
    
    return available_ports


# Variables globales
config = load_config()
current_photo = None
original_photo = None  # Photo originale pour régénérer les effets
camera_active = False
camera_process = None
usb_camera = None

@app.route('/')
def index():
    """Page principale avec aperçu vidéo"""
    return render_template('index.html', timer=config['timer_seconds'])

# Variable globale pour stocker la dernière frame MJPEG
last_frame = None
frame_lock = threading.Lock()

@app.route('/capture', methods=['POST'])
def capture_photo():
    """Capturer une photo haute résolution pour impression 15x10cm (300dpi)"""
    global current_photo, original_photo, last_frame
    
    try:
        # Récupérer le style de la requête
        data = request.get_json() or {}
        photo_style = data.get('style', 'color')
        
        # Générer un nom de fichier unique
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'photo_{timestamp}.jpg'
        filepath = os.path.join(PHOTOS_FOLDER, filename)
        
        camera_type = config.get('camera_type', 'picamera')
        
        if camera_type == 'usb':
            # Pour caméra USB, utiliser la frame du flux
            with frame_lock:
                if last_frame is not None:
                    with open(filepath, 'wb') as f:
                        f.write(last_frame)
                    logger.info(f"Photo USB capturée: {filename}")
                else:
                    return jsonify({'success': False, 'error': 'Aucune frame disponible'})
        else:
            # Pour Pi Camera: capture haute résolution avec rpicam-still/libcamera-still
            import shutil
            
            if shutil.which('rpicam-still'):
                still_cmd = 'rpicam-still'
            elif shutil.which('libcamera-still'):
                still_cmd = 'libcamera-still'
            else:
                # Fallback: utiliser la frame du flux
                with frame_lock:
                    if last_frame is not None:
                        with open(filepath, 'wb') as f:
                            f.write(last_frame)
                        logger.info(f"Photo (fallback flux) capturée: {filename}")
                    else:
                        return jsonify({'success': False, 'error': 'Aucune frame disponible'})
                current_photo = filename
                original_photo = filename
                return jsonify({'success': True, 'filename': filename})
            
            # Capture haute résolution pour impression Canon SELPHY CP1500
            # Format carte postale 10x15cm (100x148mm) @ 300 DPI
            # Dimensions exactes: 1182 x 1748 pixels
            # On capture en plus haute résolution pour qualité, ratio 148:100 = 1.48
            # 2220 x 1500 serait exact, mais on prend un peu plus large pour crop ultérieur
            cmd = [
                still_cmd,
                '--output', filepath,
                '--width', '2244',      # 1748 * 1.284 (marge de qualité)
                '--height', '1518',     # 1182 * 1.284 (même ratio 1.478)
                '--quality', '95',      # Qualité JPEG élevée
                '--immediate',          # Capture immédiate sans délai
                '--nopreview',          # Pas d'aperçu
                '--timeout', '1'        # Timeout minimal
            ]
            
            logger.info(f"[CAPTURE] Commande: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            
            if result.returncode != 0:
                error_msg = result.stderr.decode() if result.stderr else "Erreur inconnue"
                logger.error(f"[CAPTURE] Erreur: {error_msg}")
                # Fallback sur le flux si la capture échoue
                with frame_lock:
                    if last_frame is not None:
                        with open(filepath, 'wb') as f:
                            f.write(last_frame)
                        logger.info(f"Photo (fallback après erreur) capturée: {filename}")
                    else:
                        return jsonify({'success': False, 'error': f'Erreur capture: {error_msg}'})
            else:
                logger.info(f"[CAPTURE] Photo haute résolution capturée: {filename} (2400x1600)")
        
        # Appliquer le style N&B si sélectionné
        if photo_style == 'bw':
            try:
                img = Image.open(filepath)
                img_bw = img.convert('L').convert('RGB')  # Convertir en niveaux de gris puis RGB
                img_bw.save(filepath, 'JPEG', quality=95)
                logger.info(f"[CAPTURE] Style N&B appliqué à {filename}")
            except Exception as e:
                logger.error(f"[CAPTURE] Erreur application N&B: {e}")
        
        current_photo = filename
        original_photo = filename
        
        # Envoyer sur Telegram si activé
        send_type = config.get('telegram_send_type', 'photos')
        if send_type in ['photos', 'both']:
            threading.Thread(target=send_to_telegram, args=(filepath, config, "photo")).start()
        
        return jsonify({'success': True, 'filename': filename})
            
    except subprocess.TimeoutExpired:
        logger.error("[CAPTURE] Timeout lors de la capture")
        return jsonify({'success': False, 'error': 'Timeout de capture'})
    except Exception as e:
        logger.info(f"Erreur lors de la capture: {e}")
        return jsonify({'success': False, 'error': f'Erreur de capture: {str(e)}'})

@app.route('/review')
def review_photo():
    """Page de révision de la photo"""
    if not current_photo:
        return redirect(url_for('index'))
    return render_template('review.html', photo=current_photo, config=config)

@app.route('/print_photo', methods=['POST'])
def print_photo():
    """Imprimer la photo actuelle"""
    global current_photo
    
    if not current_photo:
        return jsonify({'success': False, 'error': 'Aucune photo à imprimer'})
    
    try:
        # Vérifier si l'imprimante est activée
        if not config.get('printer_enabled', True):
            return jsonify({'success': False, 'error': 'Imprimante désactivée dans la configuration'})
        
        # Chercher la photo dans le bon dossier
        photo_path = None
        if os.path.exists(os.path.join(PHOTOS_FOLDER, current_photo)):
            photo_path = os.path.join(PHOTOS_FOLDER, current_photo)
        elif os.path.exists(os.path.join(EFFECT_FOLDER, current_photo)):
            photo_path = os.path.join(EFFECT_FOLDER, current_photo)
        else:
            return jsonify({'success': False, 'error': 'Photo introuvable'})
        
        # Déterminer le type d'imprimante
        printer_type = config.get('printer_type', 'thermal')
        
        if printer_type == 'cups':
            # Impression via CUPS (Canon SELPHY, etc.)
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'print_cups.py')
            if not os.path.exists(script_path):
                return jsonify({'success': False, 'error': 'Script d\'impression CUPS introuvable (print_cups.py)'})
            
            cmd = ['python3', script_path, '--image', photo_path, '--quality', 'high']
            
            # Ajouter le nom de l'imprimante si configuré
            printer_name = config.get('printer_name', '')
            if printer_name:
                cmd.extend(['--printer', printer_name])
            
            # Ajouter le format papier si configuré
            paper_size = config.get('paper_size', '4x6')
            cmd.extend(['--paper-size', paper_size])
            
            # Exécuter l'impression
            logger.info(f"[PRINT] Commande CUPS: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
            
            if result.returncode == 0:
                return jsonify({'success': True, 'message': 'Photo envoyée à l\'imprimante!'})
            else:
                error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                return jsonify({'success': False, 'error': f'Erreur d\'impression: {error_msg}'})
        
        else:
            # Impression thermique ESC/POS (imprimante ticket)
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ScriptPythonPOS.py')
            if not os.path.exists(script_path):
                return jsonify({'success': False, 'error': 'Script d\'impression introuvable (ScriptPythonPOS.py)'})
            
            # Construire la commande d'impression avec les paramètres thermiques
            cmd = ['python3', script_path, '--image', photo_path]
            
            # Ajouter les paramètres de port et baudrate
            printer_port = config.get('printer_port', '/dev/ttyAMA0')
            printer_baudrate = config.get('printer_baudrate', 9600)
            cmd.extend(['--port', printer_port, '--baudrate', str(printer_baudrate)])
            
            # Ajouter le texte de pied de page si configuré
            footer_text = config.get('footer_text', '')
            if footer_text:
                cmd.extend(['--text', footer_text])
            
            # Ajouter l'option haute résolution selon la configuration
            print_resolution = config.get('print_resolution', 384)
            if print_resolution > 384:
                cmd.append('--hd')
            
            # Exécuter l'impression
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
            
            if result.returncode == 0:
                return jsonify({'success': True, 'message': 'Photo imprimée avec succès!'})
            elif result.returncode == 2:
                # Code d'erreur spécifique pour manque de papier
                return jsonify({'success': False, 'error': 'Plus de papier dans l\'imprimante', 'error_type': 'no_paper'})
            else:
                error_msg = result.stderr.strip() if result.stderr else 'Erreur inconnue'
                if 'ModuleNotFoundError' in error_msg and 'escpos' in error_msg:
                    return jsonify({'success': False, 'error': 'Module escpos manquant. Installez-le avec: pip install python-escpos'})
                else:
                    return jsonify({'success': False, 'error': f'Erreur d\'impression: {error_msg}'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete_current', methods=['POST'])
def delete_current_photo():
    """Supprimer la photo actuelle (depuis photos ou effet)"""
    global current_photo, original_photo
    
    if current_photo:
        try:
            # Chercher la photo dans le bon dossier
            photo_path = None
            if os.path.exists(os.path.join(PHOTOS_FOLDER, current_photo)):
                photo_path = os.path.join(PHOTOS_FOLDER, current_photo)
            elif os.path.exists(os.path.join(EFFECT_FOLDER, current_photo)):
                photo_path = os.path.join(EFFECT_FOLDER, current_photo)
            
            if photo_path and os.path.exists(photo_path):
                os.remove(photo_path)
                current_photo = None
                original_photo = None  # Réinitialiser aussi l'original
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Photo introuvable'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    
    return jsonify({'success': False, 'error': 'Aucune photo à supprimer'})

@app.route('/apply_effect', methods=['POST'])
def apply_effect():
    """Appliquer un effet IA à la photo actuelle via Runware"""
    global current_photo, original_photo
    
    # Utiliser la photo originale pour permettre la régénération
    photo_to_process = original_photo if original_photo else current_photo
    
    if not photo_to_process:
        return jsonify({'success': False, 'error': 'Aucune photo à traiter'})
    
    if not config.get('effect_enabled', False):
        return jsonify({'success': False, 'error': 'Les effets sont désactivés'})
    
    if not config.get('runware_api_key'):
        return jsonify({'success': False, 'error': 'Clé API Runware manquante'})
    
    try:
        # Toujours utiliser la photo originale (dans PHOTOS_FOLDER)
        photo_path = os.path.join(PHOTOS_FOLDER, photo_to_process)
        
        if not os.path.exists(photo_path):
            return jsonify({'success': False, 'error': 'Photo originale introuvable'})
        
        logger.info(f"[IA] Régénération depuis la photo originale: {photo_to_process}")
        result = asyncio.run(apply_effect_runware(photo_path))
        return result
            
    except Exception as e:
        logger.info(f"Erreur lors de l'application de l'effet: {e}")
        return jsonify({'success': False, 'error': f'Erreur IA: {str(e)}'})


async def apply_effect_runware(photo_path):
    """Fonction asynchrone pour appliquer l'effet IA via Runware"""
    global current_photo
    
    try:
        logger.info("[DEBUG IA] Début de l'application de l'effet IA")
        logger.info(f"[DEBUG IA] Photo source: {photo_path}")
        logger.info(f"[DEBUG IA] Clé API configurée: {'Oui' if config.get('runware_api_key') else 'Non'}")
        logger.info(f"[DEBUG IA] Prompt: {config.get('effect_prompt', 'Transform this photo into a beautiful ghibli style')}")
        
        # Initialiser Runware
        logger.info("[DEBUG IA] Initialisation de Runware...")
        runware = Runware(api_key=config['runware_api_key'])
        logger.info("[DEBUG IA] Connexion à Runware...")
        await runware.connect()
        logger.info("[DEBUG IA] Connexion établie avec succès")
        
        # Lire et encoder l'image en base64
        logger.info("[DEBUG IA] Lecture et encodage de l'image...")
        with open(photo_path, 'rb') as img_file:
            img_data = img_file.read()
            img_base64 = base64.b64encode(img_data).decode('utf-8')
        logger.info(f"[DEBUG IA] Image encodée: {len(img_base64)} caractères base64")
        
        # Préparer la requête d'inférence avec referenceImages (requis pour ce modèle)
        logger.info("[DEBUG IA] Préparation de la requête d'inférence avec referenceImages...")
        request = IImageInference(
            positivePrompt=config.get('effect_prompt', 'Transforme cette image en illustration de style Studio Ghibli'),
            referenceImages=[f"data:image/jpeg;base64,{img_base64}"],
            model="runware:106@1",
            height=752, 
            width=1392,  
            steps=config.get('effect_steps', 5),
            CFGScale=2.5,
            numberResults=1
        )
        logger.info("[DEBUG IA] Requête préparée avec les paramètres de base:")
        logger.info(f"[DEBUG IA]   - Modèle: runware:106@1")
        logger.info(f"[DEBUG IA]   - Dimensions: 1392x752")
        logger.info(f"[DEBUG IA]   - Étapes: {config.get('effect_steps', 5)}")
        logger.info(f"[DEBUG IA]   - CFG Scale: 2.5")
        logger.info(f"[DEBUG IA]   - Nombre de résultats: 1")
        
        # Appliquer l'effet
        logger.info("[DEBUG IA] Envoi de la requête à l'API Runware...")
        # La méthode correcte est imageInference
        images = await runware.imageInference(requestImage=request)
        logger.info(f"[DEBUG IA] Réponse reçue: {len(images) if images else 0} image(s) générée(s)")
        
        if images and len(images) > 0:
            # Télécharger l'image transformée
            logger.info(f"[DEBUG IA] URL de l'image générée: {images[0].imageURL}")
            logger.info("[DEBUG IA] Téléchargement de l'image transformée...")
            import requests
            response = requests.get(images[0].imageURL)
            logger.info(f"[DEBUG IA] Statut de téléchargement: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"[DEBUG IA] Taille de l'image téléchargée: {len(response.content)} bytes")
                
                # S'assurer que le dossier effet existe
                logger.info(f"[DEBUG IA] Vérification du dossier effet: {EFFECT_FOLDER}")
                os.makedirs(EFFECT_FOLDER, exist_ok=True)
                logger.info(f"[DEBUG IA] Dossier effet existe: {os.path.exists(EFFECT_FOLDER)}")
                
                # Créer un nouveau nom de fichier pour l'image avec effet
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                effect_filename = f'effect_{timestamp}.jpg'
                effect_path = os.path.join(EFFECT_FOLDER, effect_filename)
                logger.info(f"[DEBUG IA] Sauvegarde vers: {effect_path}")
                
                # Sauvegarder l'image avec effet
                with open(effect_path, 'wb') as f:
                    f.write(response.content)
                logger.info("[DEBUG IA] Image sauvegardée avec succès")
                
                # Appliquer l'overlay si activé
                if config.get('overlay_enabled', False) and config.get('current_overlay', ''):
                    logger.info("[DEBUG IA] Application de l'overlay sur l'effet...")
                    apply_overlay(effect_path)
                
                # Mettre à jour la photo actuelle
                current_photo = effect_filename
                logger.info(f"[DEBUG IA] Photo actuelle mise à jour: {current_photo}")
                logger.info("[DEBUG IA] Effet appliqué avec succès!")
                
                # Envoyer sur Telegram si activé
                send_type = config.get('telegram_send_type', 'photos')
                if send_type in ['effet', 'both']:
                    threading.Thread(target=send_to_telegram, args=(effect_path, config, "effet")).start()
                
                return jsonify({
                    'success': True, 
                    'message': 'Effet appliqué avec succès!',
                    'new_filename': effect_filename
                })
            else:
                logger.info(f"[DEBUG IA] ERREUR: Échec du téléchargement (code {response.status_code})")
                return jsonify({'success': False, 'error': 'Erreur lors du téléchargement de l\'image transformée'})
        else:
            logger.info("[DEBUG IA] ERREUR: Aucune image générée par l'IA")
            return jsonify({'success': False, 'error': 'Aucune image générée par l\'IA'})
            
    except Exception as e:
        logger.info(f"Erreur lors de l'application de l'effet: {e}")
        return jsonify({'success': False, 'error': f'Erreur IA: {str(e)}'})


# ============================================
# GESTION DES OVERLAYS
# ============================================

# Dimensions exactes pour Canon SELPHY CP1500 @ 300 DPI
# Format carte postale 10x15cm (100x148mm)
SELPHY_WIDTH = 1748   # 148mm @ 300 DPI
SELPHY_HEIGHT = 1182  # 100mm @ 300 DPI
SELPHY_RATIO = SELPHY_WIDTH / SELPHY_HEIGHT  # ~1.479

def apply_overlay(photo_path, output_path=None):
    """
    Appliquer l'overlay actuel sur une photo.
    
    L'overlay ET la photo sont redimensionnés aux dimensions exactes 
    de la Canon SELPHY CP1500 (1748x1182 pixels) pour garantir 
    un alignement parfait à l'impression.
    
    Args:
        photo_path: Chemin de la photo source
        output_path: Chemin de sortie (si None, écrase la photo source)
    
    Returns:
        Chemin de la photo avec overlay, ou None si échec
    """
    if not config.get('overlay_enabled', False):
        return photo_path
    
    current_overlay = config.get('current_overlay', '')
    if not current_overlay:
        return photo_path
    
    overlay_path = os.path.join(OVERLAYS_FOLDER, current_overlay)
    if not os.path.exists(overlay_path):
        logger.warning(f"[OVERLAY] Overlay introuvable: {overlay_path}")
        return photo_path
    
    try:
        # Ouvrir la photo et l'overlay
        photo = Image.open(photo_path).convert('RGBA')
        overlay = Image.open(overlay_path).convert('RGBA')
        
        logger.info(f"[OVERLAY] Photo originale: {photo.size}, Overlay: {overlay.size}")
        
        # Redimensionner la photo aux dimensions SELPHY avec crop centré
        photo_ratio = photo.width / photo.height
        
        if photo_ratio > SELPHY_RATIO:
            # Photo plus large → on ajuste sur la hauteur et on crop les côtés
            new_height = SELPHY_HEIGHT
            new_width = int(SELPHY_HEIGHT * photo_ratio)
        else:
            # Photo plus haute → on ajuste sur la largeur et on crop haut/bas
            new_width = SELPHY_WIDTH
            new_height = int(SELPHY_WIDTH / photo_ratio)
        
        photo_resized = photo.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Crop centré aux dimensions exactes SELPHY
        left = (new_width - SELPHY_WIDTH) // 2
        top = (new_height - SELPHY_HEIGHT) // 2
        photo_cropped = photo_resized.crop((left, top, left + SELPHY_WIDTH, top + SELPHY_HEIGHT))
        
        logger.info(f"[OVERLAY] Photo après crop: {photo_cropped.size}")
        
        # Redimensionner l'overlay aux dimensions SELPHY exactes
        overlay_resized = overlay.resize((SELPHY_WIDTH, SELPHY_HEIGHT), Image.Resampling.LANCZOS)
        
        logger.info(f"[OVERLAY] Overlay redimensionné: {overlay_resized.size}")
        
        # Superposer l'overlay sur la photo
        photo_with_overlay = Image.alpha_composite(photo_cropped, overlay_resized)
        
        # Convertir en RGB pour sauvegarder en JPEG
        photo_with_overlay_rgb = photo_with_overlay.convert('RGB')
        
        # Déterminer le chemin de sortie
        if output_path is None:
            output_path = photo_path
        
        # Sauvegarder avec DPI correct pour impression
        photo_with_overlay_rgb.save(output_path, 'JPEG', quality=95, dpi=(300, 300))
        logger.info(f"[OVERLAY] Overlay appliqué: {current_overlay} → {SELPHY_WIDTH}x{SELPHY_HEIGHT}px")
        
        return output_path
        
    except Exception as e:
        logger.error(f"[OVERLAY] Erreur lors de l'application de l'overlay: {e}")
        import traceback
        traceback.print_exc()
        return photo_path


@app.route('/api/overlays')
def list_overlays():
    """Lister tous les overlays disponibles"""
    overlays = []
    
    if os.path.exists(OVERLAYS_FOLDER):
        for filename in os.listdir(OVERLAYS_FOLDER):
            if filename.lower().endswith(('.png', '.webp')):
                overlays.append({
                    'filename': filename,
                    'url': f'/overlays/{filename}'
                })
    
    overlays.sort(key=lambda x: x['filename'])
    
    return jsonify({
        'overlays': overlays,
        'current': config.get('current_overlay', ''),
        'enabled': config.get('overlay_enabled', False)
    })


@app.route('/api/overlay/upload', methods=['POST'])
def upload_overlay():
    """Uploader un nouvel overlay (PNG transparent)"""
    if 'overlay' not in request.files:
        return jsonify({'success': False, 'error': 'Aucun fichier fourni'})
    
    file = request.files['overlay']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'})
    
    # Vérifier l'extension
    allowed_extensions = {'.png', '.webp'}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_extensions:
        return jsonify({'success': False, 'error': 'Format non supporté. Utilisez PNG ou WebP.'})
    
    try:
        # Sécuriser le nom de fichier
        filename = secure_filename(file.filename)
        
        # S'assurer que le dossier existe
        os.makedirs(OVERLAYS_FOLDER, exist_ok=True)
        
        # Sauvegarder le fichier
        filepath = os.path.join(OVERLAYS_FOLDER, filename)
        file.save(filepath)
        
        # Vérifier que c'est bien une image avec transparence
        try:
            img = Image.open(filepath)
            if img.mode not in ('RGBA', 'LA', 'PA'):
                # Avertissement mais on garde le fichier
                logger.warning(f"[OVERLAY] L'image {filename} n'a pas de canal alpha")
        except Exception as e:
            # Si ce n'est pas une image valide, supprimer
            os.remove(filepath)
            return jsonify({'success': False, 'error': f'Image invalide: {str(e)}'})
        
        logger.info(f"[OVERLAY] Overlay uploadé: {filename}")
        
        return jsonify({
            'success': True,
            'filename': filename,
            'url': f'/overlays/{filename}'
        })
        
    except Exception as e:
        logger.error(f"[OVERLAY] Erreur lors de l'upload: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/overlay/select', methods=['POST'])
def select_overlay():
    """Sélectionner un overlay comme overlay actif"""
    data = request.get_json()
    
    if not data:
        return jsonify({'success': False, 'error': 'Données JSON manquantes'})
    
    filename = data.get('filename', '')
    enabled = data.get('enabled', True)
    
    # Vérifier que l'overlay existe (sauf si on désactive)
    if filename and enabled:
        overlay_path = os.path.join(OVERLAYS_FOLDER, filename)
        if not os.path.exists(overlay_path):
            return jsonify({'success': False, 'error': 'Overlay introuvable'})
    
    # Mettre à jour la configuration
    config['current_overlay'] = filename
    config['overlay_enabled'] = enabled
    save_config(config)
    
    logger.info(f"[OVERLAY] Overlay sélectionné: {filename}, activé: {enabled}")
    
    return jsonify({
        'success': True,
        'current': filename,
        'enabled': enabled
    })


@app.route('/api/overlay/delete/<filename>', methods=['DELETE'])
def delete_overlay(filename):
    """Supprimer un overlay"""
    try:
        # Sécuriser le nom de fichier
        filename = secure_filename(filename)
        filepath = os.path.join(OVERLAYS_FOLDER, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': 'Overlay introuvable'})
        
        os.remove(filepath)
        
        # Si c'était l'overlay actif, le désactiver
        if config.get('current_overlay') == filename:
            config['current_overlay'] = ''
            config['overlay_enabled'] = False
            save_config(config)
        
        logger.info(f"[OVERLAY] Overlay supprimé: {filename}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"[OVERLAY] Erreur lors de la suppression: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/overlays/<filename>')
def serve_overlay(filename):
    """Servir un fichier overlay"""
    return send_from_directory(OVERLAYS_FOLDER, filename)


@app.route('/api/overlay/current')
def get_current_overlay():
    """Récupérer l'overlay actuel (pour le preview)"""
    if not config.get('overlay_enabled', False):
        return jsonify({'enabled': False, 'overlay': None})
    
    current = config.get('current_overlay', '')
    if not current:
        return jsonify({'enabled': False, 'overlay': None})
    
    return jsonify({
        'enabled': True,
        'overlay': current,
        'url': f'/overlays/{current}'
    })


@app.route('/api/overlay/apply', methods=['POST'])
def apply_overlay_to_current_photo():
    """
    Appliquer l'overlay à la photo actuelle (sans effet IA).
    Crée une copie avec overlay dans le dossier effet.
    """
    global current_photo
    
    if not current_photo:
        return jsonify({'success': False, 'error': 'Aucune photo à traiter'})
    
    if not config.get('overlay_enabled', False):
        return jsonify({'success': False, 'error': 'Overlay désactivé'})
    
    if not config.get('current_overlay', ''):
        return jsonify({'success': False, 'error': 'Aucun overlay sélectionné'})
    
    try:
        # Chercher la photo source
        source_path = None
        if os.path.exists(os.path.join(PHOTOS_FOLDER, current_photo)):
            source_path = os.path.join(PHOTOS_FOLDER, current_photo)
        elif os.path.exists(os.path.join(EFFECT_FOLDER, current_photo)):
            source_path = os.path.join(EFFECT_FOLDER, current_photo)
        
        if not source_path:
            return jsonify({'success': False, 'error': 'Photo introuvable'})
        
        # Créer un nouveau fichier avec overlay
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        overlay_filename = f'overlay_{timestamp}.jpg'
        overlay_path = os.path.join(EFFECT_FOLDER, overlay_filename)
        
        # S'assurer que le dossier existe
        os.makedirs(EFFECT_FOLDER, exist_ok=True)
        
        # Appliquer l'overlay
        result = apply_overlay(source_path, overlay_path)
        
        if result and os.path.exists(overlay_path):
            current_photo = overlay_filename
            logger.info(f"[OVERLAY] Photo avec overlay créée: {overlay_filename}")
            
            return jsonify({
                'success': True,
                'message': 'Overlay appliqué avec succès!',
                'new_filename': overlay_filename
            })
        else:
            return jsonify({'success': False, 'error': 'Échec de l\'application de l\'overlay'})
            
    except Exception as e:
        logger.error(f"[OVERLAY] Erreur: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ============================================
# QR CODE TELEGRAM
# ============================================

QRCODES_FOLDER = 'static/qrcodes'

@app.route('/api/telegram/qrcode')
def get_telegram_qrcode():
    """
    Récupérer le QR Code du groupe Telegram.
    Utilise un cache basé sur le chat_id pour éviter les appels API répétés.
    """
    if not config.get('telegram_enabled', False):
        return jsonify({'success': False, 'error': 'Telegram non configuré'})
    
    chat_id = config.get('telegram_chat_id', '')
    bot_token = config.get('telegram_bot_token', '')
    
    if not chat_id or not bot_token:
        return jsonify({'success': False, 'error': 'Configuration Telegram incomplète'})
    
    # Nettoyer le chat_id pour le nom de fichier
    safe_chat_id = chat_id.replace('@', '').replace('-', '_').replace('/', '_')
    qrcode_filename = f'qrcode_{safe_chat_id}.png'
    qrcode_path = os.path.join(QRCODES_FOLDER, qrcode_filename)
    
    # Vérifier si le QR Code existe déjà en cache
    if os.path.exists(qrcode_path):
        logger.info(f"[QRCODE] Utilisation du cache: {qrcode_filename}")
        return jsonify({
            'success': True,
            'url': f'/static/qrcodes/{qrcode_filename}'
        })
    
    # Le QR Code n'existe pas, on doit le générer
    try:
        # S'assurer que le dossier existe
        os.makedirs(QRCODES_FOLDER, exist_ok=True)
        
        invite_link = None
        
        # Si le chat_id commence par @, c'est un username public
        if chat_id.startswith('@'):
            invite_link = f'https://t.me/{chat_id[1:]}'
            logger.info(f"[QRCODE] Canal public détecté: {invite_link}")
        else:
            # Sinon, on essaie de récupérer le lien d'invitation via l'API
            try:
                api_url = f'https://api.telegram.org/bot{bot_token}/exportChatInviteLink'
                response = requests.post(api_url, json={'chat_id': chat_id}, timeout=10)
                data = response.json()
                
                if data.get('ok'):
                    invite_link = data.get('result')
                    logger.info(f"[QRCODE] Lien d'invitation récupéré: {invite_link}")
                else:
                    logger.warning(f"[QRCODE] Erreur API Telegram: {data.get('description')}")
                    # Essayer avec createChatInviteLink (pour les groupes/canaux où exportChatInviteLink ne fonctionne pas)
                    api_url = f'https://api.telegram.org/bot{bot_token}/createChatInviteLink'
                    response = requests.post(api_url, json={'chat_id': chat_id}, timeout=10)
                    data = response.json()
                    if data.get('ok'):
                        invite_link = data.get('result', {}).get('invite_link')
                        logger.info(f"[QRCODE] Lien créé via createChatInviteLink: {invite_link}")
            except Exception as e:
                logger.error(f"[QRCODE] Erreur appel API Telegram: {e}")
        
        if not invite_link:
            return jsonify({'success': False, 'error': 'Impossible de récupérer le lien Telegram'})
        
        # Générer le QR Code
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data(invite_link)
        qr.make(fit=True)
        
        qr_image = qr.make_image(fill_color="black", back_color="white")
        qr_image.save(qrcode_path)
        
        logger.info(f"[QRCODE] QR Code généré et sauvegardé: {qrcode_filename}")
        
        return jsonify({
            'success': True,
            'url': f'/static/qrcodes/{qrcode_filename}'
        })
        
    except ImportError:
        logger.error("[QRCODE] Module qrcode non installé. Installez-le avec: pip install qrcode[pil]")
        return jsonify({'success': False, 'error': 'Module qrcode non installé'})
    except Exception as e:
        logger.error(f"[QRCODE] Erreur: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin')
def admin():
    # Vérifier si le dossier photos existe
    if not os.path.exists(PHOTOS_FOLDER):
        os.makedirs(PHOTOS_FOLDER)
    
    # Vérifier si le dossier effet existe
    if not os.path.exists(EFFECT_FOLDER):
        os.makedirs(EFFECT_FOLDER)
    
    # Récupérer la liste des photos avec leurs métadonnées
    photos = []
    
    # Récupérer les photos du dossier PHOTOS_FOLDER
    if os.path.exists(PHOTOS_FOLDER):
        for filename in os.listdir(PHOTOS_FOLDER):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                file_path = os.path.join(PHOTOS_FOLDER, filename)
                file_size_kb = os.path.getsize(file_path) / 1024  # Taille en KB
                file_date = datetime.fromtimestamp(os.path.getmtime(file_path))
                
                photos.append({
                    'filename': filename,
                    'size_kb': file_size_kb,
                    'date': file_date.strftime("%d/%m/%Y %H:%M"),
                    'type': 'photo',
                    'folder': PHOTOS_FOLDER
                })
    
    # Récupérer les photos du dossier EFFECT_FOLDER
    if os.path.exists(EFFECT_FOLDER):
        for filename in os.listdir(EFFECT_FOLDER):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                file_path = os.path.join(EFFECT_FOLDER, filename)
                file_size_kb = os.path.getsize(file_path) / 1024  # Taille en KB
                file_date = datetime.fromtimestamp(os.path.getmtime(file_path))
                
                photos.append({
                    'filename': filename,
                    'size_kb': file_size_kb,
                    'date': file_date.strftime("%d/%m/%Y %H:%M"),
                    'type': 'effet',
                    'folder': EFFECT_FOLDER
                })
    
    # Trier les photos par date (plus récentes en premier)
    photos.sort(key=lambda x: datetime.strptime(x['date'], "%d/%m/%Y %H:%M"), reverse=True)
    
    # Compter les photos de chaque type
    photo_count = sum(1 for p in photos if p['type'] == 'photo')
    effect_count = sum(1 for p in photos if p['type'] == 'effet')
    
    # Détecter les caméras USB disponibles
    available_cameras = detect_cameras()
    
    # Détecter les ports série disponibles
    available_serial_ports = detect_serial_ports()
    
    # Détecter les imprimantes CUPS disponibles
    available_cups_printers = detect_cups_printers()
    
    # Charger la configuration
    config = load_config()
    
    return render_template('admin.html', 
                           config=config, 
                           photos=photos,
                           photo_count=photo_count,
                           effect_count=effect_count,
                           available_cameras=available_cameras,
                           available_serial_ports=available_serial_ports,
                           available_cups_printers=available_cups_printers,
                           show_toast=request.args.get('show_toast', False))

@app.route('/admin/save', methods=['POST'])
def save_admin_config():
    """Sauvegarder la configuration admin"""
    global config
    
    try:
        config['footer_text'] = request.form.get('footer_text', '')
        
        # Gestion sécurisée des champs numériques
        timer_seconds = request.form.get('timer_seconds', '3').strip()
        config['timer_seconds'] = int(timer_seconds) if timer_seconds else 3
        
        config['high_density'] = 'high_density' in request.form
        config['slideshow_enabled'] = 'slideshow_enabled' in request.form
        
        slideshow_delay = request.form.get('slideshow_delay', '60').strip()
        config['slideshow_delay'] = int(slideshow_delay) if slideshow_delay else 60
        
        slideshow_photo_duration = request.form.get('slideshow_photo_duration', '5').strip()
        config['slideshow_photo_duration'] = int(slideshow_photo_duration) if slideshow_photo_duration else 5
        
        config['slideshow_source'] = request.form.get('slideshow_source', 'photos')
        config['effect_enabled'] = 'effect_enabled' in request.form
        config['effect_prompt'] = request.form.get('effect_prompt', '')
        
        effect_steps = request.form.get('effect_steps', '5').strip()
        config['effect_steps'] = int(effect_steps) if effect_steps else 5
        
        config['runware_api_key'] = request.form.get('runware_api_key', '')
        
        # Configuration overlay
        config['overlay_enabled'] = 'overlay_enabled' in request.form
        
        config['telegram_enabled'] = 'telegram_enabled' in request.form
        config['telegram_bot_token'] = request.form.get('telegram_bot_token', '')
        config['telegram_chat_id'] = request.form.get('telegram_chat_id', '')
        config['telegram_send_type'] = request.form.get('telegram_send_type', 'photos')
        
        # Configuration de la caméra
        config['camera_type'] = request.form.get('camera_type', 'picamera')
        
        # Récupérer l'ID de la caméra USB sélectionnée
        selected_camera = request.form.get('usb_camera_select', '0')
        # L'ID est stocké comme premier caractère de la valeur
        try:
            config['usb_camera_id'] = int(selected_camera)
        except ValueError:
            config['usb_camera_id'] = 0
        
        # Configuration de l'imprimante
        config['printer_enabled'] = 'printer_enabled' in request.form
        config['printer_type'] = request.form.get('printer_type', 'cups')
        config['printer_name'] = request.form.get('printer_name', '')
        config['printer_port'] = request.form.get('printer_port', '/dev/ttyAMA0')
        
        printer_baudrate = request.form.get('printer_baudrate', '9600').strip()
        try:
            config['printer_baudrate'] = int(printer_baudrate)
        except ValueError:
            config['printer_baudrate'] = 9600
        
        # Format papier pour CUPS
        config['paper_size'] = request.form.get('paper_size', '4x6')
        
        print_resolution = request.form.get('print_resolution', '384').strip()
        try:
            config['print_resolution'] = int(print_resolution)
        except ValueError:
            config['print_resolution'] = 384
        
        save_config(config)
        flash('Configuration sauvegardée avec succès!', 'success')
        
    except Exception as e:
        flash(f'Erreur lors de la sauvegarde: {str(e)}', 'error')
    
    return redirect(url_for('admin'))

@app.route('/admin/delete_photos', methods=['POST'])
def delete_all_photos():
    """Supprimer toutes les photos (normales et avec effet)"""
    try:
        deleted_count = 0
        
        # Supprimer les photos normales
        if os.path.exists(PHOTOS_FOLDER):
            for filename in os.listdir(PHOTOS_FOLDER):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                    os.remove(os.path.join(PHOTOS_FOLDER, filename))
                    deleted_count += 1
        
        # Supprimer les photos avec effet
        if os.path.exists(EFFECT_FOLDER):
            for filename in os.listdir(EFFECT_FOLDER):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                    os.remove(os.path.join(EFFECT_FOLDER, filename))
                    deleted_count += 1
        
        flash(f'{deleted_count} photo(s) supprimée(s) avec succès!', 'success')
    except Exception as e:
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')
    
    return redirect(url_for('admin'))

@app.route('/admin/download_photo/<filename>')
def download_photo(filename):
    """Télécharger une photo spécifique"""
    try:
        # Chercher la photo dans les deux dossiers
        if os.path.exists(os.path.join(PHOTOS_FOLDER, filename)):
            return send_from_directory(PHOTOS_FOLDER, filename, as_attachment=True)
        elif os.path.exists(os.path.join(EFFECT_FOLDER, filename)):
            return send_from_directory(EFFECT_FOLDER, filename, as_attachment=True)
        else:
            flash('Photo introuvable', 'error')
            return redirect(url_for('admin'))
    except Exception as e:
        flash(f'Erreur lors du téléchargement: {str(e)}', 'error')
        return redirect(url_for('admin'))

@app.route('/admin/reprint_photo/<filename>', methods=['POST'])
def reprint_photo(filename):
    """Réimprimer une photo spécifique"""
    try:
        # Chercher la photo dans les deux dossiers
        photo_path = None
        if os.path.exists(os.path.join(PHOTOS_FOLDER, filename)):
            photo_path = os.path.join(PHOTOS_FOLDER, filename)
        elif os.path.exists(os.path.join(EFFECT_FOLDER, filename)):
            photo_path = os.path.join(EFFECT_FOLDER, filename)
        
        if photo_path:
            # Vérifier si le script d'impression existe
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ScriptPythonPOS.py')
            if not os.path.exists(script_path):
                flash('Script d\'impression introuvable (ScriptPythonPOS.py)', 'error')
                return redirect(url_for('admin'))
            
            # Utiliser le script d'impression existant
            import subprocess
            cmd = [
                'python3', 'ScriptPythonPOS.py',
                '--image', photo_path
            ]
            
            # Ajouter le texte de pied de page si défini
            footer_text = config.get('footer_text', '')
            if footer_text:
                cmd.extend(['--text', footer_text])
            
            # Ajouter l'option HD si la résolution est élevée
            print_resolution = config.get('print_resolution', 384)
            if print_resolution > 384:
                cmd.append('--hd')
            
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
            
            if result.returncode == 0:
                flash('Photo réimprimée avec succès!', 'success')
            else:
                error_msg = result.stderr.strip() if result.stderr else 'Erreur inconnue'
                if 'ModuleNotFoundError' in error_msg and 'escpos' in error_msg:
                    flash('Module escpos manquant. Installez-le avec: pip install python-escpos', 'error')
                else:
                    flash(f'Erreur d\'impression: {error_msg}', 'error')
        else:
            flash('Photo introuvable', 'error')
    except Exception as e:
        flash(f'Erreur lors de la réimpression: {str(e)}', 'error')
    
    return redirect(url_for('admin'))

@app.route('/api/slideshow')
def get_slideshow_data():
    """API pour récupérer les données du diaporama/écran de veille"""
    photos = []
    
    # Déterminer le dossier source selon la configuration
    source_folder = EFFECT_FOLDER if config.get('slideshow_source', 'photos') == 'effet' else PHOTOS_FOLDER
    
    if os.path.exists(source_folder):
        for filename in os.listdir(source_folder):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                photos.append(filename)
    
    photos.sort(reverse=True)  # Plus récentes en premier
    
    return jsonify({
        'enabled': config.get('slideshow_enabled', False),
        'delay': config.get('slideshow_delay', 60),
        'photo_duration': config.get('slideshow_photo_duration', 5),
        'source': config.get('slideshow_source', 'photos'),
        'photos': photos
    })

@app.route('/api/printer_status')
def get_printer_status():
    """API pour vérifier l'état de l'imprimante"""
    return jsonify(check_printer_status())

@app.route('/photos/<filename>')
def serve_photo(filename):
    """Servir les photos"""
    # Vérifier d'abord dans le dossier photos
    if os.path.exists(os.path.join(PHOTOS_FOLDER, filename)):
        return send_from_directory(PHOTOS_FOLDER, filename)
    # Sinon vérifier dans le dossier effet
    elif os.path.exists(os.path.join(EFFECT_FOLDER, filename)):
        return send_from_directory(EFFECT_FOLDER, filename)
    else:
        abort(404)

@app.route('/video_stream')
def video_stream():
    """Flux vidéo MJPEG en temps réel"""
    return Response(generate_video_stream(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

def generate_video_stream():
    """Générer le flux vidéo MJPEG selon le type de caméra configuré"""
    global camera_process, usb_camera, last_frame
    
    # Déterminer le type de caméra à utiliser
    camera_type = config.get('camera_type', 'picamera')
    
    try:
        # Arrêter tout processus caméra existant
        stop_camera_process()
        
        # Utiliser la caméra USB si configurée
        if camera_type == 'usb':
            logger.info("[CAMERA] Démarrage de la caméra USB...")
            camera_id = config.get('usb_camera_id', 0)
            usb_camera = UsbCamera(camera_id=camera_id)
            if not usb_camera.start():
                raise Exception(f"Impossible de démarrer la caméra USB avec ID {camera_id}")
            
            # Générateur de frames pour la caméra USB
            while True:
                frame = usb_camera.get_frame()
                if frame:
                    # Stocker la frame pour capture instantanée
                    with frame_lock:
                        last_frame = frame
                    
                    # Envoyer la frame au navigateur
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n'
                           b'Content-Length: ' + str(len(frame)).encode() + b'\r\n\r\n' +
                           frame + b'\r\n')
                else:
                    time.sleep(0.03)  # Attendre si pas de frame disponible
        
        # Utiliser la Pi Camera par défaut
        else:
            logger.info("[CAMERA] Démarrage de la Pi Camera...")
            # Détecter si rpicam-vid ou libcamera-vid est disponible (Pi 5 vs Pi 4)
            import shutil
            if shutil.which('rpicam-vid'):
                camera_cmd = 'rpicam-vid'
                logger.info("[CAMERA] Utilisation de rpicam-vid (Raspberry Pi 5 / Bookworm)")
            elif shutil.which('libcamera-vid'):
                camera_cmd = 'libcamera-vid'
                logger.info("[CAMERA] Utilisation de libcamera-vid (Raspberry Pi 4 / Bullseye)")
            else:
                raise Exception("Aucune commande caméra trouvée (rpicam-vid ou libcamera-vid)")
            
            # Commande pour flux MJPEG - résolution 16/9
            cmd = [
                camera_cmd,
                '--codec', 'mjpeg',
                '--width', '1280',   # Résolution native plus compatible
                '--height', '720',   # Vrai 16/9 sans bandes noires
                '--framerate', '15', # Framerate plus élevé pour cette résolution
                '--timeout', '0',    # Durée infinie
                '--output', '-',     # Sortie vers stdout
                '--inline',          # Headers inline
                '--flush',           # Flush immédiat
                '--nopreview'        # Pas d'aperçu local
            ]
            
            camera_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Buffer pour assembler les frames JPEG
            buffer = b''
            
            while camera_process and camera_process.poll() is None:
                try:
                    # Lire les données par petits blocs
                    chunk = camera_process.stdout.read(1024)
                    if not chunk:
                        break
                        
                    buffer += chunk
                    
                    # Chercher les marqueurs JPEG
                    while True:
                        # Chercher le début d'une frame JPEG (0xFFD8)
                        start = buffer.find(b'\xff\xd8')
                        if start == -1:
                            break
                            
                        # Chercher la fin de la frame JPEG (0xFFD9)
                        end = buffer.find(b'\xff\xd9', start + 2)
                        if end == -1:
                            break
                            
                        # Extraire la frame complète
                        jpeg_frame = buffer[start:end + 2]
                        buffer = buffer[end + 2:]
                        
                        # Stocker la frame pour capture instantanée
                        with frame_lock:
                            last_frame = jpeg_frame
                        
                        # Envoyer la frame au navigateur
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n'
                               b'Content-Length: ' + str(len(jpeg_frame)).encode() + b'\r\n\r\n' +
                               jpeg_frame + b'\r\n')
                               
                except Exception as e:
                    logger.info(f"[CAMERA] Erreur lecture flux: {e}")
                    break
                
    except Exception as e:
        logger.info(f"Erreur flux vidéo: {e}")
        # Envoyer une frame d'erreur
        error_msg = f"Erreur caméra: {str(e)}"
        yield (b'--frame\r\n'
               b'Content-Type: text/plain\r\n\r\n' +
               error_msg.encode() + b'\r\n')
    finally:
        stop_camera_process()

def stop_camera_process():
    """Arrêter proprement le processus caméra (Pi Camera ou USB)"""
    global camera_process, usb_camera
    
    # Arrêter la caméra USB si active
    if usb_camera:
        try:
            usb_camera.stop()
        except Exception as e:
            logger.info(f"[CAMERA] Erreur lors de l'arrêt de la caméra USB: {e}")
        usb_camera = None
    
    # Arrêter le processus libcamera-vid si actif
    if camera_process:
        try:
            camera_process.terminate()
            camera_process.wait(timeout=2)
        except:
            try:
                camera_process.kill()
            except:
                pass
        camera_process = None

@app.route('/start_camera')
def start_camera():
    """Démarrer l'aperçu caméra"""
    global camera_active
    camera_active = True
    return jsonify({'status': 'camera_started'})

@app.route('/stop_camera')
def stop_camera():
    """Arrêter l'aperçu caméra"""
    global camera_active
    camera_active = False
    stop_camera_process()
    return jsonify({'status': 'camera_stopped'})

# Nettoyer les processus à la fermeture
@atexit.register
def cleanup():
    logger.info("[APP] Arrêt de l'application, nettoyage des ressources...")
    stop_camera_process()

def signal_handler(sig, frame):
    stop_camera_process()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
