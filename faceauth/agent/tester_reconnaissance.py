
import cv2
import serial
import time

# ---------------------------------
# CONFIGURATION ESP32 PAR USB
# ---------------------------------

PORT_ESP32 = "COM5"
VITESSE = 115200

esp32 = serial.Serial(
    PORT_ESP32,
    VITESSE,
    timeout=1,
    write_timeout=1
)

# L'ouverture du port peut redemarrer l'ESP32
time.sleep(2)

dernier_etat = None
etat_en_attente = None
images_consecutives = 0

# Nombre d'analyses identiques avant
# de changer les LED
STABILITE = 5


def envoyer_commande(etat):
    global dernier_etat

    commandes = {
        "reconnu": "RECONNU\n",
        "non_reconnu": "NON_RECONNU\n",
        "off": "OFF\n"
    }

    if etat == dernier_etat:
        return

    try:
        esp32.write(commandes[etat].encode("utf-8"))
        dernier_etat = etat
        print("Commande ESP32 :", etat)

    except serial.SerialException as erreur:
        print("Erreur USB ESP32 :", erreur)


# ---------------------------------
# RECONNAISSANCE FACIALE
# ---------------------------------

detecteur = cv2.CascadeClassifier(
    cv2.data.haarcascades +
    "haarcascade_frontalface_default.xml"
)

if detecteur.empty():
    raise RuntimeError("Detecteur introuvable")

modele = cv2.face.LBPHFaceRecognizer_create()
modele.read("modele_visage.yml")

# ---------------------------------
# WEBCAM
# ---------------------------------

camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)

if not camera.isOpened():
    esp32.close()
    raise RuntimeError("Camera introuvable")

camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

SEUIL = 60

# Analyse une image sur trois
INTERVALLE = 3
compteur = 0

# Resultats affiches sur l'image
resultats = []

try:
    # Eteindre les LED au demarrage
    envoyer_commande("off")

    while True:
        ok, image = camera.read()

        if not ok:
            break

        compteur += 1

        if compteur % INTERVALLE == 0:
            resultats = []
            nouvel_etat = "off"

            gris = cv2.cvtColor(
                image,
                cv2.COLOR_BGR2GRAY
            )

            petit_gris = cv2.resize(
                gris,
                None,
                fx=0.5,
                fy=0.5
            )

            visages = detecteur.detectMultiScale(
                petit_gris,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(40, 40)
            )

            if len(visages) == 1:
                x, y, w, h = visages[0]

                # Adapter les coordonnees
                x *= 2
                y *= 2
                w *= 2
                h *= 2

                visage = gris[y:y+h, x:x+w]
                visage = cv2.resize(
                    visage,
                    (200, 200)
                )

                identifiant, distance = modele.predict(
                    visage
                )

                if identifiant == 1 and distance < SEUIL:
                    nouvel_etat = "reconnu"
                    texte = "VISAGE RECONNU"
                    couleur = (0, 255, 0)
                else:
                    nouvel_etat = "non_reconnu"
                    texte = "VISAGE NON RECONNU"
                    couleur = (0, 0, 255)

                resultats.append(
                    (x, y, w, h, texte, couleur)
                )

            elif len(visages) > 1:
                nouvel_etat = "off"
                resultats.append(
                    (
                        20, 40, 0, 0,
                        "PLUSIEURS VISAGES",
                        (0, 0, 255)
                    )
                )

            # ---------------------------------
            # STABILISER LA RECONNAISSANCE
            # ---------------------------------

            if nouvel_etat == etat_en_attente:
                images_consecutives += 1
            else:
                etat_en_attente = nouvel_etat
                images_consecutives = 1

            if images_consecutives >= STABILITE:
                envoyer_commande(nouvel_etat)

        # Affichage de la video
        for x, y, w, h, texte, couleur in resultats:
            if w > 0 and h > 0:
                cv2.rectangle(
                    image,
                    (x, y),
                    (x+w, y+h),
                    couleur,
                    2
                )
                position = (x, max(20, y-10))
            else:
                position = (x, y)

            cv2.putText(
                image,
                texte,
                position,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                couleur,
                2
            )

        cv2.imshow(
            "EPSI - Reconnaissance + ESP32 USB",
            image
        )

        # Appuyer sur Q pour quitter
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:
    try:
        esp32.write(b"OFF\n")
    except serial.SerialException:
        pass

    camera.release()
    cv2.destroyAllWindows()
    esp32.close()

    print("Programme termine")
