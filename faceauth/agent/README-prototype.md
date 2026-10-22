# Resilience-Alpha — Reconnaissance faciale et ESP32

## Présentation

Ce projet est développé dans le cadre de la formation **EPSI Bac+5**, au sein du projet **Resilience-Alpha**.

L'objectif est de réaliser un prototype de contrôle d'accès utilisant la reconnaissance faciale et un microcontrôleur ESP32 pour signaler le résultat de l'identification à l'aide de LED.

Le système utilise une webcam pour capturer les visages, Python et OpenCV pour effectuer la reconnaissance, puis communique avec l'ESP32 par câble USB-C.

## Fonctionnalités

- Détection des visages en temps réel avec une webcam.
- Enregistrement d'images de référence d'un visage autorisé.
- Entraînement d'un modèle de reconnaissance faciale.
- Reconnaissance d'un visage enregistré.
- Détection d'un visage non reconnu.
- Communication Python–ESP32 par USB.
- Signalisation lumineuse avec deux LED :
  - 🟢 **LED verte** : visage reconnu, clignotement.
  - 🔴 **LED rouge** : visage non reconnu, allumage fixe.
- Extinction des LED lorsqu'aucun visage n'est détecté.
- Optimisation du traitement vidéo pour améliorer la fluidité.

## Technologies utilisées

| Technologie | Utilisation |
|---|---|
| Python | Développement du programme principal |
| OpenCV | Détection et reconnaissance faciale |
| Haar Cascade | Localisation des visages |
| LBPH | Reconnaissance faciale |
| PySerial | Communication USB avec l'ESP32 |
| ESP32 | Contrôle des LED |
| Arduino IDE | Programmation de l'ESP32 |
| Webcam | Capture vidéo |

## Architecture du système

```text
Webcam
   |
   v
Python + OpenCV
   |
   v
Détection du visage
   |
   v
Reconnaissance LBPH
   |
   v
Communication série USB
   |
   v
ESP32
   |
   +---- Visage reconnu ----> LED verte
   |
   +---- Visage inconnu ----> LED rouge
   |
   +---- Aucun visage ------> LED éteintes
```

## Structure du projet

```text
resilience-alpha-reco/
├── enregistrer_visage.py
├── entrainer_visage.py
├── reconnaissance.py
├── tester_reconnaissance.py
├── test_led_usb.py
├── README.md
└── .gitignore
```

Les images de référence, le modèle entraîné et l'environnement virtuel Python ne sont pas publiés sur GitHub.

## Installation

### Prérequis

- Python 3.12
- Une webcam
- Un ESP32
- Deux LED (verte et rouge)
- Deux résistances de protection (220 à 330 Ω)
- Un câble USB-C
- Arduino IDE

### Installation des dépendances Python

Sous Windows, depuis le dossier du projet :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install opencv-contrib-python==4.11.0.86 pyserial
```

## Utilisation

### 1. Enregistrer un visage autorisé

```powershell
.\.venv\Scripts\python.exe enregistrer_visage.py
```

Le programme capture les images de référence, stockées localement dans le dossier `visages_autorises/`.

### 2. Entraîner le modèle

```powershell
.\.venv\Scripts\python.exe entrainer_visage.py
```

Le modèle entraîné est enregistré dans `modele_visage.yml`.

### 3. Programmer l'ESP32

Dans Arduino IDE, téléverser le programme de communication série USB sur l'ESP32.

Les commandes attendues sont :

| Commande USB | Action |
|---|---|
| `RECONNU` | LED verte clignotante |
| `NON_RECONNU` | LED rouge allumée |
| `OFF` | Extinction des LED |

Le port série utilisé dans le prototype est `COM5`. Il doit être adapté en fonction de l'ordinateur.

### 4. Tester les LED

Fermer le Moniteur série Arduino, puis exécuter :

```powershell
.\.venv\Scripts\python.exe test_led_usb.py
```

### 5. Lancer la reconnaissance faciale

```powershell
.\.venv\Scripts\python.exe tester_reconnaissance.py
```

Une fenêtre vidéo s'ouvre et affiche le résultat de la reconnaissance.

Appuyer sur **Q** pour quitter le programme.

## Sécurité et confidentialité

Ce projet est un **prototype pédagogique** et ne doit pas être considéré comme un système d'authentification biométrique prêt pour la production.

- Les images utilisées pour l'entraînement doivent être collectées avec consentement.
- Les images et le modèle biométrique restent en local et sont exclus du dépôt Git.
- Le seuil de reconnaissance LBPH est expérimental.
- Le système ne dispose pas encore de mécanisme anti-usurpation par photographie ou vidéo.
- Un véritable contrôle d'accès doit inclure une validation côté serveur et des mécanismes d'authentification supplémentaires.

## État d'avancement

**Réalisé :**
- Capture des images de référence.
- Entraînement et test de la reconnaissance faciale.
- Montage et test des LED sur l'ESP32.
- Validation de la communication USB entre Python et l'ESP32.

**À finaliser :**
- Tests complets de la chaîne reconnaissance faciale → ESP32 → LED.
- Ajout d'une vérification par mot de passe.
- Tests de sécurité et documentation des scénarios.
- Stabilisation éventuelle de la communication Wi-Fi.

## Contexte pédagogique

Ce module constitue une partie du projet **Resilience-Alpha**, consacré à la résilience et à la sécurité des systèmes informatiques dans le cadre de la formation EPSI Bac+5.


1. Installer les dépendances (première utilisation)

Ouvrir PowerShell dans le dossier du projet :

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install opencv-contrib-python==4.11.0.86 pyserial

2. Capturer les photos du visage

.\.venv\Scripts\python.exe enregistrer_visage.py

Cette commande enregistre les photos de référence dans visages_autorises/.

3. Entraîner le modèle facial

.\.venv\Scripts\python.exe entrainer_visage.py

Cette commande génère le fichier modele_visage.yml.

4. Tester la reconnaissance faciale

.\.venv\Scripts\python.exe tester_reconnaissance.py

Résultat attendu :

Visage autorisé : VISAGE RECONNU

Visage inconnu : VISAGE NON RECONNU

Appuyer sur Q pour quitter.