
import cv2
import os

# Dossier de stockage local
dossier = "visages_autorises"
os.makedirs(dossier, exist_ok=True)

# Detecteur de visage
detecteur = cv2.CascadeClassifier(
    cv2.data.haarcascades +
    "haarcascade_frontalface_default.xml"
)

camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)

if not camera.isOpened():
    raise RuntimeError("Camera introuvable")

compteur = 0
maximum = 30

print("Appuie sur E pour enregistrer un visage.")
print("Appuie sur Q pour quitter.")

try:
    while True:
        ok, image = camera.read()

        if not ok:
            break

        gris = cv2.cvtColor(
            image, cv2.COLOR_BGR2GRAY
        )

        visages = detecteur.detectMultiScale(
            gris,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(80, 80)
        )

        for (x, y, w, h) in visages:
            cv2.rectangle(
                image,
                (x, y),
                (x + w, y + h),
                (0, 255, 0),
                2
            )

        cv2.putText(
            image,
            f"Photos : {compteur}/{maximum}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2
        )

        cv2.imshow("Enregistrement visage", image)

        touche = cv2.waitKey(1) & 0xFF

        if touche == ord("e") and len(visages) == 1:
            x, y, w, h = visages[0]

            visage = gris[y:y+h, x:x+w]
            visage = cv2.resize(visage, (200, 200))

            chemin = os.path.join(
                dossier,
                f"visage_{compteur:03d}.png"
            )

            cv2.imwrite(chemin, visage)
            compteur += 1

            print(f"Photo {compteur} enregistree")

            if compteur >= maximum:
                print("Enregistrement termine")
                break

        if touche == ord("q"):
            break

finally:
    camera.release()
    cv2.destroyAllWindows()
