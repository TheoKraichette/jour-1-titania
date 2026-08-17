"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue les six phases d'une traite.

    python analyse.py
"""

import csv
import os
import re
import urllib.request
from collections import Counter

import matplotlib
import numpy as np
import polars as pl
from scipy.sparse import csr_matrix, hstack
from sklearn.decomposition import TruncatedSVD
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                             roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import OneHotEncoder, StandardScaler

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

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


def compter_lignes_physiques(chemin):
    with open(chemin, "rb") as f:
        return sum(bloc.count(b"\n") for bloc in iter(lambda: f.read(1 << 20), b""))


DATE_SEULE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _numerique(valeur):
    try:
        float(valeur)
        return True
    except ValueError:
        return False


def reconstructions(champs):
    """Reconstructions distinctes d'une ligne trop longue, en retirant un champ vide.

    Une reconstruction n'est retenue que si les champs repères retombent sur leurs
    pieds : date de publication, coordonnées et durée.
    """
    trouvees = set()
    for i, valeur in enumerate(champs):
        if valeur != "":
            continue
        candidat = tuple(champs[:i] + champs[i + 1 :])
        if len(candidat) != len(COLONNES):
            continue
        if (
            DATE_SEULE.match(candidat[8])
            and _numerique(candidat[9])
            and _numerique(candidat[10])
            and (candidat[5] == "" or _numerique(candidat[5]))
        ):
            trouvees.add(candidat)
    return trouvees


def phase1_ouvrir_la_caisse():
    """Charger tout le fichier. Rendre : lignes du fichier, chargées, mises à part."""
    titre(1, "ouvrir la caisse")

    lignes_physiques = compter_lignes_physiques(CSV)

    # Parcours manuel plutôt qu'un read_csv tolérant : on veut garder les lignes
    # rejetées sous la main, pas les laisser disparaître.
    conformes, rejets = [], []
    with open(CSV, encoding="utf-8", errors="replace", newline="") as f:
        for numero, champs in enumerate(csv.reader(f), start=1):
            if len(champs) == len(COLONNES):
                conformes.append(champs)
            else:
                rejets.append((numero, champs))

    total = len(conformes) + len(rejets)
    df = pl.DataFrame(
        conformes, schema={col: pl.String for col in COLONNES}, orient="row"
    )

    print(f"Lignes physiques (\\n)      : {lignes_physiques}")
    print(f"Enregistrements CSV        : {total}")
    print(f"Chargés (11 champs)        : {df.height}")
    print(f"Mis à part                 : {len(rejets)}")
    print(f"Cohérence                  : {df.height} + {len(rejets)} = {total}")

    if rejets:
        print("\nCe qu'il y a dans les lignes mises à part :")
        for nb_champs, combien in sorted(Counter(len(c) for _, c in rejets).items()):
            print(f"  {combien} ligne(s) à {nb_champs} champs au lieu de {len(COLONNES)}")

        numero, champs = rejets[0]
        print(f"\nExemple, ligne {numero} ({len(champs)} champs) :")
        for i, valeur in enumerate(champs):
            nom = COLONNES[i] if i < len(COLONNES) else "— en trop —"
            print(f"  [{i:2}] {nom:<19} {valeur[:60]}")

        # Peut-on les recoller en retirant le champ vide en trop ?
        combien = Counter(len(reconstructions(c)) for _, c in rejets)
        uniques = combien[1]
        ambigues = sum(n for k, n in combien.items() if k > 1)
        print(
            f"\nRecollage : {uniques} lignes ont une reconstruction unique, "
            f"{ambigues} en ont plusieurs, {combien[0]} aucune."
        )

    return df


NUMERIQUES = ["duration_seconds", "latitude", "longitude"]


def phase2_typer(df):
    """Chaque champ dans son vrai type, sans supprimer de ligne."""
    titre(2, "rien n'est du bon type")

    conversions = {col: pl.col(col).cast(pl.Float64, strict=False) for col in NUMERIQUES}
    conversions["datetime"] = pl.col("datetime").str.to_datetime(
        "%m/%d/%Y %H:%M", strict=False
    )
    # date_posted n'a pas d'heure : une vraie date, pas un datetime à minuit.
    conversions["date_posted"] = pl.col("date_posted").str.to_date(
        "%m/%d/%Y", strict=False
    )

    print(f"{'champ':<18} {'vides':>6} {'résistent':>10}   valeurs fautives")
    for col, expr in conversions.items():
        brut = df[col]
        converti = df.select(expr).to_series()
        vide = brut.str.strip_chars() == ""
        resiste = ~vide & converti.is_null()
        fautives = brut.filter(resiste).unique().head(4).to_list()
        print(f"{col:<18} {vide.sum():>6} {resiste.sum():>10}   {fautives}")

    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731
    duree = df["duration_seconds"]
    natures = {
        "lettre parasite dans latitude": df["latitude"].str.contains("[A-Za-z]"),
        "apostrophe inversée collée à la durée": duree.str.contains("`"),
        "espaces autour de la durée": (duree != duree.str.strip_chars()) & (duree != ""),
        "heure 24:00, qui n'existe pas": df["datetime"].str.contains(" 24:"),
        "entités HTML dans le témoignage": df["comments"].str.contains("&#"),
        "pays non renseigné": df["country"].str.strip_chars() == "",
    }
    # Celle-ci passe la conversion sans broncher : zéro est un nombre valide.
    lat = df.select(conversions["latitude"]).to_series()
    lon = df.select(conversions["longitude"]).to_series()
    natures["coordonnées à (0, 0), géocodage raté"] = (lat == 0) & (lon == 0)
    print("\nAnomalies par nature :")
    for nom, masque in natures.items():
        print(f"  {nom:<40} {masque.sum():>6}")

    # 24:00 = minuit de fin de journée, donc 00:00 du lendemain. Récupérable sans
    # rien inventer, contrairement au « 33q » de la latitude qu'on laisse en null.
    minuit_fin = pl.col("datetime").str.contains(" 24:")
    conversions["datetime"] = (
        pl.when(minuit_fin)
        .then(
            pl.col("datetime")
            .str.replace(" 24:", " 00:")
            .str.to_datetime("%m/%d/%Y %H:%M", strict=False)
            .dt.offset_by("1d")
        )
        .otherwise(conversions["datetime"])
    )
    print(f"\nRécupéré : {compte(minuit_fin)} heures 24:00 basculées au lendemain 00:00.")

    avant = df.height
    df = df.with_columns(**conversions)
    print(f"\nLignes : {avant} en entrée, {df.height} en sortie.")
    print("Type final de chaque champ :")
    for nom, type_final in df.schema.items():
        print(f"  {nom:<20} {type_final}")

    carte_des_observations(df)
    return df


def carte_des_observations(df):
    """La carte que le Conseil n'arrivait pas à tracer, une fois les types corrigés."""
    points = df.select("latitude", "longitude").drop_nulls()
    os.makedirs("figures", exist_ok=True)

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.scatter(points["longitude"], points["latitude"], s=1, alpha=0.15, linewidths=0)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{points.height} observations situées")
    fig.savefig("figures/carte.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"\nCarte : figures/carte.png ({points.height} points)")


def phase3_etiqueter_les_canulars(df):
    """Fabriquer l'étiquette « canular » : aucun champ ne la donne."""
    titre(3, "trier les canulars")

    bas = pl.col("comments").str.to_lowercase()
    # Le Bureau annote les dossiers douteux entre doubles parenthèses : ((HOAX??)),
    # ((NUFORC Note: Possible hoax?? PD)) ou ((NUFORC Note: Student report. PD)).
    dans_note = bas.str.contains(r"\(\([^)]*(?:hoax|student report)")
    df = df.with_columns(canular=dans_note)

    marques = int(df["canular"].sum())
    print("Règle : dans sa note, le Bureau parle de « hoax » ou de « student report ».")
    print(f"Marqués canulars : {marques} / {df.height} ({marques / df.height:.2%})")

    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731
    hoax = bas.str.contains(r"\(\([^)]*hoax")
    print(f"  dont « hoax »                                    : {compte(hoax)}")
    print(f"  dont « student report » seul                     : {compte(dans_note & ~hoax)}")
    print(f"  « hoax » écrit par le témoin, hors note, écarté  : "
          f"{compte(bas.str.contains('hoax', literal=True) & ~hoax)}")
    print(f"  parmi les marqués, « hoax?? », donc un doute     : "
          f"{compte(bas.str.contains('hoax??', literal=True) & dans_note)}")

    # Une méprise n'est pas un canular : le témoin a vu quelque chose, il l'a mal
    # identifié. Ces notes-là ne comptent pas.
    meprises = bas.str.contains(
        r"\(\([^)]*(?:venus|sirius|jupiter|contrail|satellite|meteor|missile"
        r"|advertising|balloon)"
    )
    print(f"  méprises annotées par le Bureau, non comptées    : {compte(meprises)}")

    print("\nExemples marqués :")
    for texte in df.filter(pl.col("canular"))["comments"].head(3).to_list():
        print("  -", texte[:100].replace("&#44", ","))

    return df


COLS_CAT = ["shape", "country", "state"]
COLS_NUM = ["duration_seconds", "latitude", "longitude", "heure", "mois", "annee"]
# Dérivées du récit lui-même, pas de son contenu : longueur et emportement.
COLS_STYLE = ["longueur", "exclamations"]
GRAINE = 0


# Les notes du Bureau, y compris celles qui ne sont pas refermées proprement.
NOTE_DU_BUREAU = r"\(\(.*?\)+|\(\(.*$"


def ajouter_delai(df):
    """Jours entre l'observation et la publication du signalement par le Bureau."""
    return df.with_columns(
        delai_jours=(
            pl.col("date_posted").cast(pl.Datetime) - pl.col("datetime")
        ).dt.total_days()
    )


def ajouter_temoignage(df):
    """Sépare ce que le témoin a raconté de ce que le Bureau a ajouté après coup."""
    df = df.with_columns(
        temoignage=pl.col("comments").str.replace_all(NOTE_DU_BUREAU, "")
    )
    # Tout se calcule sur le témoignage seul : mesurer les majuscules ou la longueur
    # sur `comments` ferait rentrer la note du Bureau par la fenêtre.
    return df.with_columns(
        heure=pl.col("datetime").dt.hour(),
        mois=pl.col("datetime").dt.month(),
        annee=pl.col("datetime").dt.year(),
        longueur=pl.col("temoignage").str.len_chars(),
        exclamations=pl.col("temoignage").str.count_matches("&#33"),
    )


def entrainer(df, texte, avec_delai):
    """Entraîne un modèle et l'évalue sur un quart des relevés, mis de côté d'avance.

    `texte` désigne la colonne de texte donnée au modèle, ou None pour n'en donner aucune.
    """
    y = df["canular"].to_numpy().astype(int)
    i_train, i_test = train_test_split(
        np.arange(len(y)), test_size=0.25, stratify=y, random_state=GRAINE
    )

    blocs_train, blocs_test = [], []

    if texte:
        tfidf = TfidfVectorizer(max_features=20000, min_df=2)
        textes = df[texte].to_list()
        blocs_train.append(tfidf.fit_transform([textes[i] for i in i_train]))
        blocs_test.append(tfidf.transform([textes[i] for i in i_test]))

    cats = df.select(COLS_CAT).fill_null("").to_numpy()
    encodeur = OneHotEncoder(handle_unknown="ignore", min_frequency=20)
    blocs_train.append(encodeur.fit_transform(cats[i_train]))
    blocs_test.append(encodeur.transform(cats[i_test]))

    colonnes = COLS_NUM + COLS_STYLE + (["delai_jours"] if avec_delai else [])
    nums = df.select(colonnes).to_numpy().astype(float)
    mediane = np.nanmedian(nums[i_train], axis=0)
    nums = np.where(np.isnan(nums), mediane, nums)
    echelle = StandardScaler()
    blocs_train.append(csr_matrix(echelle.fit_transform(nums[i_train])))
    blocs_test.append(csr_matrix(echelle.transform(nums[i_test])))

    modele = LogisticRegression(max_iter=2000, class_weight="balanced")
    modele.fit(hstack(blocs_train).tocsr(), y[i_train])
    X_test = hstack(blocs_test).tocsr()
    predit = modele.predict(X_test)

    return {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "exactitude": accuracy_score(y[i_test], predit),
        "auc": roc_auc_score(y[i_test], modele.decision_function(X_test)),
        "n_test": len(i_test),
        "canulars_test": int(y[i_test].sum()),
        "signalements": int(predit.sum()),
        "attrapes": int(((predit == 1) & (y[i_test] == 1)).sum()),
        "y_test": y[i_test],
    }


def phase4_premier_verdict(df):
    """Un modèle, évalué sur des relevés jamais vus : rappel et précision."""
    titre(4, "le premier verdict")

    resultat = entrainer(df, texte="comments", avec_delai=True)
    print(
        f"Test sur {resultat['n_test']} relevés jamais vus à l'entraînement, "
        f"dont {resultat['canulars_test']} canulars."
    )
    print(f"Sur 100 canulars réels, le système en attrape        : "
          f"{resultat['rappel'] * 100:.0f}")
    print(f"Sur 100 relevés signalés, en sont vraiment           : "
          f"{resultat['precision'] * 100:.0f}")
    return resultat


# Une ligne par colonne donnée au modèle. La dernière case est celle qui tranche :
# si la personne qui remplit le champ savait déjà, la colonne sort.
PROVENANCE = [
    ("comments : le témoignage", "témoin", "au signalement", False),
    ("comments : la note ((...))", "Bureau", "au traitement", True),
    ("shape", "témoin", "au signalement", False),
    ("country", "témoin", "au signalement", False),
    ("state", "témoin", "au signalement", False),
    ("duration_seconds", "témoin", "au signalement", False),
    ("datetime : heure, mois, année", "témoin", "au signalement", False),
    ("longueur et exclamations du récit", "témoin", "au signalement", False),
    ("latitude", "géocodage automatique", "au traitement", False),
    ("longitude", "géocodage automatique", "au traitement", False),
    ("delai_jours", "Bureau (date_posted)", "à la publication", True),
]


def phase5_fuite_temporelle(df, avant):
    """Retirer les colonnes remplies après coup, réentraîner, comparer."""
    titre(5, "le Conseil ne vous croit pas")

    print(f"{'ce que le modèle lit':<34} {'qui écrit':<22} {'à quel moment':<16} savait ?")
    for colonne, qui, quand, savait in PROVENANCE:
        print(f"{colonne:<34} {qui:<22} {quand:<16} {'OUI' if savait else 'non'}")

    sortent = [colonne for colonne, _, _, savait in PROVENANCE if savait]
    print(f"\nSortent du modèle : {', '.join(sortent)}")

    apres = entrainer(df, texte="temoignage", avec_delai=False)

    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'relevés dénoncés sur ' + str(apres['n_test']):<38}"
          f"{avant['signalements']:>8}{apres['signalements']:>8}")
    print(f"{'AUC (0.5 = hasard)':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")

    meilleur = meilleur_modele_honnete(df)
    print("\nMeilleur modèle honnête atteint (gradient boosting, témoignage résumé) :")
    print(f"  attrapés {meilleur['rappel'] * 100:.0f} / 100, "
          f"justes {meilleur['precision'] * 100:.0f} / 100, "
          f"{meilleur['signalements']} dénoncés, AUC {meilleur['auc']:.3f}")

    return apres, meilleur


