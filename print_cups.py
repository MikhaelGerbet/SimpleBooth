#!/usr/bin/env python3
# coding: utf-8

"""
Script d'impression via CUPS pour imprimantes photo (Canon SELPHY, etc.)
Compatible avec les imprimantes configurées via le système CUPS.
Optimisé pour impression photo couleur haute qualité.

Usage:
  python3 print_cups.py --image photo.jpg
  python3 print_cups.py --image photo.jpg --printer "Canon_SELPHY_CP1500"
  python3 print_cups.py --image photo.jpg --copies 2

Installation: pip install Pillow
"""

import sys
import argparse
import os
import subprocess
import tempfile
from PIL import Image, ImageEnhance, ExifTags


def parse_arguments():
    """Parser les arguments de ligne de commande"""
    parser = argparse.ArgumentParser(description='Impression photo via CUPS')
    parser.add_argument('--image', type=str, required=True,
                       help='Chemin vers l\'image à imprimer (obligatoire)')
    parser.add_argument('--printer', type=str, default=None,
                       help='Nom de l\'imprimante CUPS (défaut: imprimante par défaut)')
    parser.add_argument('--copies', type=int, default=1,
                       help='Nombre de copies (défaut: 1)')
    parser.add_argument('--quality', type=str, default='high',
                       choices=['draft', 'normal', 'high'],
                       help='Qualité d\'impression (défaut: high)')
    parser.add_argument('--paper-size', type=str, default='4x6',
                       choices=['4x6', 'credit-card', 'square'],
                       help='Format du papier (défaut: 4x6)')
    return parser.parse_args()


def get_default_printer():
    """Récupérer l'imprimante par défaut via lpstat"""
    try:
        result = subprocess.run(['lpstat', '-d'], capture_output=True, text=True)
        if result.returncode == 0:
            output = result.stdout.strip()
            if ':' in output:
                return output.split(':')[1].strip()
    except Exception as e:
        print(f"Erreur récupération imprimante par défaut: {e}")
    return None


def list_printers():
    """Lister les imprimantes disponibles"""
    try:
        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True)
        if result.returncode == 0:
            printers = []
            for line in result.stdout.strip().split('\n'):
                if line.startswith('printer '):
                    parts = line.split()
                    if len(parts) >= 2:
                        printers.append(parts[1])
            return printers
    except Exception as e:
        print(f"Erreur listing imprimantes: {e}")
    return []


def check_printer_status(printer_name):
    """Vérifier le statut de l'imprimante"""
    try:
        result = subprocess.run(['lpstat', '-p', printer_name], capture_output=True, text=True)
        if result.returncode == 0:
            output = result.stdout.lower()
            if 'idle' in output:
                return True, "Imprimante prête"
            elif 'printing' in output:
                return True, "Imprimante en cours d'impression"
            elif 'disabled' in output:
                return False, "Imprimante désactivée"
            else:
                return True, "Statut accessible"
        else:
            return False, f"Imprimante '{printer_name}' introuvable"
    except Exception as e:
        return False, f"Erreur vérification: {e}"


def fix_image_orientation(img):
    """Corriger l'orientation de l'image selon les données EXIF"""
    try:
        # Trouver la clé EXIF pour l'orientation
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation] == 'Orientation':
                break
        
        exif = img._getexif()
        if exif is not None:
            exif_data = dict(exif.items())
            if orientation in exif_data:
                if exif_data[orientation] == 3:
                    img = img.rotate(180, expand=True)
                elif exif_data[orientation] == 6:
                    img = img.rotate(270, expand=True)
                elif exif_data[orientation] == 8:
                    img = img.rotate(90, expand=True)
    except (AttributeError, KeyError, IndexError):
        pass
    return img


