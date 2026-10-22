
import cv2

# Charger le detecteur de visages
detecteur = cv2.CascadeClassifier(
    cv2.data.haarcascades +
    "haarcascade_frontalface_default.xml"
)

if detecteur.empty():
    raise RuntimeError("Detecteur de visages introuvable")

# Ouvrir la webcam integree
camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)

if not camera.isOpened():
    print("Erreur : impossible d'ouvrir la camera")
    exit()

print("Camera active - Appuie sur Q pour quitter")

while True:
    ret, image = camera.read()

    if not ret:
        print("Erreur de lecture de la camera")
        break

    # Transformer en niveaux de gris
    gris = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Detecter les visages
    visages = detecteur.detectMultiScale(
        gris,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(60, 60)
    )

    # Dessiner un rectangle autour des visages
    for (x, y, largeur, hauteur) in visages:
        cv2.rectangle(
            image,
            (x, y),
            (x + largeur, y + hauteur),
            (0, 255, 0),
            2
        )

        cv2.putText(
            image,
            "Visage detecte",
            (x, max(y - 10, 20)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2
        )

    cv2.imshow("EPSI - Detection faciale", image)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

camera.release()
cv2.destroyAllWindows()