def meilleur_modele_honnete(df):
    """Le meilleur qu'on obtienne sans jamais lire ce que le Bureau a écrit.

    Gradient boosting, qui attrape les effets non linéaires que la régression
    logistique manque, et témoignage résumé en 120 dimensions.
    """
    y = df["canular"].to_numpy().astype(int)
    i_train, i_test = train_test_split(
        np.arange(len(y)), test_size=0.25, stratify=y, random_state=GRAINE
    )
    encodeur = OneHotEncoder(
        handle_unknown="ignore", min_frequency=20, sparse_output=False
    )
    cats = df.select(COLS_CAT).fill_null("").to_numpy()
    nums = df.select(COLS_NUM + COLS_STYLE).to_numpy().astype(float)

    tfidf = TfidfVectorizer(max_features=20000, min_df=2)
    textes = df["temoignage"].to_list()
    resume = TruncatedSVD(n_components=120, random_state=GRAINE)
    texte_train = resume.fit_transform(tfidf.fit_transform([textes[i] for i in i_train]))
    texte_test = resume.transform(tfidf.transform([textes[i] for i in i_test]))

    modele = HistGradientBoostingClassifier(class_weight="balanced", random_state=GRAINE)
    modele.fit(
        np.hstack([encodeur.fit_transform(cats[i_train]), nums[i_train], texte_train]),
        y[i_train],
    )
    X_test = np.hstack([encodeur.transform(cats[i_test]), nums[i_test], texte_test])
    predit = modele.predict(X_test)

    return {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "exactitude": accuracy_score(y[i_test], predit),
        "auc": roc_auc_score(y[i_test], modele.predict_proba(X_test)[:, 1]),
        "signalements": int(predit.sum()),
        "attrapes": int(((predit == 1) & (y[i_test] == 1)).sum()),
    }


