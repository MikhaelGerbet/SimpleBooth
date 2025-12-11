#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, flash, Response, abort, session
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
from functools import wraps
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
camera_lock = threading.Lock()  # Verrou pour éviter les conflits de caméra
usb_camera = None

# Nettoyage des processus caméra zombies au démarrage
def cleanup_camera_on_startup():
    """Nettoyer tous les processus caméra au démarrage de l'application"""
    try:
        subprocess.run(['pkill', '-9', '-f', 'rpicam-vid'], capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'libcamera-vid'], capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'rpicam-still'], capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'libcamera-still'], capture_output=True, timeout=2)
        logger.info("[STARTUP] Processus caméra nettoyés au démarrage")
    except Exception as e:
        logger.warning(f"[STARTUP] Erreur nettoyage caméra: {e}")

def prestart_camera():
    """Pré-démarrer la caméra Pi pour qu'elle soit prête dès le premier accès"""
    global camera_process, camera_active
    
    camera_type = config.get('camera_type', 'picamera')
    if camera_type != 'picamera':
        logger.info("[STARTUP] Caméra USB configurée, pas de pré-démarrage")
        return
    
    try:
        import shutil
        if shutil.which('rpicam-vid'):
            camera_cmd = 'rpicam-vid'
        elif shutil.which('libcamera-vid'):
            camera_cmd = 'libcamera-vid'
        else:
            logger.warning("[STARTUP] Commande caméra non trouvée, pas de pré-démarrage")
            return
        
        cmd = [
            camera_cmd,
            '--codec', 'mjpeg',
            '--width', '1280',
            '--height', '720',
            '--framerate', '15',
            '--timeout', '0',
            '--output', '-',
            '--inline',
            '--flush',
            '--nopreview'
        ]
        
        logger.info(f"[STARTUP] Pré-démarrage caméra: {' '.join(cmd)}")
        
        with camera_lock:
            camera_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
        
        # Attendre un peu et vérifier que ça démarre
        time.sleep(0.5)
        
        if camera_process.poll() is None:
            camera_active = True
            logger.info("[STARTUP] Caméra pré-démarrée avec succès!")
        else:
            stderr = camera_process.stderr.read().decode('utf-8', errors='ignore')
            logger.warning(f"[STARTUP] Échec pré-démarrage caméra: {stderr}")
            camera_process = None
            
    except Exception as e:
        logger.warning(f"[STARTUP] Erreur pré-démarrage caméra: {e}")

# Exécuter le nettoyage au chargement du module
cleanup_camera_on_startup()

# Pré-démarrer la caméra en arrière-plan après un court délai
def delayed_camera_start():
    time.sleep(1)  # Laisser Flask démarrer d'abord
    prestart_camera()

# Lancer le pré-démarrage dans un thread séparé
threading.Thread(target=delayed_camera_start, daemon=True).start()

# ============================================
# AUTHENTIFICATION PAR CODE PIN
# ============================================

def require_pin(f):
    """Décorateur pour protéger les routes admin avec le code PIN"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_authenticated'):
            return redirect(url_for('unlock'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/unlock')
def unlock():
    """Page de saisie du code PIN"""
    # Si déjà authentifié, rediriger vers admin
    if session.get('admin_authenticated'):
        return redirect(url_for('admin'))
    return render_template('unlock.html')

@app.route('/verify_pin', methods=['POST'])
def verify_pin():
    """Vérification du code PIN"""
    data = request.get_json(force=True, silent=True) or {}
    entered_pin = data.get('pin', '')
    correct_pin = config.get('admin_pin', '1234')
    
    if entered_pin == correct_pin:
        session['admin_authenticated'] = True
        session.permanent = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Code incorrect'})

@app.route('/logout')
def logout():
    """Déconnexion de l'admin"""
    session.pop('admin_authenticated', None)
    return redirect(url_for('index'))

# ============================================

@app.route('/')
def index():
    """Page principale avec aperçu vidéo"""
    return render_template('index.html', timer=config['timer_seconds'])

# Variable globale pour stocker la dernière frame MJPEG
last_frame = None
frame_lock = threading.Lock()
last_frame_time = 0  # Timestamp de la dernière frame reçue