def prepare_image_for_selphy(image_path, paper_size='4x6'):
    """
    Préparer l'image pour impression sur Canon SELPHY CP1500.
    Format carte postale 4x6 pouces (100x148mm) à 300 DPI.
    """
    try:
        img = Image.open(image_path)
        
        # Corriger l'orientation EXIF
        img = fix_image_orientation(img)
        
        # Convertir en RGB (obligatoire pour JPEG couleur)
        if img.mode in ('RGBA', 'P', 'LA', 'L'):
            # Créer un fond blanc pour les images avec transparence
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            else:
                img = img.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Dimensions pour Canon SELPHY CP1500 à 300 DPI
        # Format carte postale 4x6" = 100x148mm = 1200x1800 pixels à 300 DPI
        paper_sizes = {
            '4x6': (1800, 1200),      # Paysage: 6x4 pouces (1800x1200)
            'credit-card': (1024, 642),  # Format carte de crédit
            'square': (1200, 1200),    # Carré
        }
        
        target_width, target_height = paper_sizes.get(paper_size, (1800, 1200))
        
        # Déterminer l'orientation optimale
        img_ratio = img.width / img.height
        target_ratio = target_width / target_height
        
        # Si l'image est en portrait et le papier en paysage, pivoter
        if img.height > img.width and target_width > target_height:
            img = img.rotate(90, expand=True)
        elif img.width > img.height and target_height > target_width:
            img = img.rotate(90, expand=True)
        
        # Recalculer après rotation
        img_ratio = img.width / img.height
        
        # Calculer les dimensions pour remplir le papier (crop au centre)
        if img_ratio > target_ratio:
            # Image plus large proportionnellement
            new_height = target_height
            new_width = int(target_height * img_ratio)
        else:
            # Image plus haute proportionnellement
            new_width = target_width
            new_height = int(target_width / img_ratio)
        
        # Redimensionner avec haute qualité
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Centrer et rogner pour obtenir les dimensions exactes
        left = (new_width - target_width) // 2
        top = (new_height - target_height) // 2
        img = img.crop((left, top, left + target_width, top + target_height))
        
        # Améliorer légèrement les couleurs pour l'impression
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(1.05)  # Légère saturation
        
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.02)  # Léger contraste
        
        # Sauvegarder en JPEG haute qualité
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        img.save(temp_file.name, 'JPEG', quality=98, dpi=(300, 300))
        
        print(f"📐 Image préparée: {target_width}x{target_height} pixels (300 DPI)")
        
        return temp_file.name
        
    except Exception as e:
        print(f"❌ Erreur préparation image: {e}")
        return image_path


def print_image_cups(image_path, printer_name=None, copies=1, quality='high', paper_size='4x6'):
    """Imprimer l'image via CUPS avec les options optimales pour SELPHY"""
    try:
        cmd = ['lp']
        
        # Spécifier l'imprimante
        if printer_name:
            cmd.extend(['-d', printer_name])
        
        # Nombre de copies
        if copies > 1:
            cmd.extend(['-n', str(copies)])
        
        # Options d'impression pour photo couleur
        options = []
        
        # Qualité d'impression
        quality_map = {
            'draft': '3',
            'normal': '4', 
            'high': '5'
        }
        options.append(f'print-quality={quality_map.get(quality, "5")}')
        
        # Mode couleur
        options.append('print-color-mode=color')
        
        # Format papier pour SELPHY
        paper_map = {
            '4x6': 'Postcard.Fullbleed',
            'credit-card': 'w155h244',
            'square': 'w288h288'
        }
        media = paper_map.get(paper_size, 'Postcard.Fullbleed')
        options.append(f'media={media}')
        
        # Ajuster à la page
        options.append('fit-to-page')
        
        # Orientation automatique
        options.append('orientation-requested=0')
        
        # Ajouter toutes les options
        for opt in options:
            cmd.extend(['-o', opt])
        
        # Ajouter le fichier image
        cmd.append(image_path)
        
        print(f"🖨️  Commande: {' '.join(cmd)}")
        
        # Exécuter l'impression
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            output = result.stdout.strip()
            print(f"✅ Impression lancée: {output}")
            return True, output
        else:
            error = result.stderr.strip() if result.stderr else "Erreur inconnue"
            print(f"❌ Erreur: {error}")
            return False, error
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False, str(e)


def main():
    args = parse_arguments()
    
    # Vérifier que l'image existe
    if not os.path.exists(args.image):
        print(f"❌ Image '{args.image}' non trouvée")
        sys.exit(1)
    
    # Déterminer l'imprimante
    printer = args.printer
    if not printer:
        printer = get_default_printer()
        if not printer:
            printers = list_printers()
            if printers:
                printer = printers[0]
                print(f"📌 Utilisation: {printer}")
            else:
                print("❌ Aucune imprimante configurée")
                sys.exit(1)
    
    print(f"🖨️  Imprimante: {printer}")
    print(f"📄 Format: {args.paper_size}")
    print(f"⭐ Qualité: {args.quality}")
    
    # Vérifier le statut
    status_ok, status_msg = check_printer_status(printer)
    print(f"📊 Statut: {status_msg}")
    
    if not status_ok:
        print("❌ Imprimante non disponible")
        sys.exit(1)
    
    # Préparer l'image pour SELPHY
    print(f"🖼️  Préparation: {args.image}")
    prepared_image = prepare_image_for_selphy(args.image, args.paper_size)
    
    # Imprimer
    print(f"🖨️  Envoi à l'imprimante...")
    success, message = print_image_cups(
        prepared_image,
        printer_name=printer,
        copies=args.copies,
        quality=args.quality,
        paper_size=args.paper_size
    )
    
    # Nettoyer le fichier temporaire
    if prepared_image != args.image and os.path.exists(prepared_image):
        try:
            os.unlink(prepared_image)
        except:
            pass
    
    if success:
        print("✅ Impression terminée avec succès!")
        sys.exit(0)
    else:
        print(f"❌ Échec: {message}")
        sys.exit(1)


if __name__ == '__main__':
    main()
