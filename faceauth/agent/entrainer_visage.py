
import cv2
import os
import numpy as np

# Dossier contenant les 30 photos
DOSSIER = "visages_autorises"

images = []
identifiants = []

# Identifiant de l'utilisateur autorise
USER_ID = 1

for fichier in sorted(os.listdir(DOSSIER)):
    if fichier.endswith(".png"):
        chemin = os.path.join(DOSSIER, fichier)

        image = cv2.imread(
            chemin, cv2.IMREAD_GRAYSCALE
        )

        if image is not None:
            image = cv2.resize(image, (200, 200))
            images.append(image)
            identifiants.append(USER_ID)

print(f"{len(images)} photos chargees")

if len(images) == 0:
    raise RuntimeError("Aucune photo trouvee")

# Creer le modele de reconnaissance
modele = cv2.face.LBPHFaceRecognizer_create()

# Entrainer le modele
modele.train(
    images,
    np.array(identifiants, dtype=np.int32)
)

# Enregistrer le modele
modele.write("modele_visage.yml")

print("Apprentissage termine !")
print("Modele enregistre : modele_visage.yml")