@app.route('/api/restart_camera', methods=['POST'])
def restart_camera():
    """Redémarrer le flux caméra en cas de problème"""
    global camera_process
    try:
        logger.info("[CAMERA] Redémarrage demandé via API...")
        stop_camera_process()
        time.sleep(0.5)
        logger.info("[CAMERA] Caméra prête à être redémarrée")
        return jsonify({'success': True, 'message': 'Caméra redémarrée'})
    except Exception as e:
        logger.error(f"[CAMERA] Erreur redémarrage: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/camera_status')
def camera_status():
    """Vérifier si la caméra envoie des frames"""
    global last_frame_time
    current_time = time.time()
    is_active = (current_time - last_frame_time) < 3  # Frame reçue dans les 3 dernières secondes
    return jsonify({
        'active': is_active,
        'last_frame_age': current_time - last_frame_time if last_frame_time > 0 else -1
    })

@app.route('/api/config')
def get_public_config():
    """Retourner la configuration publique pour le frontend"""
    return jsonify({
        'print_enabled': config.get('print_enabled', True),
        'effect_enabled': config.get('effect_enabled', True),
        'runware_api_key': bool(config.get('runware_api_key', '')),
        'telegram_enabled': config.get('telegram_enabled', False),
        'timer_seconds': config.get('timer_seconds', 5),
        'slideshow_enabled': config.get('slideshow_enabled', True)
    })

@app.route('/capture', methods=['POST'])
def capture_photo():
    """Capturer une photo haute résolution pour impression 15x10cm (300dpi)"""
    global current_photo, original_photo, last_frame
    
    try:
        # Récupérer le style de la requête
        data = request.get_json(force=True, silent=True) or {}
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
            
            # IMPORTANT: Stopper le flux vidéo AVANT la capture pour libérer la caméra
            # rpicam-still ne peut pas accéder à la caméra si rpicam-vid tourne
            logger.info("[CAPTURE] Arrêt du flux vidéo pour libérer la caméra...")
            stop_camera_process()
            time.sleep(0.3)  # Petit délai pour s'assurer que la caméra est libérée
            
            # Capture HAUTE RÉSOLUTION maximale pour Pi Camera Module 3 (IMX708)
            # La caméra supporte jusqu'à 4608x2592 (12MP)
            # On capture à la résolution max pour avoir la meilleure qualité source
            # Le redimensionnement pour l'impression se fait dans print_cups.py
            cmd = [
                still_cmd,
                '--output', filepath,
                '--width', '4608',      # Résolution max IMX708
                '--height', '2592',     # Résolution max IMX708
                '--quality', '98',      # Qualité JPEG maximale
                '--immediate',          # Capture immédiate sans délai
                '--nopreview',          # Pas d'aperçu
                '--timeout', '1'        # Timeout minimal
            ]
            
            logger.info(f"[CAPTURE] Commande: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            
            # Relancer le flux vidéo après capture (sera fait automatiquement par le prochain appel à /video_stream)
            # On ne relance pas ici car l'utilisateur est sur la page review, pas besoin du flux immédiatement
            
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
                logger.info(f"[CAPTURE] Photo haute résolution capturée: {filename} (4608x2592 - 12MP)")
        
        # Appliquer le style N&B si sélectionné
        if photo_style == 'bw':
            try:
                img = Image.open(filepath)
                img_bw = img.convert('L').convert('RGB')  # Convertir en niveaux de gris puis RGB
                img_bw.save(filepath, 'JPEG', quality=95)
                logger.info(f"[CAPTURE] Style N&B appliqué à {filename}")
            except Exception as e:
                logger.error(f"[CAPTURE] Erreur application N&B: {e}")
        
        # Appliquer l'overlay si activé
        if config.get('overlay_enabled', False) and config.get('current_overlay', ''):
            logger.info(f"[CAPTURE] Application de l'overlay sur la photo...")
            apply_overlay(filepath)
        
        current_photo = filename
        original_photo = filename
        
        # Réinitialiser le compteur de générations IA pour la nouvelle photo
        global ai_generation_count
        ai_generation_count = 0
        
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

@app.route('/api/last_photo')
def api_last_photo():
    """API pour récupérer le chemin de la dernière photo capturée"""
    global current_photo
    
    if not current_photo:
        return jsonify({'success': False, 'error': 'Aucune photo disponible'})
    
    # Vérifier si la photo existe dans photos/ ou effet/
    if os.path.exists(os.path.join(PHOTOS_FOLDER, current_photo)):
        photo_path = f'photos/{current_photo}'
    elif os.path.exists(os.path.join(EFFECT_FOLDER, current_photo)):
        photo_path = f'effet/{current_photo}'
    else:
        return jsonify({'success': False, 'error': 'Photo introuvable'})
    
    return jsonify({
        'success': True,
        'photo_path': photo_path,
        'filename': current_photo
    })

@app.route('/print_photo', methods=['POST'])
def print_photo():
    """Imprimer la photo actuelle"""
    global current_photo
    
    # Récupérer le photo_path depuis le JSON envoyé, sinon utiliser current_photo
    data = request.get_json(force=True, silent=True) or {}
    logger.info(f"[PRINT] Data reçue: {data}, current_photo: {current_photo}")
    photo_filename = data.get('photo_path') or current_photo
    
    if not photo_filename:
        return jsonify({'success': False, 'error': 'Aucune photo à imprimer'})
    
    # Extraire juste le nom du fichier si un chemin complet est fourni
    photo_filename = os.path.basename(photo_filename)
    logger.info(f"[PRINT] Photo filename: {photo_filename}")
    
    try:
        # Vérifier si l'imprimante est activée
        if not config.get('printer_enabled', True):
            return jsonify({'success': False, 'error': 'Imprimante désactivée dans la configuration'})
        
        # Chercher la photo dans le bon dossier
        photo_path = None
        if os.path.exists(os.path.join(PHOTOS_FOLDER, photo_filename)):
            photo_path = os.path.join(PHOTOS_FOLDER, photo_filename)
        elif os.path.exists(os.path.join(EFFECT_FOLDER, photo_filename)):
            photo_path = os.path.join(EFFECT_FOLDER, photo_filename)
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

# Compteur de générations IA par photo (reset à chaque nouvelle photo)
ai_generation_count = 0
MAX_AI_GENERATIONS = 5

@app.route('/apply_effect', methods=['POST'])
def apply_effect():
    """Appliquer un effet IA à la photo actuelle via Runware"""
    global current_photo, original_photo, ai_generation_count
    
    # Vérifier le compteur de générations
    if ai_generation_count >= MAX_AI_GENERATIONS:
        return jsonify({
            'success': False, 
            'error': f'Limite de {MAX_AI_GENERATIONS} générations atteinte pour cette photo',
            'limit_reached': True,
            'generation_count': ai_generation_count
        })
    
    # Récupérer le prompt_id depuis la requête
    data = request.get_json() or {}
    prompt_id = data.get('prompt_id')
    
    if not prompt_id:
        return jsonify({'success': False, 'error': 'Veuillez sélectionner un style'})
    
    # Trouver le prompt correspondant (utiliser les prompts par défaut si non configurés)
    prompts = config.get('ai_prompts') or get_default_ai_prompts()
    selected_prompt = None
    for p in prompts:
        if p['id'] == prompt_id and p.get('enabled', True):
            selected_prompt = p
            break
    
    if not selected_prompt:
        return jsonify({'success': False, 'error': 'Style non trouvé ou désactivé'})
    
    # Utiliser la photo originale (SANS overlay) pour le traitement IA
    photo_to_process = original_photo if original_photo else current_photo
    
    if not photo_to_process:
        return jsonify({'success': False, 'error': 'Aucune photo à traiter'})
    
    if not config.get('effect_enabled', False):
        return jsonify({'success': False, 'error': 'Les effets IA sont désactivés'})
    
    if not config.get('runware_api_key'):
        return jsonify({'success': False, 'error': 'Clé API Runware manquante'})
    
    try:
        # Utiliser la photo originale SANS overlay (dans PHOTOS_FOLDER)
        photo_path = os.path.join(PHOTOS_FOLDER, photo_to_process)
        
        if not os.path.exists(photo_path):
            return jsonify({'success': False, 'error': 'Photo originale introuvable'})
        
        logger.info(f"[IA] Génération {ai_generation_count + 1}/{MAX_AI_GENERATIONS} avec style: {selected_prompt['name']}")
        result = asyncio.run(apply_effect_runware(photo_path, selected_prompt))
        
        # Incrémenter le compteur si succès
        result_data = result.get_json()
        if result_data.get('success'):
            ai_generation_count += 1
            # Ajouter le compteur à la réponse
            result_data['generation_count'] = ai_generation_count
            result_data['max_generations'] = MAX_AI_GENERATIONS
            result_data['limit_reached'] = ai_generation_count >= MAX_AI_GENERATIONS
            return jsonify(result_data)
        
        return result
            
    except Exception as e:
        logger.error(f"Erreur lors de l'application de l'effet: {e}")
        return jsonify({'success': False, 'error': f'Erreur IA: {str(e)}'})

@app.route('/api/ai_generation_status')
def get_ai_generation_status():
    """Récupérer le statut des générations IA pour la photo actuelle"""
    return jsonify({
        'generation_count': ai_generation_count,
        'max_generations': MAX_AI_GENERATIONS,
        'limit_reached': ai_generation_count >= MAX_AI_GENERATIONS
    })


async def apply_effect_runware(photo_path, prompt_config):
    """Fonction asynchrone pour appliquer l'effet IA via Runware"""
    global current_photo
    
    try:
        prompt_text = prompt_config['prompt']
        prompt_name = prompt_config['name']
        
        logger.info(f"[IA] Début du traitement avec style: {prompt_name}")
        logger.info(f"[IA] Photo source: {photo_path}")
        
        # Initialiser Runware
        runware = Runware(api_key=config['runware_api_key'])
        await runware.connect()
        logger.info("[IA] Connexion Runware établie")
        
        # Lire et encoder l'image en base64
        with open(photo_path, 'rb') as img_file:
            img_data = img_file.read()
            img_base64 = base64.b64encode(img_data).decode('utf-8')
        
        # Résolution supportée par Runware pour Canon SELPHY CP1500
        # Ratio 1.50 (proche de 1.48 pour 148x100mm)
        # Dimensions supportées: 1248x832
        AI_WIDTH = 1248
        AI_HEIGHT = 832
        
        # Préparer la requête d'inférence
        request = IImageInference(
            positivePrompt=prompt_text,
            referenceImages=[f"data:image/jpeg;base64,{img_base64}"],
            model="runware:106@1",
            height=AI_HEIGHT, 
            width=AI_WIDTH,  
            steps=config.get('effect_steps', 5),
            CFGScale=2.5,
            numberResults=1
        )
        
        logger.info(f"[IA] Requête préparée - {AI_WIDTH}x{AI_HEIGHT}, {config.get('effect_steps', 5)} étapes")
        
        # Appliquer l'effet
        images = await runware.imageInference(requestImage=request)
        
        if images and len(images) > 0:
            # Télécharger l'image transformée
            logger.info(f"[IA] Image générée, téléchargement...")
            import requests as req
            response = req.get(images[0].imageURL)
            
            if response.status_code == 200:
                os.makedirs(EFFECT_FOLDER, exist_ok=True)
                
                # Créer un nom de fichier unique
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                prompt_id = prompt_config['id']
                
                # Sauvegarder l'image SANS overlay d'abord
                effect_filename_raw = f'effect_{prompt_id}_{timestamp}_raw.jpg'
                effect_path_raw = os.path.join(EFFECT_FOLDER, effect_filename_raw)
                with open(effect_path_raw, 'wb') as f:
                    f.write(response.content)
                logger.info(f"[IA] Image brute sauvegardée: {effect_filename_raw}")
                
                # Créer la version avec overlay
                effect_filename = f'effect_{prompt_id}_{timestamp}.jpg'
                effect_path = os.path.join(EFFECT_FOLDER, effect_filename)
                
                # Copier l'image brute comme base
                with open(effect_path, 'wb') as f:
                    f.write(response.content)
                
                # Appliquer l'overlay si activé
                if config.get('overlay_enabled', False) and config.get('current_overlay', ''):
                    logger.info("[IA] Application de l'overlay...")
                    apply_overlay(effect_path)
                
                # Mettre à jour la photo actuelle (version avec overlay)
                current_photo = effect_filename
                logger.info(f"[IA] Effet '{prompt_name}' appliqué avec succès!")
                
                # Envoyer sur Telegram si activé
                send_type = config.get('telegram_send_type', 'photos')
                if send_type in ['effet', 'both']:
                    threading.Thread(target=send_to_telegram, args=(effect_path, config, "effet")).start()
                
                return jsonify({
                    'success': True, 
                    'message': f'Style "{prompt_name}" appliqué!',
                    'new_filename': effect_filename,
                    'photo_path': f'effet/{effect_filename}',
                    'style_name': prompt_name
                })
            else:
                logger.error(f"[IA] Échec téléchargement: code {response.status_code}")
                return jsonify({'success': False, 'error': 'Erreur lors du téléchargement'})
        else:
            logger.error("[IA] Aucune image générée")
            return jsonify({'success': False, 'error': 'Aucune image générée par l\'IA'})
            
    except Exception as e:
        logger.error(f"[IA] Erreur: {e}")
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
    data = request.get_json(force=True, silent=True) or {}
    
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
# GESTION DES PROMPTS IA
# ============================================

def get_default_ai_prompts():
    """Retourne les prompts IA par défaut"""
    return [
        {
            "id": "superhero",
            "name": "Super-Héros",
            "icon": "fa-mask",
            "prompt": "Transform this photo into an Avengers superhero scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Only add superhero costumes and elements around the body: Iron Man armor, Captain America suit with shield, Thor cape and armor, Black Widow tactical suit, or Spider-Man suit. Add dramatic Marvel cinematic lighting and epic background with city skyline. Maintain photorealistic quality for faces, only stylize the costumes and environment. Do not modify facial features in any way. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 1
        },
        {
            "id": "astronaut",
            "name": "Astronaute",
            "icon": "fa-rocket",
            "prompt": "Transform this photo into an astronaut space scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions visible through a realistic space helmet visor. Add authentic NASA-style spacesuit with detailed textures, patches, and equipment. Background should be outer space with Earth visible, stars, and perhaps the International Space Station. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality, cinematic lighting.",
            "enabled": True,
            "order": 2
        },
        {
            "id": "redcarpet",
            "name": "Red Carpet",
            "icon": "fa-star",
            "prompt": "Transform this photo into a Hollywood red carpet glamour scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add elegant formal attire: stunning evening gowns or sharp tuxedos. Background should be a prestigious red carpet event with paparazzi flashes, velvet ropes, and movie premiere atmosphere. Add subtle professional makeup enhancement without changing facial structure. Ultra high resolution, 8K quality, professional photography lighting.",
            "enabled": True,
            "order": 3
        },
        {
            "id": "popart",
            "name": "Pop Art",
            "icon": "fa-palette",
            "prompt": "Transform this photo into Andy Warhol style pop art. Keep the facial features recognizable but apply bold pop art colors: bright pinks, yellows, blues, and oranges. Add Ben-Day dots pattern, bold outlines, and comic book style effects. Background should be divided into colorful panels like Warhol's famous portraits. Maintain the essence of the person while applying artistic stylization. High contrast, vibrant colors.",
            "enabled": True,
            "order": 4
        },
        {
            "id": "pirate",
            "name": "Pirate",
            "icon": "fa-skull-crossbones",
            "prompt": "Transform this photo into a pirate captain scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add authentic pirate costume: tricorn hat, weathered coat, bandana, and pirate accessories. Background should be a pirate ship deck with ocean, sails, and treasure. Add dramatic lighting like sunset on the sea. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 5
        },
        {
            "id": "royalty",
            "name": "Royauté",
            "icon": "fa-crown",
            "prompt": "Transform this photo into a royal portrait scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add regal attire: ornate royal robes, crown or tiara, jewels, and royal regalia. Background should be a magnificent throne room or palace interior with velvet drapes, golden decorations, and chandeliers. Renaissance painting style lighting. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 6
        },
        {
            "id": "wizard",
            "name": "Fantasy",
            "icon": "fa-hat-wizard",
            "prompt": "Transform this photo into a magical fantasy wizard scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add wizard robes, magical staff, and mystical accessories. Background should be an enchanted forest or magical castle with floating lights, magical particles, and mystical atmosphere. Add subtle magical glow effects around the person. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 7
        },
        {
            "id": "christmas",
            "name": "Noël",
            "icon": "fa-snowflake",
            "prompt": "Transform this photo into a magical Christmas scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add festive holiday attire: cozy Christmas sweaters, Santa hat, or elegant winter clothing. Background should be a warm Christmas setting with decorated tree, fireplace, snow falling outside window, and warm golden lighting. Add subtle snowflakes and holiday magic. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 8
        },
        {
            "id": "tropical",
            "name": "Tropical",
            "icon": "fa-umbrella-beach",
            "prompt": "Transform this photo into a tropical paradise beach scene. Keep the faces exactly as they are, with identical facial features, skin tone, and expressions. Add Hawaiian shirts, leis, sunglasses, or beach attire. Background should be a stunning tropical beach with palm trees, crystal clear turquoise water, white sand, and beautiful sunset. Add warm golden hour lighting. Maintain photorealistic quality for faces. Ultra high resolution, 8K quality.",
            "enabled": True,
            "order": 9
        },
        {
            "id": "anime",
            "name": "Anime",
            "icon": "fa-yin-yang",
            "prompt": "Transform this photo into beautiful Studio Ghibli anime style illustration. Convert the people into anime characters while maintaining their recognizable features, hairstyle, and expressions. Use soft watercolor-like textures, warm colors, and dreamy atmosphere typical of Hayao Miyazaki films. Background should be whimsical and magical with floating elements, soft clouds, or enchanted scenery. High quality anime illustration, vibrant but soft colors.",
            "enabled": True,
            "order": 10
        }
    ]

def init_ai_prompts():
    """Initialise les prompts IA si non existants"""
    global config
    if 'ai_prompts' not in config:
        config['ai_prompts'] = get_default_ai_prompts()
        save_config(config)
    return config['ai_prompts']

@app.route('/api/ai_prompts')
def get_ai_prompts():
    """Récupérer tous les prompts IA (pour le frontend)"""
    prompts = config.get('ai_prompts', get_default_ai_prompts())
    # Ne retourner que les prompts actifs pour le frontend public
    active_prompts = [p for p in prompts if p.get('enabled', True)]
    active_prompts.sort(key=lambda x: x.get('order', 999))
    return jsonify({'success': True, 'prompts': active_prompts})

@app.route('/api/ai_prompts/all')
def get_all_ai_prompts():
    """Récupérer tous les prompts IA (pour l'admin)"""
    prompts = config.get('ai_prompts', get_default_ai_prompts())
    prompts.sort(key=lambda x: x.get('order', 999))
    return jsonify({'success': True, 'prompts': prompts})

@app.route('/api/ai_prompts', methods=['POST'])
def add_ai_prompt():
    """Ajouter un nouveau prompt IA"""
    global config
    data = request.get_json()
    
    if not data.get('name') or not data.get('prompt'):
        return jsonify({'success': False, 'error': 'Nom et prompt requis'})
    
    prompts = config.get('ai_prompts', [])
    
    # Générer un ID unique
    import uuid
    new_id = data.get('id', str(uuid.uuid4())[:8])
    
    # Trouver le prochain ordre
    max_order = max([p.get('order', 0) for p in prompts], default=0)
    
    new_prompt = {
        'id': new_id,
        'name': data['name'],
        'icon': data.get('icon', 'fa-magic'),
        'prompt': data['prompt'],
        'enabled': data.get('enabled', True),
        'order': data.get('order', max_order + 1)
    }
    
    prompts.append(new_prompt)
    config['ai_prompts'] = prompts
    save_config(config)
    
    return jsonify({'success': True, 'prompt': new_prompt})

@app.route('/api/ai_prompts/<prompt_id>', methods=['PUT'])
def update_ai_prompt(prompt_id):
    """Mettre à jour un prompt IA"""
    global config
    data = request.get_json()
    
    prompts = config.get('ai_prompts', [])
    
    for i, p in enumerate(prompts):
        if p['id'] == prompt_id:
            prompts[i]['name'] = data.get('name', p['name'])
            prompts[i]['icon'] = data.get('icon', p['icon'])
            prompts[i]['prompt'] = data.get('prompt', p['prompt'])
            prompts[i]['enabled'] = data.get('enabled', p['enabled'])
            prompts[i]['order'] = data.get('order', p['order'])
            
            config['ai_prompts'] = prompts
            save_config(config)
            return jsonify({'success': True, 'prompt': prompts[i]})
    
    return jsonify({'success': False, 'error': 'Prompt non trouvé'})

@app.route('/api/ai_prompts/<prompt_id>', methods=['DELETE'])
def delete_ai_prompt(prompt_id):
    """Supprimer un prompt IA"""
    global config
    
    prompts = config.get('ai_prompts', [])
    prompts = [p for p in prompts if p['id'] != prompt_id]
    
    config['ai_prompts'] = prompts
    save_config(config)
    
    return jsonify({'success': True})

@app.route('/api/ai_prompts/<prompt_id>/toggle', methods=['POST'])
def toggle_ai_prompt(prompt_id):
    """Activer/désactiver un prompt IA"""
    global config
    
    prompts = config.get('ai_prompts', [])
    
    for i, p in enumerate(prompts):
        if p['id'] == prompt_id:
            prompts[i]['enabled'] = not prompts[i].get('enabled', True)
            config['ai_prompts'] = prompts
            save_config(config)
            return jsonify({'success': True, 'enabled': prompts[i]['enabled']})
    
    return jsonify({'success': False, 'error': 'Prompt non trouvé'})

@app.route('/api/ai_prompts/reset', methods=['POST'])
def reset_ai_prompts():
    """Réinitialiser les prompts IA par défaut"""
    global config
    config['ai_prompts'] = get_default_ai_prompts()
    save_config(config)
    return jsonify({'success': True, 'prompts': config['ai_prompts']})


# ============================================
# GESTION WIFI
# ============================================

def get_saved_wifi_networks():
    """Récupérer les réseaux WiFi sauvegardés dans la config"""
    return config.get('wifi_networks', [])

def save_wifi_network(ssid, password):
    """Sauvegarder un réseau WiFi dans la config"""
    global config
    networks = config.get('wifi_networks', [])
    
    # Mettre à jour si existe déjà, sinon ajouter
    found = False
    for i, net in enumerate(networks):
        if net['ssid'] == ssid:
            networks[i]['password'] = password
            found = True
            break
    
    if not found:
        networks.append({'ssid': ssid, 'password': password})
    
    config['wifi_networks'] = networks
    save_config(config)

@app.route('/api/wifi/status')
def get_wifi_status():
    """Récupérer le statut WiFi actuel"""
    try:
        # Récupérer le réseau connecté
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL,SECURITY', 'dev', 'wifi'],
            capture_output=True, text=True, timeout=10
        )
        
        connected_ssid = None
        signal = None
        
        for line in result.stdout.strip().split('\n'):
            if line.startswith('yes:'):
                parts = line.split(':')
                if len(parts) >= 3:
                    connected_ssid = parts[1]
                    signal = parts[2]
                    break
        
        # Récupérer l'IP
        ip_result = subprocess.run(
            ['hostname', '-I'],
            capture_output=True, text=True, timeout=5
        )
        ip_address = ip_result.stdout.strip().split()[0] if ip_result.stdout.strip() else 'Non connecté'
        
        return jsonify({
            'success': True,
            'connected': connected_ssid is not None,
            'ssid': connected_ssid,
            'signal': signal,
            'ip_address': ip_address
        })
    except Exception as e:
        logger.error(f"[WIFI] Erreur statut: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/wifi/scan')
def scan_wifi_networks():
    """Scanner les réseaux WiFi disponibles"""
    try:
        # Forcer un rescan
        subprocess.run(['nmcli', 'dev', 'wifi', 'rescan'], capture_output=True, timeout=10)
        time.sleep(2)
        
        # Lister les réseaux
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,ACTIVE', 'dev', 'wifi', 'list'],
            capture_output=True, text=True, timeout=15
        )
        
        networks = []
        seen_ssids = set()
        
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':')
            if len(parts) >= 3:
                ssid = parts[0].strip()
                if ssid and ssid not in seen_ssids:
                    seen_ssids.add(ssid)
                    networks.append({
                        'ssid': ssid,
                        'signal': int(parts[1]) if parts[1].isdigit() else 0,
                        'security': parts[2] if len(parts) > 2 else 'Open',
                        'connected': parts[3] == 'yes' if len(parts) > 3 else False
                    })
        
        # Trier par signal décroissant
        networks.sort(key=lambda x: x['signal'], reverse=True)
        
        return jsonify({'success': True, 'networks': networks})
    except Exception as e:
        logger.error(f"[WIFI] Erreur scan: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/wifi/connect', methods=['POST'])
def connect_wifi():
    """Se connecter à un réseau WiFi"""
    data = request.get_json()
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    save_network = data.get('save', True)
    
    if not ssid:
        return jsonify({'success': False, 'error': 'SSID requis'})
    
    try:
        logger.info(f"[WIFI] Connexion à {ssid}...")
        
        # Supprimer l'ancienne connexion si existe
        subprocess.run(['nmcli', 'connection', 'delete', ssid], 
                      capture_output=True, timeout=10)
        
        # Créer la nouvelle connexion
        if password:
            result = subprocess.run(
                ['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password],
                capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ['nmcli', 'dev', 'wifi', 'connect', ssid],
                capture_output=True, text=True, timeout=30
            )
        
        if result.returncode == 0:
            logger.info(f"[WIFI] Connecté à {ssid}")
            
            # Sauvegarder si demandé
            if save_network and password:
                save_wifi_network(ssid, password)
            
            # Attendre et récupérer la nouvelle IP
            time.sleep(3)
            ip_result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
            ip_address = ip_result.stdout.strip().split()[0] if ip_result.stdout.strip() else ''
            
            return jsonify({
                'success': True,
                'message': f'Connecté à {ssid}',
                'ip_address': ip_address
            })
        else:
            error_msg = result.stderr.strip() or 'Échec de connexion'
            logger.error(f"[WIFI] Erreur: {error_msg}")
            return jsonify({'success': False, 'error': error_msg})
            
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Timeout de connexion'})
    except Exception as e:
        logger.error(f"[WIFI] Erreur connexion: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/wifi/saved')
def get_saved_wifi():
    """Récupérer les réseaux WiFi sauvegardés"""
    networks = get_saved_wifi_networks()
    # Ne pas exposer les mots de passe complets
    safe_networks = []
    for net in networks:
        safe_networks.append({
            'ssid': net['ssid'],
            'has_password': bool(net.get('password'))
        })
    return jsonify({'success': True, 'networks': safe_networks})

@app.route('/api/wifi/saved', methods=['POST'])
def add_saved_wifi():
    """Ajouter un réseau WiFi sauvegardé"""
    data = request.get_json()
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    
    if not ssid:
        return jsonify({'success': False, 'error': 'SSID requis'})
    
    save_wifi_network(ssid, password)
    return jsonify({'success': True})

@app.route('/api/wifi/saved/<ssid>', methods=['DELETE'])
def delete_saved_wifi(ssid):
    """Supprimer un réseau WiFi sauvegardé"""
    global config
    networks = config.get('wifi_networks', [])
    networks = [n for n in networks if n['ssid'] != ssid]
    config['wifi_networks'] = networks
    save_config(config)
    return jsonify({'success': True})

@app.route('/api/wifi/connect_saved', methods=['POST'])
def connect_saved_wifi():
    """Se connecter à un réseau WiFi sauvegardé"""
    data = request.get_json()
    ssid = data.get('ssid', '').strip()
    
    if not ssid:
        return jsonify({'success': False, 'error': 'SSID requis'})
    
    # Chercher le mot de passe sauvegardé
    networks = get_saved_wifi_networks()
    password = None
    for net in networks:
        if net['ssid'] == ssid:
            password = net.get('password', '')
            break
    
    if password is None:
        return jsonify({'success': False, 'error': 'Réseau non trouvé dans les configs sauvegardées'})
    
    # Utiliser la fonction de connexion existante
    try:
        logger.info(f"[WIFI] Connexion au réseau sauvegardé: {ssid}")
        
        subprocess.run(['nmcli', 'connection', 'delete', ssid], capture_output=True, timeout=10)
        
        if password:
            result = subprocess.run(
                ['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password],
                capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ['nmcli', 'dev', 'wifi', 'connect', ssid],
                capture_output=True, text=True, timeout=30
            )
        
        if result.returncode == 0:
            time.sleep(3)
            ip_result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
            ip_address = ip_result.stdout.strip().split()[0] if ip_result.stdout.strip() else ''
            
            return jsonify({
                'success': True,
                'message': f'Connecté à {ssid}',
                'ip_address': ip_address
            })
        else:
            return jsonify({'success': False, 'error': result.stderr.strip() or 'Échec de connexion'})
            
    except Exception as e:
        logger.error(f"[WIFI] Erreur: {e}")
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
    telegram_invite_link = config.get('telegram_invite_link', '')  # Lien manuel optionnel
    
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
            'qrcode_url': f'/static/qrcodes/{qrcode_filename}'
        })
    
    # Le QR Code n'existe pas, on doit le générer
    try:
        # S'assurer que le dossier existe
        os.makedirs(QRCODES_FOLDER, exist_ok=True)
        
        invite_link = None
        
        # 1. Utiliser le lien manuel s'il est configuré
        if telegram_invite_link:
            invite_link = telegram_invite_link
            logger.info(f"[QRCODE] Utilisation du lien manuel: {invite_link}")
        # 2. Si le chat_id commence par @, c'est un username public
        elif chat_id.startswith('@'):
            invite_link = f'https://t.me/{chat_id[1:]}'
            logger.info(f"[QRCODE] Canal public détecté: {invite_link}")
        else:
            # 3. Sinon, on essaie de récupérer le lien d'invitation via l'API
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
            'qrcode_url': f'/static/qrcodes/{qrcode_filename}'
        })
        
    except ImportError:
        logger.error("[QRCODE] Module qrcode non installé. Installez-le avec: pip install qrcode[pil]")
        return jsonify({'success': False, 'error': 'Module qrcode non installé'})
    except Exception as e:
        logger.error(f"[QRCODE] Erreur: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin')
@require_pin
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
@require_pin
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
        config['telegram_invite_link'] = request.form.get('telegram_invite_link', '')
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
        
        # Configuration sécurité
        new_pin = request.form.get('admin_pin', '').strip()
        if new_pin and new_pin.isdigit() and 4 <= len(new_pin) <= 8:
            config['admin_pin'] = new_pin
        
        save_config(config)
        flash('Configuration sauvegardée avec succès!', 'success')
        
    except Exception as e:
        flash(f'Erreur lors de la sauvegarde: {str(e)}', 'error')
    
    return redirect(url_for('admin'))

@app.route('/admin/delete_photos', methods=['POST'])
@require_pin
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
@require_pin
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

@app.route('/admin/delete_photo/<filename>', methods=['POST'])
@require_pin
def delete_single_photo(filename):
    """Supprimer une photo individuelle"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        photo_type = data.get('type', 'photo')
        
        # Déterminer le dossier selon le type
        if photo_type == 'effet':
            photo_path = os.path.join(EFFECT_FOLDER, filename)
        else:
            photo_path = os.path.join(PHOTOS_FOLDER, filename)
        
        # Si pas trouvé, chercher dans l'autre dossier
        if not os.path.exists(photo_path):
            if photo_type == 'effet':
                photo_path = os.path.join(PHOTOS_FOLDER, filename)
            else:
                photo_path = os.path.join(EFFECT_FOLDER, filename)
        
        if os.path.exists(photo_path):
            os.remove(photo_path)
            logger.info(f"Photo supprimée: {photo_path}")
            return jsonify({'success': True, 'message': 'Photo supprimée'})
        else:
            return jsonify({'success': False, 'error': 'Photo introuvable'})
            
    except Exception as e:
        logger.error(f"Erreur suppression photo: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/reprint_photo/<filename>', methods=['POST'])
@require_pin
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

@app.route('/effet/<filename>')
def serve_effect(filename):
    """Servir les photos avec effet IA"""
    if os.path.exists(os.path.join(EFFECT_FOLDER, filename)):
        return send_from_directory(EFFECT_FOLDER, filename)
    else:
        abort(404)

@app.route('/video_stream')
def video_stream():
    """Flux vidéo MJPEG en temps réel"""
    # Marquer la caméra comme active dès qu'un client demande le flux
    global camera_active
    camera_active = True
    return Response(generate_video_stream(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

def generate_video_stream():
    """Générer le flux vidéo MJPEG selon le type de caméra configuré"""
    global camera_process, usb_camera, last_frame
    
    # Déterminer le type de caméra à utiliser
    camera_type = config.get('camera_type', 'picamera')
    
    try:
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
            
            # Vérifier si un processus caméra existe déjà et est toujours actif
            with camera_lock:
                if camera_process is not None and camera_process.poll() is None:
                    logger.info("[CAMERA] Processus caméra déjà actif, réutilisation...")
                else:
                    # Nettoyer tout ancien processus mort
                    if camera_process is not None:
                        logger.info("[CAMERA] Ancien processus mort détecté, nettoyage...")
                        try:
                            camera_process.kill()
                            camera_process.wait(timeout=1)
                        except:
                            pass
                        camera_process = None
                    
                    # Tuer les zombies éventuels
                    kill_camera_processes()
                    time.sleep(0.3)
                    
                    # Commande pour flux MJPEG - résolution 16/9
                    cmd = [
                        camera_cmd,
                        '--codec', 'mjpeg',
                        '--width', '1280',   # Résolution native plus compatible
                        '--height', '720',   # Vrai 16/9 sans bandes noires
                        '--framerate', '15', # Framerate stable
                        '--timeout', '0',    # Durée infinie
                        '--output', '-',     # Sortie vers stdout
                        '--inline',          # Headers inline
                        '--flush',           # Flush immédiat
                        '--nopreview'        # Pas d'aperçu local
                    ]
                    
                    logger.info(f"[CAMERA] Lancement: {' '.join(cmd)}")
                    
                    camera_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0
                    )
            
            # Attendre un peu que la caméra démarre (seulement si nouveau processus)
            time.sleep(0.3)
            
            # Vérifier que le processus est toujours actif
            with camera_lock:
                if camera_process is None or camera_process.poll() is not None:
                    stderr_msg = ""
                    if camera_process and camera_process.stderr:
                        stderr_msg = camera_process.stderr.read().decode('utf-8', errors='ignore')
                    logger.error(f"[CAMERA] Échec du démarrage: {stderr_msg}")
                    raise Exception(f"La caméra n'a pas pu démarrer: {stderr_msg}")
            
            logger.info("[CAMERA] Pi Camera démarrée avec succès")
            
            # Buffer pour assembler les frames JPEG
            buffer = b''
            frames_received = 0
            last_frame_log = time.time()
            
            while True:
                # Vérifier que le processus caméra est toujours actif
                with camera_lock:
                    if camera_process is None or camera_process.poll() is not None:
                        logger.warning("[CAMERA] Processus caméra mort, sortie de la boucle pour relance...")
                        break
                
                try:
                    # Lire les données par blocs plus grands pour éviter les artefacts
                    chunk = camera_process.stdout.read(8192)
                    if not chunk:
                        time.sleep(0.01)
                        continue
                        
                    buffer += chunk
                    
                    # Limiter la taille du buffer pour éviter les fuites mémoire
                    if len(buffer) > 2000000:  # 2MB max
                        # Chercher le dernier marqueur de début valide
                        last_start = buffer.rfind(b'\xff\xd8')
                        if last_start > 0:
                            buffer = buffer[last_start:]
                    
                    # Chercher les marqueurs JPEG
                    while True:
                        # Chercher le début d'une frame JPEG (0xFFD8)
                        start = buffer.find(b'\xff\xd8')
                        if start == -1:
                            buffer = b''  # Vider le buffer si pas de début trouvé
                            break
                        
                        # Jeter tout ce qui précède le début
                        if start > 0:
                            buffer = buffer[start:]
                            start = 0
                            
                        # Chercher la fin de la frame JPEG (0xFFD9)
                        end = buffer.find(b'\xff\xd9', start + 2)
                        if end == -1:
                            break
                            
                        # Extraire la frame complète
                        jpeg_frame = buffer[start:end + 2]
                        buffer = buffer[end + 2:]
                        
                        # Validation minimale : taille raisonnable
                        if len(jpeg_frame) < 5000:  # Frame trop petite, probablement corrompue
                            continue
                        
                        # Stocker la frame pour capture instantanée
                        global last_frame_time
                        with frame_lock:
                            last_frame = jpeg_frame
                            last_frame_time = time.time()
                        
                        frames_received += 1
                        
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
        # Ne pas tuer le flux caméra si le mode aperçu est actif
        # Évite l'arrêt intempestif du processus lors d'une déconnexion client ou d'un petit glitch
        global camera_active
        if not camera_active:
            stop_camera_process()
        else:
            logger.info("[CAMERA] Générateur terminé, caméra laissée active (camera_active=True)")

def kill_camera_processes():
    """Tuer tous les processus caméra zombies de façon agressive"""
    try:
        # Tuer tous les processus rpicam-vid et libcamera-vid
        subprocess.run(['pkill', '-9', '-f', 'rpicam-vid'], 
                      capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'libcamera-vid'], 
                      capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'libcamera-still'], 
                      capture_output=True, timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'rpicam-still'], 
                      capture_output=True, timeout=2)
        time.sleep(0.3)  # Laisser le temps aux processus de mourir
        logger.info("[CAMERA] Processus caméra zombies nettoyés")
    except Exception as e:
        logger.warning(f"[CAMERA] Erreur lors du nettoyage des processus: {e}")

def stop_camera_process():
    """Arrêter proprement le processus caméra (Pi Camera ou USB)"""
    global camera_process, usb_camera
    
    with camera_lock:
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
                camera_process.wait(timeout=1)
            except:
                try:
                    camera_process.kill()
                    camera_process.wait(timeout=1)
                except:
                    pass
            camera_process = None
        
        # Toujours nettoyer les processus zombies
        kill_camera_processes()

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
    # Désactiver le reloader en mode kiosk par défaut pour éviter les courses au démarrage
    debug_mode = os.environ.get('SIMPLEBOOTH_DEBUG') == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)