def phase6_modele_du_stagiaire(honnete, meilleur):
    """Baseline « jamais un canular », à mettre en face du vrai modèle."""
    titre(6, "le modèle le plus bête du Bureau")

    vrai = honnete["y_test"]
    stagiaire = np.zeros_like(vrai)  # « ce n'est pas un canular », toujours
    canulars = int(vrai.sum())

    print("Système du stagiaire : répondre « ce n'est pas un canular », quoi qu'il arrive.")
    print(f"\n{'':<34}{'bonnes réponses':>17}{'canulars attrapés':>20}")
    for nom, exactitude, attrapes in (
        ("le stagiaire", accuracy_score(vrai, stagiaire), 0),
        ("mon modèle (régression)", honnete["exactitude"], honnete["attrapes"]),
        ("mon meilleur modèle (boosting)", meilleur["exactitude"], meilleur["attrapes"]),
    ):
        print(f"{nom:<34}{exactitude:>16.2%}{f'{attrapes} / {canulars}':>20}")

    print("\nLe stagiaire gagne sur les bonnes réponses parce que dire non à tout suffit")
    print(f"quand seulement {vrai.mean():.2%} des relevés sont des canulars.")
    print("La mesure à présenter au Conseil est donc le nombre de canulars attrapés.")


def main():
    recuperer_la_transmission()
    df = phase1_ouvrir_la_caisse()
    df = phase2_typer(df)
    df = phase3_etiqueter_les_canulars(df)
    df = ajouter_temoignage(ajouter_delai(df))
    avant = phase4_premier_verdict(df)
    honnete, meilleur = phase5_fuite_temporelle(df, avant)
    phase6_modele_du_stagiaire(honnete, meilleur)
    print("\nFin de l'analyse. Les chiffres sont à reporter dans RAPPORT.md.")


if __name__ == "__main__":
    main()
