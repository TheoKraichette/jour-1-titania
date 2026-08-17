"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue les six phases d'une traite.

    python analyse.py
"""

import os
import urllib.request

import polars as pl

URL = (
    "https://raw.githubusercontent.com/planetsig/ufo-reports/master/csv-data/"
    "ufo-complete-geocoded-time-standardized.csv"
)
CSV = "releves_klaxo3.csv"

# Le fichier est livré sans en-têtes.
COLONNES = [
    "datetime",
    "city",
    "state",
    "country",
    "shape",
    "duration_seconds",
    "duration_hours_min",
    "comments",
    "date_posted",
    "latitude",
    "longitude",
]


def titre(n, nom):
    print(f"\n{'=' * 70}\nPHASE {n} — {nom}\n{'=' * 70}")


def recuperer_la_transmission():
    if os.path.exists(CSV):
        print(f"{CSV} déjà présent ({os.path.getsize(CSV) / 1e6:.1f} Mo)")
        return
    print(f"Téléchargement depuis {URL}")
    urllib.request.urlretrieve(URL, CSV)
    print(f"Reçu : {os.path.getsize(CSV) / 1e6:.1f} Mo")


def phase1_ouvrir_la_caisse():
    """Charger tout le fichier. Rendre : lignes du fichier, chargées, mises à part."""
    titre(1, "ouvrir la caisse")
    # TODO : compter les lignes brutes, charger avec has_header=False et
    # new_columns=COLONNES, récupérer les lignes rejetées au lieu de les perdre.
    df = None
    return df


def phase2_typer(df):
    """Chaque champ dans son vrai type, sans supprimer de ligne."""
    titre(2, "rien n'est du bon type")
    # TODO : convertir, puis compter et montrer les valeurs qui ont résisté.
    # Quatre anomalies de nature différente au minimum, avec leur origine.
    return df


def phase3_etiqueter_les_canulars(df):
    """Fabriquer l'étiquette « canular » : aucun champ ne la donne."""
    titre(3, "trier les canulars")
    # TODO : une règle tenant en une phrase, son compte, sa proportion, sa limite.
    # La source de l'étiquette conditionne la phase 5.
    return df


def phase4_premier_verdict(df):
    """Un modèle, évalué sur des relevés jamais vus : rappel et précision."""
    titre(4, "le premier verdict")
    # TODO
    return None


def phase5_fuite_temporelle(df):
    """Retirer les colonnes remplies après coup, réentraîner, comparer."""
    titre(5, "le Conseil ne vous croit pas")
    # TODO : tableau qui écrit quoi et quand, puis les deux nombres avant / après.
    return None


def phase6_modele_du_stagiaire(df):
    """Baseline « jamais un canular », à mettre en face du vrai modèle."""
    titre(6, "le modèle le plus bête du Bureau")
    # TODO
    return None


def main():
    recuperer_la_transmission()
    df = phase1_ouvrir_la_caisse()
    df = phase2_typer(df)
    df = phase3_etiqueter_les_canulars(df)
    phase4_premier_verdict(df)
    phase5_fuite_temporelle(df)
    phase6_modele_du_stagiaire(df)
    print("\nFin de l'analyse. Les chiffres sont à reporter dans RAPPORT.md.")


if __name__ == "__main__":
    main()
